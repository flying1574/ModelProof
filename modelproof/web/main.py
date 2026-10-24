"""Web 入口：REST API + 页面。

三个入口共用 `core.orchestrator.run_verification`，所以 Web 与 CLI
的能力天然一致，产出的也是同一个 `VerificationReport` JSON。

选型说明：DESIGN 里写的是 FastAPI，这里用了 Flask —— 它与同一套
Jinja2 模板/原生 JS 前端配合更直接，且不加额外依赖即可运行。
REST 路径与 §2.3 的定义保持一致，将来换 FastAPI 不需要改契约。

任务是长操作（几十次请求），走后台线程 + 轮询。任务状态**落盘**
（core/tasks.py），所以页面切走再回来、甚至进程重启，都能接着看进度。
"""

from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, render_template, request, send_file

from ..core.channel_meta import CHANNEL_LABEL
from ..core.baseline.banks import create_bank, delete_bank, list_banks
from ..core.baseline.store import BaselineStore
from ..core.collect import collect_baseline, describe_plan
from ..core.estimate import format_estimate, load_price_table
from ..core.channels.backend_fp import load_known, load_ledger
from ..core.orchestrator import estimate_run, run_verification
from ..core.progress import Progress
from ..core.report import VerificationReport
from ..core.suite.registry import (
    COLLECT_PRIORITY,
    DEFAULT_SUITE,
    REGISTRY,
    collect_categories,
    pick_channels,
)
from ..core.tasks import TaskStore, export_report, new_task_id, summarize
from ..core.types import TargetLayer

TEMPLATES = Path(__file__).parent / "templates"
STATIC = Path(__file__).parent / "static"

app = Flask(__name__, template_folder=str(TEMPLATES), static_folder=str(STATIC))

TASKS = TaskStore()
# 仍在跑的线程，防止重复启动
_THREADS: dict[str, threading.Thread] = {}

# 界面上已经没有的旋钮，在这里定死（CLI 仍可通过参数覆盖）
#   价格表：只用于内部成本估算，界面不再让用户选
#   判定层级：一路打到最后一层，參考 ModelTrace 直接给最具体的结论
DEFAULT_PRICE_TABLE = "openai"
DEFAULT_TARGET_LAYER: TargetLayer = "version"


# --------------------------------------------------------------------------
# 任务执行
# --------------------------------------------------------------------------


def _resolve_items(payload: dict[str, Any]) -> list[Any]:
    """本次要跑哪些测试项。

    三种选法，对应界面上的两种模式：
      手动     显式 items（逗号分隔的测试项 id）
      自动·特定项目  categories（测试大类 id）—— 取这些大类下的全部项
      自动·快速      item_count（按通道优先级取前 N 项）
    顺序不能反：越具体的选法越应该赢。
    """
    explicit = payload.get("items")
    if explicit:
        return REGISTRY.resolve(str(explicit))

    wanted = payload.get("categories")
    if wanted:
        channels = {str(c).strip() for c in wanted if str(c).strip()}
        picked = [i for i in REGISTRY.items() if i.channel in channels]
        return picked or REGISTRY.pick(None, payload.get("suite"))

    return REGISTRY.pick(payload.get("item_count"), payload.get("suite"))


def _categories_with_members(items: list[Any]) -> list[dict[str, Any]]:
    """大类 + 它下面的具体测试项。

    手动模式要逐项勾选，就必须看见项本身；但只有切到手动时才展示。
    """
    categories = REGISTRY.categories(items)
    members: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        members.setdefault(item.channel, []).append({
            "id": item.id,
            "name": item.name,
            "requests": int(item.requests or 0),
            "description": item.description or "",
            "manual_friendly": bool(item.manual_friendly),
        })
    for cat in categories:
        cat["members"] = members.get(cat["id"], [])
    return categories


def _single_candidate(payload: dict[str, Any]) -> list[str] | None:
    """对照模型只允许一个。

    界面是单选；接口层也收口，避免「选了三个、其实只用了第一个」这种
    看不出来的一厢情愿。留空 = 在所选数据来源内自由比对。
    """
    raw = payload.get("candidates") or payload.get("include") or None
    if raw is None:
        return None
    names = [str(n).strip() for n in raw] if isinstance(raw, (list, tuple)) else [str(raw).strip()]
    names = [n for n in names if n]
    return names[:1] or None


