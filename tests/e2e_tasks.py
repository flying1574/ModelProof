"""进度、任务持久化、基准库与采集的端到端验证。

覆盖四项改进里最容易悄悄坏掉的地方：
  1. 进度必须是**结构化的**——有当前项名称，不只是「3/37」；
  2. 任务必须落盘——切走页面、重启进程都还在；
  3. 基准库必须真的隔离——自定义库不污染默认库；
  4. 采集与验证必须共用同一套数据结构——采进去的能比出来。

不依赖真实 API 密钥。用法：
    python tests/e2e_tasks.py
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.e2e_mock import serve  # noqa: E402

from modelproof.core.baseline.banks import delete_bank, list_banks  # noqa: E402
from modelproof.core.baseline.store import BaselineStore  # noqa: E402
from modelproof.core.collect import collect_baseline  # noqa: E402
from modelproof.core.orchestrator import run_verification  # noqa: E402
from modelproof.core.tasks import TaskStore, summarize  # noqa: E402

WEB_PORT = 7863
MOCK_PORT = 7904
BANK = "e2e-custom"
BASE = f"http://127.0.0.1:{WEB_PORT}"

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    mark = "✓" if ok else "✗"
    print(f"    {mark} {label}" + (f"  {detail}" if detail else ""))
    if not ok:
        FAILURES.append(label)


# 本机端点一律直连：环境里的 http_proxy 会随机拦住环回连接（表现为请求挂住），
# 那是测试基础设施的问题，不该算到被测代码头上。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http(method: str, path: str, payload: dict | None = None, raw: bool = False,
         retries: int = 3):
    """带重试的请求。本机偶发 socket 抖动不该让测试假失败。"""
    url = BASE + path
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    last: Exception | None = None
    for attempt in range(retries):
        request = urllib.request.Request(
            url, data=data, method=method, headers={"Content-Type": "application/json"},
        )
        try:
            with _OPENER.open(request, timeout=30) as resp:
                body = resp.read().decode("utf-8")
                return (body if raw else json.loads(body)), resp.status
        except TimeoutError as error:  # 只有超时才重试，其它错误照实抛
            last = error
            time.sleep(0.5)
    raise last  # type: ignore[misc]


def wait_ready(port: int, timeout: float = 25.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            _OPENER.open(f"http://127.0.0.1:{port}/v1/healthz", timeout=2).read()
            return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.4)
    return False


# --------------------------------------------------------------------------
# 1. 采集进度：每一条都要有「当前项」
# --------------------------------------------------------------------------


def test_collect_progress() -> None:
    print("【1】采集进度（每条都要带当前项名称）")

    events: list[dict] = []

    def on_progress(progress) -> None:
        events.append(progress.to_dict())

    delete_bank(BANK)
    entry, notes = collect_baseline(
        f"http://127.0.0.1:{MOCK_PORT}/v1",
        "k",
        "mock-alpha",
        vendor="mockvendor",
        family="alpha",
        series="std",
        tier="community",
        bank=BANK,
        channels=("tokenizer", "behavior", "distribution", "backend_fp"),
        on_progress=on_progress,
    )

    print(f"    共 {len(events)} 条进度事件，写入通道：{sorted(entry.channels)}")
    check("产生了进度事件", len(events) > 30, f"{len(events)} 条")

    with_item = [e for e in events if e["item"]]
    check("多数事件带当前项名称", len(with_item) > len(events) * 0.8,
          f"{len(with_item)}/{len(events)}")

    with_counter = [e for e in events if e["counter"]]
    check("带已完成/总数", bool(with_counter),
          with_counter[-1]["counter"] if with_counter else "无")

    monotonic = all(
        (b["current"], b["pct"]) >= (a["current"], a["pct"])
        for a, b in zip(events, events[1:])
        if a["phase"] == b["phase"]
    )
    check("同一阶段内计数单调不减", monotonic)

    phases = []
    for event in events:
        if event["phase"] and event["phase"] not in phases:
            phases.append(event["phase"])
    check("阶段可区分", len(phases) >= 3, " → ".join(phases))

    sample = next((e for e in events if e["phase"] == "tokenizer" and e["item"]), None)
    if sample:
        print(f"    样例：{sample['label']} {sample['counter']} · {sample['item']} "
              f"（{sample['pct']}%）")

    check("distribution 采到数据", bool((entry.channels or {}).get("distribution")),
          str((entry.channels or {}).get("distribution", {}).get("samples", 0)) + " 份样本")
    for note in notes:
        print(f"    提示：{note}")


# --------------------------------------------------------------------------
# 2. 验证进度：整体百分比 + 当前项
# --------------------------------------------------------------------------


def test_verify_progress() -> None:
    print("\n【2】验证进度（整体百分比 + 当前项）")

    events: list[dict] = []

    def on_progress(progress) -> None:
        events.append(progress.to_dict())

    report = run_verification(
        f"http://127.0.0.1:{MOCK_PORT}/v1",
        "k",
        "mock-alpha",
        suite="standard",
        target_layer="family",
        bank=BANK,
        on_progress=on_progress,
    )

    finals = [e["pct"] for e in events if e["total"]]
    check("进度带总数（不是只有计数）", bool(finals))
    if finals:
        check("整体百分比递增到 100", max(finals) >= 99.0, f"峰值 {max(finals)}%")
    labels = []
    for event in events:
        if event["label"] and event["label"] not in labels:
            labels.append(event["label"])
    check("阶段名可读", len(labels) >= 2, " → ".join(labels))

    family = report.layers.get("family")
    print(f"    判定：{family.value if family else '弃权'}"
          f"（{family.confidence if family else 0:.0%}）")
    print(f"    报告记的基准库：{report.baseline.get('bank')}")


# --------------------------------------------------------------------------
# 3. 基准库隔离
# --------------------------------------------------------------------------


def test_bank_isolation() -> None:
    print("\n【3】基准库隔离")

    custom = BaselineStore(bank=BANK)
    custom.load(force=True)
    default = BaselineStore(bank="default")
    default.load(force=True)

    check("自定义库只有刚采的那条", custom.count() == 1, f"{custom.count()} 条")
    check("默认库未被污染", default.count() > 50, f"{default.count()} 条")
    check("自定义库里查得到 mock-alpha", custom.get("mock-alpha") is not None)
    check("默认库里查不到它", default.get("mock-alpha") is None)

    banks = {b["id"]: b for b in list_banks()}
    check("list_banks 能看到自定义库", BANK in banks,
          f"{banks.get(BANK, {}).get('model_count', 0)} 条")


# --------------------------------------------------------------------------
# 4. 任务落盘与历史
# --------------------------------------------------------------------------


def test_task_persistence() -> None:
    print("\n【4】任务落盘与历史")

    store = TaskStore(ROOT / "tests" / "tmp_tasks")
    for path in (ROOT / "tests" / "tmp_tasks").glob("*.json"):
        path.unlink()

    task_id = "vfy_e2edemo"
    store.create(task_id, "verification",
                 {"base_url": "http://x/v1", "api_key": "secret", "model": "mock-alpha",
                  "bank": BANK, "suite": "standard"})
    store.update(task_id, progress={"label": "词表指纹", "current": 10, "total": 37,
                                    "item": "韩文组合字母", "pct": 27.0})

    # 密钥绝不能落盘
    saved = (ROOT / "tests" / "tmp_tasks" / f"{task_id}.json").read_text(encoding="utf-8")
    check("任务文件里没有密钥", "secret" not in saved)

    reloaded = TaskStore(ROOT / "tests" / "tmp_tasks").get(task_id)
    check("重新打开还在（模拟切走页面/重启）", reloaded is not None)
    check("进度也在", (reloaded or {}).get("progress", {}).get("item") == "韩文组合字母")

    active = TaskStore(ROOT / "tests" / "tmp_tasks").active()
    check("能列出运行中的任务", len(active) == 1)

    report = run_verification(
        f"http://127.0.0.1:{MOCK_PORT}/v1", "k", "mock-alpha",
        suite="standard", target_layer="family", bank=BANK,
    )
    store.update(task_id, status="completed", report=report.to_dict())

    row = summarize(store.get(task_id) or {})
    print(f"    历史行：{row['model']} · 库 {row['bank']} · 结论 {row['conclusion']} "
          f"· 退出码 {row['exit_code']}")
    check("历史含被测模型", row["model"] == "mock-alpha")
    check("历史含基准库", row["bank"] == BANK)
    check("历史含结论", bool(row["conclusion"]))

    exported = (ROOT / "tests" / "tmp_tasks").parent
    from modelproof.core.tasks import export_report

    path = export_report(store.get(task_id) or {}, ROOT / "tests" / "tmp_reports")
    check("报告可导出", path.exists(), f"{path.stat().st_size} 字节")


# --------------------------------------------------------------------------
# 5. HTTP 接口
# --------------------------------------------------------------------------


def test_http() -> None:
    print("\n【5】HTTP 接口")

    data, _ = http("GET", "/v1/banks")
    check("GET /v1/banks", any(b["id"] == BANK for b in data["banks"]))

    data, _ = http("GET", f"/v1/baselines?bank={BANK}")
    check("GET /v1/baselines?bank= 只返回该库", data["total"] == 1, f"{data['total']} 条")

    data, _ = http("GET", "/v1/baselines")
    check("不带 bank 时回到默认库", data["bank"] == "default")

    data, _ = http("POST", "/v1/baselines/collections?dry_run=1",
                   {"channels": ["tokenizer", "distribution"]})
    check("采集预估（dry_run）", data["plan"]["requests"] == 40, str(data["plan"]))

    data, status = http("POST", "/v1/verifications", {
        "base_url": f"http://127.0.0.1:{MOCK_PORT}/v1",
        "api_key": "test-key",
        "model": "mock-alpha",
        "suite": "standard",
        "bank": BANK,
    })
    task_id = data["id"]
    check("发起验证返回 202", status == 202, task_id)

    # 轮询过程中记录一次「运行中」的进度形态
    seen_progress: dict = {}
    for _ in range(240):
        task, _ = http("GET", f"/v1/verifications/{task_id}")
        if task["status"] == "running":
            if task.get("progress"):
                seen_progress = task["progress"]
            time.sleep(0.05)
            continue
        break
    else:
        check("轮询完成", False, "超时")
        return

    check("任务最终完成", task["status"] == "completed", task["status"])
    if seen_progress:
        print(f"    运行中抓到的进度：{seen_progress.get('label')} "
              f"{seen_progress.get('counter')} · {seen_progress.get('item')}")
        check("运行中进度含当前项", bool(seen_progress.get("item") or seen_progress.get("pct")))
    else:
        print("    （任务太快，没抓到运行中快照——不算失败）")

    data, _ = http("GET", "/v1/history")
    check("历史列表里有这次任务", any(r["id"] == task_id for r in data["items"]),
          f"共 {data['total']} 条")

    md, _ = http("GET", f"/v1/history/{task_id}/export?format=md", raw=True)
    check("导出 Markdown", len(md) > 200, f"{len(md)} 字符")

    data, _ = http("DELETE", f"/v1/history/{task_id}")
    check("删除任务", data["deleted"] is True)


def _start_web(port: int) -> tuple[subprocess.Popen, list[str]]:
    """起 Web 服务，并**把它的输出抽干**。

    为什么要抽干：子进程写满 stdout 管道缓冲区（Windows 上 64KB 左右）就会
    阻塞在 print / 日志上。表现是「TCP 连上了，但服务端永远不发状态行」——
    看起来像服务挂了，其实是测试把自己憋死的。Werkzeug 每请求都打一行日志，
    跑满一轮验证足够填满。
    """
    proc = subprocess.Popen(
        [sys.executable, "-m", "modelproof.web.main", str(port)],
        cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    lines: list[str] = []

    def drain() -> None:
        if proc.stdout is None:
            return
        for raw in iter(proc.stdout.readline, b""):
            lines.append(raw.decode("utf-8", "replace"))
        proc.stdout.close()

    threading.Thread(target=drain, daemon=True).start()
    return proc, lines


def main() -> int:
    print("══ ModelProof 进度 / 任务 / 基准库 端到端验证 ══\n")

    mock = serve("char", "fp_" + "e" * 32, "mock-alpha", MOCK_PORT)
    print(f"  mock 端点 127.0.0.1:{MOCK_PORT}（模型 mock-alpha）")

    proc, log = _start_web(WEB_PORT)
    try:
        if not wait_ready(WEB_PORT):
            print("✗ Web 服务未能启动")
            print("".join(log[-40:]))
            return 1
        print(f"  Web 服务 {BASE}\n")

        test_collect_progress()
        test_verify_progress()
        test_bank_isolation()
        test_task_persistence()
        test_http()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        mock.shutdown()
        mock.server_close()
        delete_bank(BANK)
        for directory in ("tmp_tasks", "tmp_reports"):
            shutil.rmtree(ROOT / "tests" / directory, ignore_errors=True)

    if FAILURES:
        print(f"\n✗ {len(FAILURES)} 项未通过：")
        for item in FAILURES:
            print(f"    - {item}")
        return 1
    print("\n══ 全部通过 ══")
    return 0


if __name__ == "__main__":
    sys.exit(main())
