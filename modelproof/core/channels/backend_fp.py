"""C6 —— 后端指纹（system_fingerprint）。

OpenAI 的定义是「模型运行所依赖的后端配置」：模型快照 + 推理软件 +
数值设置 + 硬件的组合。它的价值不在识别身份，而在**变更检测**：
指纹一变，说明上游换了部署或模型版本。

2026-10-17 DeepSeek 上线 V4.1-Flash-1017 时，社区正是靠
`aeb56401…` → `e72d3c7f…` 提前发现的 —— 官方公告之前就有了信号。

各家支持情况（2026-10 核实）：
  OpenAI / Azure / DeepSeek / Qwen / Moonshot / 智谱  有
  Anthropic                                           没有（改用带日期模型名）
  Gemini                                              没有（有 modelVersion）
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..types import Evidence, TargetLayer
from .base import BaseChannel, RunContext

DATA_DIR = Path(__file__).resolve().parents[3] / "baselines" / "fingerprints"
LEDGER_FILE = DATA_DIR / "backend_fingerprints.json"
KNOWN_FILE = DATA_DIR / "known_fingerprints.json"

# 已知公开指纹。只收录有公开来源可查的，来源写进 note。
DEFAULT_KNOWN: dict[str, dict[str, str]] = {
    "e72d3c7f663d3e1383b00b3884cb4501": {
        "vendor": "deepseek",
        "model": "deepseek-chat / V4.1-Flash-1017",
        "as_of": "2026-10-17",
        "source": "社区观测，DeepSeek V4.1-Flash-1017 上线时切换",
    },
    "aeb56401ca74e127821c4f9126dcb669": {
        "vendor": "deepseek",
        "model": "deepseek-chat / V4-Flash（上一代）",
        "as_of": "2026-10 之前",
        "source": "社区观测，被 e72d3c7f… 取代",
    },
}

# DeepSeek 的自描述格式：fp_<hash>_prod<批次>_<量化>_kvcache
DEEPSEEK_FP_RE = re.compile(
    r"^fp_(?P<hash>[0-9a-f]{32})_prod(?P<batch>\d+)_(?P<quant>[a-z0-9]+)_(?P<cache>.+)$"
)


def parse_fingerprint(value: str) -> dict[str, Any]:
    """把指纹串拆成结构化信息。认不出的就原样返回。"""
    result: dict[str, Any] = {"value": value, "format": "opaque"}
    match = DEEPSEEK_FP_RE.match(value.strip())
    if match:
        result.update(
            {
                "format": "deepseek_selfdescribing",
                "hash": match.group("hash"),
                "deploy_batch": int(match.group("batch")),
                "quantization": match.group("quant"),
                "cache": match.group("cache"),
            }
        )
    elif re.fullmatch(r"[0-9a-f]{32}", value.strip()):
        result["format"] = "hash32"
    elif re.fullmatch(r"fp_[0-9a-f]+", value.strip()):
        result["format"] = "openai_style"
    return result


class BackendFingerprintChannel(BaseChannel):
    id = "backend_fp"
    yields = {"version"}
    reliability = 0.80

    def collect(self, ctx: RunContext) -> list[Evidence]:
        adapter = ctx.adapter
        # 搭车采集：一次极短补全，只为拿到 system_fingerprint。
        completion = adapter.complete("hi", max_tokens=1)
        if not completion.ok:
            return [
                self._evidence(
                    "backend_fp.read",
                    None,
                    "无法完成搭车请求，本通道不可用",
                    match=None,
                    note=completion.error or "请求失败",
                    weight_scale=0.0,
                )
            ]

        value = completion.system_fingerprint
        if not value:
            # Anthropic / Gemini 走这条路：没有 system_fingerprint，但模型名带日期。
            dated = _date_from_model_name(completion.model or ctx.declared_model)
            if dated:
                return [
                    self._evidence(
                        "backend_fp.read",
                        "version",
                        f"无 system_fingerprint；模型名带日期 {dated}",
                        metric={"dated_model": dated},
                        match=None,
                        note=(
                            "Anthropic 用带日期的模型名承担版本标识作用"
                            "（如 claude-sonnet-4-5-20250929），这是它的「指纹」。"
                        ),
                    )
                ]
            return [
                self._evidence(
                    "backend_fp.read",
                    None,
                    "端点未返回 system_fingerprint",
                    match=None,
                    note=(
                        "Anthropic 与 Gemini 不返回该字段；部分中转也会把它剥掉。"
                        "属正常现象，不构成可疑信号。"
                    ),
                    weight_scale=0.3,
                )
            ]

        parsed = parse_fingerprint(value)
        known = _known_lookup(value)
        endpoint = _endpoint_of(ctx, adapter)
        ledger = record_fingerprint(endpoint, completion.model or ctx.declared_model, value)

        observed = f"system_fingerprint = {value}"
        if parsed["format"] == "deepseek_selfdescribing":
            observed += (
                f"（批次 {parsed['deploy_batch']} · 量化 {parsed['quantization']}"
                f" · {parsed['cache']}）"
            )

        note_parts: list[str] = []
        if known:
            note_parts.append(
                f"已知指纹：{known.get('model')}（{known.get('source')}）"
            )
        if ledger.get("changed"):
            note_parts.append(
                f"⚠️ 指纹已变化：{ledger.get('previous')} → {value}，"
                f"上游可能换了部署或模型版本"
            )
        elif ledger.get("first_seen"):
            note_parts.append("首次记录，已写入台账；后续运行将自动比对。")
        if parsed["format"] == "deepseek_selfdescribing":
            note_parts.append("DeepSeek 自描述格式，可直接读出部署批次与量化方式。")

        return [
            self._evidence(
                "backend_fp.read",
                "version",
                observed,
                metric={
                    "fingerprint": value,
                    "format": parsed["format"],
                    "changed": bool(ledger.get("changed")),
                    "previous": ledger.get("previous"),
                },
                match=None,
                note="；".join(note_parts) or "已记录到台账。",
            )
        ]


def _date_from_model_name(name: str | None) -> str | None:
    if not name:
        return None
    match = re.search(r"(\d{4})(\d{2})(\d{2})$", name)
    if match:
        return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
    return None


def _endpoint_of(ctx: RunContext, adapter: Any) -> str:
    base = getattr(adapter, "config", None)
    return getattr(base, "base_url", "") or "unknown"


def _known_lookup(value: str) -> dict[str, str] | None:
    known = load_known()
    return known.get(value.strip())


def load_known() -> dict[str, dict[str, str]]:
    if KNOWN_FILE.exists():
        try:
            data = json.loads(KNOWN_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data:
                return data
        except (ValueError, OSError):
            pass
    return dict(DEFAULT_KNOWN)


def record_fingerprint(endpoint: str, model: str, value: str | None) -> dict[str, Any]:
    """写台账并返回本次记录（含是否变化）。"""
    if not value:
        return {}
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ledger: dict[str, Any] = {}
    if LEDGER_FILE.exists():
        try:
            ledger = json.loads(LEDGER_FILE.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            ledger = {}

    key = f"{endpoint}::{model}"
    entry = ledger.get(key) or {"endpoint": endpoint, "model": model, "history": []}
    previous = entry.get("current")
    entry["current"] = value
    entry["last_seen"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    changed = bool(previous and previous != value)
    if changed:
        entry["previous"] = previous
        entry["changed_at"] = entry["last_seen"]
    entry.setdefault("history", [])
    if value not in entry["history"]:
        entry["history"].append(value)
    entry["history"] = entry["history"][-9:]

    ledger[key] = entry
    LEDGER_FILE.write_text(
        json.dumps(ledger, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {"changed": changed, "previous": previous, "current": value}


def load_ledger() -> dict[str, Any]:
    if not LEDGER_FILE.exists():
        return {}
    try:
        return json.loads(LEDGER_FILE.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}