def _run_verification(task_id: str, payload: dict[str, Any]) -> None:
    def on_progress(progress: Progress) -> None:
        TASKS.update(task_id, progress=progress.to_dict())

    # 界面不再提供「价格表 / 成本上限 / 判定层级 / 声称身份」这几个旋钮，
    # 一律走下面这套固定口径：
    #   - 没有价格概念，所以不过费用预算，只留时间上限
    #   - 打到最后一层（version），像 ModelTrace 那样直接给最具体的结论
    #   - 不传 claim，于是也不会产出「真伪不符」这种需要对照物的判定
    # CLI 仍然可以传这些参数，底层能力没删，只是界面不给。
    price_table = str(payload.get("price_table") or DEFAULT_PRICE_TABLE)
    try:
        report = run_verification(
            payload.get("base_url", ""),
            payload.get("api_key", ""),
            payload.get("model", ""),
            suite=payload.get("suite"),
            items=",".join(i.id for i in _resolve_items(payload)) or None,
            claim=payload.get("claim"),
            target_layer=payload.get("target_layer") or DEFAULT_TARGET_LAYER,
            max_cost=payload.get("max_cost"),
            max_seconds=payload.get("max_seconds"),
            price_table=price_table,
            protocol=payload.get("protocol"),
            bank=payload.get("bank"),
            include=_single_candidate(payload),
            reasoning=bool(payload.get("reasoning")),
            on_progress=on_progress,
        )
        TASKS.update(
            task_id,
            status="completed",
            progress={"label": "完成", "pct": 100, "done": True},
            report=report.to_dict(),
        )
    except Exception as error:  # noqa: BLE001
        TASKS.update(task_id, status="failed", error=str(error),
                     progress={"label": "失败", "pct": 100, "done": True})
    finally:
        _THREADS.pop(task_id, None)


def _run_collection(task_id: str, payload: dict[str, Any]) -> None:
    def on_progress(progress: Progress) -> None:
        TASKS.update(task_id, progress=progress.to_dict())

    try:
        create_bank(
            payload.get("bank") or "default",
            name=payload.get("bank_name", ""),
            description=payload.get("bank_description", ""),
        )
        entry, notes = collect_baseline(
            payload.get("base_url", ""),
            payload.get("api_key", ""),
            payload.get("model", ""),
            vendor=payload.get("vendor", ""),
            family=payload.get("family", ""),
            series=payload.get("series", ""),
            version=payload.get("version", ""),
            tier=payload.get("tier", "community"),
            bank=payload.get("bank"),
            channels=tuple(payload.get("channels") or pick_channels(payload.get("channel_count"))),
            aliases=payload.get("aliases") or [],
            note=payload.get("note", ""),
            on_progress=on_progress,
        )
        TASKS.update(
            task_id,
            status="completed",
            progress={"label": "完成", "pct": 100, "done": True},
            result={
                "model_id": entry.model_id,
                "bank": payload.get("bank") or "default",
                "channels": sorted((entry.channels or {}).keys()),
                "notes": notes,
            },
        )
    except Exception as error:  # noqa: BLE001
        TASKS.update(task_id, status="failed", error=str(error),
                     progress={"label": "失败", "pct": 100, "done": True})
    finally:
        _THREADS.pop(task_id, None)


def _start(kind: str, payload: dict[str, Any], runner) -> tuple[dict, int]:
    task_id = new_task_id("vfy" if kind == "verification" else "col")
    TASKS.create(task_id, kind, payload)
    thread = threading.Thread(target=runner, args=(task_id, payload), daemon=True)
    _THREADS[task_id] = thread
    thread.start()
    return {"id": task_id, "status": "running", "kind": kind}, 202


# --------------------------------------------------------------------------
# REST API —— 路径与 CLI 子命令一一对应
# --------------------------------------------------------------------------


@app.get("/v1/healthz")
def healthz():
    store = BaselineStore()
    return jsonify({
        "status": "ok",
        "version": "0.2.0",
        "baselines": store.count(),
        "banks": len(list_banks()),
        "channels": sorted({i.channel for i in REGISTRY.items()}),
    })


