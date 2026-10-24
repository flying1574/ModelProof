"""任务持久化与历史记录。

以前任务只活在 Web 进程的内存字典里：切走页面轮询就断，重启进程任务就没了。
现在每条任务都落盘成 JSON，进程重启、页面切换都不影响——前端只要带着
task_id 回来就能接着看进度。

历史列表也直接由这份目录派生：扫描 tasks/*.json 汇总出
「时间 / 被测模型 / 基准库 / 结论」，不额外维护一套索引，避免两处不一致。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

DATA_ROOT = Path.home() / ".modelproof"
TASKS_DIR = DATA_ROOT / "tasks"
REPORTS_DIR = DATA_ROOT / "reports"

STATUS_ACTIVE = "running"
STATUS_DONE = "completed"
STATUS_FAILED = "failed"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_task_id(prefix: str = "vfy") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class TaskStore:
    """任务读写。文件即事实来源。"""

    def __init__(self, directory: str | Path | None = None) -> None:
        self.dir = Path(directory) if directory else TASKS_DIR
        self.dir.mkdir(parents=True, exist_ok=True)

    # -- 写 ---------------------------------------------------------------

    def create(self, task_id: str, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        task = {
            "id": task_id,
            "kind": kind,                      # verification | collection
            "status": STATUS_ACTIVE,
            "progress": {},
            "input": _safe_input(payload),
            "created_at": _now(),
            "updated_at": _now(),
            "report": None,
            "result": None,
            "error": None,
        }
        self.save(task)
        return task

    def save(self, task: dict[str, Any]) -> None:
        task["updated_at"] = _now()
        path = self.dir / f"{task['id']}.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(task, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)

    def update(self, task_id: str, **fields: Any) -> dict[str, Any] | None:
        task = self.get(task_id)
        if task is None:
            return None
        task.update(fields)
        self.save(task)
        return task

    # -- 读 ---------------------------------------------------------------

    def get(self, task_id: str) -> dict[str, Any] | None:
        path = self.dir / f"{task_id}.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return None

    def list(self, kind: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        tasks: list[dict[str, Any]] = []
        for path in self.dir.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                continue
            if kind and data.get("kind") != kind:
                continue
            tasks.append(data)
        tasks.sort(key=lambda t: t.get("created_at") or "", reverse=True)
        return tasks[:limit]

    # -- 历史视图 ----------------------------------------------------------

    def history(self, limit: int = 50) -> list[dict[str, Any]]:
        """历史列表：时间 / 被测模型 / 基准库 / 结论。"""
        rows: list[dict[str, Any]] = []
        for task in self.list(kind="verification", limit=limit):
            rows.append(summarize(task))
        return rows

    def active(self) -> list[dict[str, Any]]:
        """仍在运行的任务 —— 前端回来时用它恢复轮询。"""
        return [t for t in self.list() if t.get("status") == STATUS_ACTIVE]

    def delete(self, task_id: str) -> bool:
        path = self.dir / f"{task_id}.json"
        if path.exists():
            path.unlink()
            return True
        return False


def summarize(task: dict[str, Any]) -> dict[str, Any]:
    """把一条任务压成历史列表里的一行。"""
    report = task.get("report") or {}
    inp = task.get("input") or {}
    layers = report.get("layers") or {}

    conclusion = None
    confidence = 0.0
    for layer in ("version", "series", "family", "vendor"):
        verdict = layers.get(layer)
        if verdict and not verdict.get("abstained") and verdict.get("value"):
            conclusion = verdict["value"]
            confidence = verdict.get("confidence", 0.0)
            break

    truth = report.get("truthfulness")
    return {
        "id": task.get("id"),
        "kind": task.get("kind"),
        "status": task.get("status"),
        "created_at": task.get("created_at"),
        "finished_at": report.get("finished_at"),
        "model": inp.get("declared_model") or inp.get("model") or "—",
        "claim": inp.get("claim"),
        "base_url": inp.get("base_url"),
        "bank": inp.get("bank") or "default",
        "suite": inp.get("suite"),
        "conclusion": conclusion,
        "confidence": round(confidence, 4),
        "truthfulness": (truth or {}).get("verdict"),
        "exit_code": _exit_code(report, task.get("status")),
        "progress": task.get("progress") or {},
    }


def _exit_code(report: dict[str, Any], status: str | None) -> int:
    if status == STATUS_FAILED:
        return 1
    if not report:
        return 2
    truth = report.get("truthfulness") or {}
    if truth.get("verdict") in ("inconsistent", "suspicious"):
        return 3
    layers = report.get("layers") or {}
    if not any(v.get("value") for v in layers.values() if not v.get("abstained")):
        return 2
    return 0


def _safe_input(payload: dict[str, Any]) -> dict[str, Any]:
    """落盘前抹掉密钥 —— 任务文件是明文 JSON，密钥绝不能进去。"""
    blocked = {"api_key", "apikey", "key", "authorization", "token"}
    return {k: v for k, v in (payload or {}).items() if k.lower() not in blocked}


def export_report(task: dict[str, Any], directory: str | Path | None = None) -> Path:
    """导出报告为独立 JSON 文件。"""
    target = Path(directory) if directory else REPORTS_DIR
    target.mkdir(parents=True, exist_ok=True)
    report = task.get("report") or {}
    name = report.get("run_id") or task.get("id")
    path = target / f"{name}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