@app.post("/v1/estimates")
def create_estimate():
    payload = request.get_json(silent=True) or {}
    picked = _resolve_items(payload)
    result = estimate_run(
        picked,
        payload.get("price_table", "openai"),
        payload.get("model"),
        reasoning=bool(payload.get("reasoning")),
    )
    return jsonify({
        "suite": payload.get("suite") or DEFAULT_SUITE,
        "item_count": len(picked),
        "items": [i.id for i in picked],
        "categories": REGISTRY.categories(picked),
        "estimate": result.to_dict(),
        "formatted": format_estimate(result),
    })


@app.post("/v1/verifications")
def create_verification():
    payload = request.get_json(silent=True) or {}
    if not payload.get("base_url"):
        return jsonify({"error": "缺少 base_url"}), 400

    if str(payload.get("async", "1")) not in ("0", "false", "False"):
        body, code = _start("verification", payload, _run_verification)
        return jsonify(body), code

    # 同步模式：直接跑完再返回
    report = run_verification(
        payload.get("base_url", ""),
        payload.get("api_key", ""),
        payload.get("model", ""),
        suite=payload.get("suite"),
        items=",".join(i.id for i in _resolve_items(payload)) or None,
        claim=payload.get("claim"),
        target_layer=payload.get("target_layer") or DEFAULT_TARGET_LAYER,
        bank=payload.get("bank"),
        include=_single_candidate(payload),
    )
    return jsonify({"status": "completed", "report": report.to_dict()})


@app.get("/v1/verifications/<task_id>")
def get_verification(task_id: str):
    task = TASKS.get(task_id)
    if task is None:
        return jsonify({"error": "未找到该任务"}), 404
    return jsonify({
        "id": task_id,
        "status": task["status"],
        "progress": task.get("progress") or {},
        "error": task.get("error"),
        "report": task.get("report"),
        "result": task.get("result"),
        "summary": summarize(task),
    })


@app.get("/v1/verifications/<task_id>/report")
def get_report(task_id: str):
    task = TASKS.get(task_id)
    if task is None:
        return jsonify({"error": "未找到该任务"}), 404
    fmt = request.args.get("format", "json")
    if not task.get("report"):
        return jsonify({"error": "任务尚未完成"}), 409
    report = VerificationReport.from_dict(task["report"])
    if fmt == "md":
        return report.to_markdown(), 200, {"Content-Type": "text/plain; charset=utf-8"}
    if fmt == "html":
        return render_template("report.html", report=report.to_dict())
    return jsonify(report.to_dict())


@app.get("/v1/tasks/active")
def active_tasks():
    """仍在运行的任务 —— 前端切走再回来时用它恢复轮询。"""
    return jsonify({"tasks": [summarize(t) for t in TASKS.active()]})


@app.get("/v1/history")
def history():
    kind = request.args.get("kind")
    rows = [summarize(t) for t in TASKS.list(kind=kind)]
    return jsonify({"total": len(rows), "items": rows})


@app.get("/v1/history/<task_id>")
def history_detail(task_id: str):
    task = TASKS.get(task_id)
    if task is None:
        return jsonify({"error": "未找到"}), 404
    return jsonify({"summary": summarize(task), "task": task})


@app.get("/v1/history/<task_id>/export")
def history_export(task_id: str):
    task = TASKS.get(task_id)
    if task is None:
        return jsonify({"error": "未找到"}), 404
    if not task.get("report"):
        return jsonify({"error": "该任务没有可导出的报告"}), 409
    fmt = request.args.get("format", "json")
    report = VerificationReport.from_dict(task["report"])
    if fmt == "md":
        path = export_report(task, TASKS.dir / "exports")
        md_path = path.with_suffix(".md")
        md_path.write_text(report.to_markdown(), encoding="utf-8")
        return send_file(str(md_path), as_attachment=True,
                         download_name=f"{task['id']}.md")
    path = export_report(task)
    return send_file(str(path), as_attachment=True, download_name=f"{report.run_id}.json")


@app.delete("/v1/history/<task_id>")
def history_delete(task_id: str):
    return jsonify({"deleted": TASKS.delete(task_id)})


# ---- 基准库（bank） ----


@app.get("/v1/banks")
def banks_index():
    return jsonify({"banks": list_banks()})


@app.post("/v1/banks")
def banks_create():
    payload = request.get_json(silent=True) or {}
    bank_id = payload.get("id") or payload.get("bank")
    if not bank_id:
        return jsonify({"error": "缺少 id"}), 400
    create_bank(bank_id, payload.get("name", ""), payload.get("description", ""))
    return jsonify({"id": bank_id, "created": True}), 201


@app.delete("/v1/banks/<bank_id>")
def banks_delete(bank_id: str):
    ok = delete_bank(bank_id)
    if not ok:
        return jsonify({"error": "无法删除（默认库受保护或不存在）"}), 400
    return jsonify({"deleted": True})


@app.get("/v1/baselines")
def list_baselines():
    bank = request.args.get("bank")
    store = BaselineStore(bank=bank)
    entries = store.search(
        vendor=request.args.get("vendor"),
        family=request.args.get("family"),
    )
    return jsonify({
        "bank": store.bank,
        "total": len(entries),
        "stats": store.stats(),
        "entries": [e.to_dict() for e in entries],
    })


@app.get("/v1/baselines/<model_id>")
def get_baseline(model_id: str):
    store = BaselineStore(bank=request.args.get("bank"))
    entry = store.get(model_id)
    if entry is None:
        return jsonify({"error": "未找到"}), 404
    return jsonify(entry.to_dict())


# ---- 自定义采集 ----


@app.post("/v1/baselines/collections")
def create_collection():
    payload = request.get_json(silent=True) or {}

    channels = payload.get("channels")
    if not channels:
        # 界面只给「采集几项」，取哪几项由工具按通道优先级定
        channels = pick_channels(payload.get("channel_count"))

    # 先出计划：只算请求数，不校验端点——预估不该因为字段没填就失败
    if request.args.get("dry_run") == "1" or payload.get("dry_run"):
        return jsonify({
            "plan": describe_plan(channels),
            "channels": list(channels),
            "channel_count": len(channels),
            "categories": collect_categories(list(channels)),
        })

    if not payload.get("base_url") or not payload.get("model"):
        return jsonify({"error": "缺少 base_url 或 model"}), 400
    if not payload.get("vendor") or not payload.get("family"):
        return jsonify({"error": "缺少 vendor 或 family（基准必须知道它自己是谁）"}), 400

    body, code = _start("collection", payload, _run_collection)
    return jsonify(body), code


@app.get("/v1/baselines/collections/<task_id>")
def get_collection(task_id: str):
    task = TASKS.get(task_id)
    if task is None:
        return jsonify({"error": "未找到"}), 404
    return jsonify({
        "id": task_id,
        "status": task["status"],
        "progress": task.get("progress") or {},
        "result": task.get("result"),
        "error": task.get("error"),
    })


@app.get("/v1/suites")
def list_suites():
    return jsonify({"suites": [s.to_dict() for s in REGISTRY.suites()]})


@app.get("/v1/catalog")
def catalog():
    """打给界面的目录：上下限、默认值、取项顺序、大类与其下的具体项。

    为什么要单独一个接口：数量与选法都交给用户之后，前端必须知道上下限、
    默认值和「有哪些项」，而这些只能由注册表给出 —— 抄一份到前端，
    外部套件一变就对不上。
    """
    all_items = REGISTRY.pick(len(REGISTRY.items()))
    return jsonify({
        "test": {
            "total": len(all_items),
            "min": 1,
            "max": len(all_items),
            "default": len(REGISTRY.pick()),
            "default_suite": DEFAULT_SUITE,
            "categories": _categories_with_members(all_items),
            # 取哪几项的顺序：前端靠它算出「N 项覆盖哪些大类」，不必来回问
            "order": [
                {"id": i.id, "channel": i.channel,
                 "name": CHANNEL_LABEL.get(i.channel, i.channel)}
                for i in all_items
            ],
        },
        "collect": {
            "total": len(COLLECT_PRIORITY),
            "min": 1,
            "max": len(COLLECT_PRIORITY),
            "default": len(pick_channels()),
            "categories": collect_categories(pick_channels()),
            "order": [
                {"id": ch, "channel": ch, "name": CHANNEL_LABEL.get(ch, ch)}
                for ch in COLLECT_PRIORITY
            ],
        },
    })


@app.get("/v1/test-items")
def list_test_items():
    return jsonify({"items": [i.to_dict() for i in REGISTRY.items()]})


@app.get("/v1/fingerprints")
def list_fingerprints():
    return jsonify({"ledger": load_ledger(), "known": load_known()})


@app.get("/v1/price-tables")
def list_price_tables():
    name = request.args.get("name", "openai")
    return jsonify(load_price_table(name).to_dict())


# --------------------------------------------------------------------------
# 页面
# --------------------------------------------------------------------------


@app.get("/")
def index():
    store = BaselineStore()
    # 默认库已经融合了全部数据来源（内置实测 + 社区导入 + 自采），
    # 界面不再提供库切换 —— 闭集范围变了判定就跟着变，不该让用户每次纠结。
    return render_template(
        "index.html",
        catalog={
            "total": len(REGISTRY.items()),
            "min": 1,
            "max": len(REGISTRY.items()),
            "default": len(REGISTRY.pick()),
            "categories": _categories_with_members(REGISTRY.pick(len(REGISTRY.items()))),
        },
        stats=store.stats(),
    )


def _cat_names(channel_ids: list[str]) -> list[str]:
    """通道 id → 大类展示名，去重后按展示顺序排。"""
    seen = []
    for cid in channel_ids:
        name = CHANNEL_LABEL.get(cid, cid)
        if name not in seen:
            seen.append(name)
    return seen


def _summarize_entry(d: dict[str, Any]) -> dict[str, Any]:
    """把条目的通道明细收成「采集项数量 + 采集大类」，页面不再逐通道列举。"""
    channels = d.get("channels") or {}
    live = [k for k, v in channels.items() if v and v.get("enabled", True) is not False]
    d["channel_count"] = len(live)
    d["channel_categories"] = _cat_names(live)
    d.pop("channels", None)  # 明细一律不外露
    return d


def _summarize_stats(stats: dict[str, Any]) -> dict[str, Any]:
    """库级「通道覆盖」改名成大类覆盖：by_channel(通道) → by_category(大类)。"""
    by_cat: dict[str, int] = {}
    for cid, n in (stats.get("by_channel") or {}).items():
        name = CHANNEL_LABEL.get(cid, cid)
        by_cat[name] = by_cat.get(name, 0) + int(n or 0)
    stats["by_category"] = by_cat
    return stats


@app.get("/baselines")
def baselines_page():
    bank = request.args.get("bank")
    store = BaselineStore(bank=bank)
    store.load(force=True)
    vendor = request.args.get("vendor")
    entries = store.search(vendor=vendor)
    return render_template(
        "baselines.html",
        # 同样按「只给数量 + 大类」处理：把每条的通道字典收成计数与大类名
        entries=[_summarize_entry(e.to_dict()) for e in entries],
        stats=_summarize_stats(store.stats()),
        vendors=store.vendors(),
        current_vendor=vendor or "",
        problems=store.validate_all(),
        banks=list_banks(),
        current_bank=store.bank,
    )


@app.get("/collect")
def collect_page():
    channels = pick_channels()
    return render_template(
        "collect.html",
        banks=list_banks(),
        # 同上：只给数量与采集大类，不给通道明细卡片
        catalog={
            "total": len(COLLECT_PRIORITY),
            "min": 1,
            "max": len(COLLECT_PRIORITY),
            "default": len(channels),
            "categories": collect_categories(channels),
        },
    )


@app.get("/history")
def history_page():
    rows = [summarize(t) for t in TASKS.list(limit=100)]
    return render_template("history.html", rows=rows)


@app.get("/history/<task_id>")
def history_detail_page(task_id: str):
    return render_template("history_detail.html", task_id=task_id)


@app.get("/report/<task_id>")
def report_page(task_id: str):
    return render_template("report_page.html", task_id=task_id)


def create_app() -> Flask:
    return app


def main(host: str = "127.0.0.1", port: int = 7861, debug: bool = False) -> None:
    """启动 Web 服务。默认只监听本地回环。"""
    app.run(host=host, port=port, debug=debug, threaded=True)


if __name__ == "__main__":
    import sys

    port = int(sys.argv[1]) if len(sys.argv) > 1 else 7861
    main(port=port)
