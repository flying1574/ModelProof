"""BaselineEntry —— 基准（对照组）数据模型。

一条基准 = 一个已知模型的全部可验真数据。它是「闭集判定」的前提：
没有对照组，任何指纹都只是噪声。

`probe_version` 是硬约束：探针串变了版本号就得变，新旧向量不可比，
store 加载时会直接拒绝版本不匹配的条目，避免拿旧数据骗出高置信度。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

SCHEMA_VERSION = "1.0"

# 当前内核认可的探针版本。采集与比对都要对得上。
CURRENT_PROBE_VERSIONS = {
    "tokenizer": "tk1-37",
    "behavior": "bh1-12",
    "distribution": "db1-4",
    "onetoken": "ot1-8",
    "capability": "cp1-6",
    "style": "st1-1",
    "trace": "tr1-3",
    "validity": "vl1-8",
}


@dataclass
class Identity:
    vendor: str = ""
    family: str = ""
    series: str = ""
    version: str = ""
    release_date: str = ""
    knowledge_cutoff: str = ""
    context_window: int | None = None
    max_output_tokens: int | None = None
    modalities: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "vendor": self.vendor,
            "family": self.family,
            "series": self.series,
            "version": self.version,
            "release_date": self.release_date,
            "knowledge_cutoff": self.knowledge_cutoff,
            "context_window": self.context_window,
            "max_output_tokens": self.max_output_tokens,
            "modalities": list(self.modalities),
        }


@dataclass
class Provenance:
    """溯源。没有溯源的基准一律不采信 —— 这是硬规则。"""

    tier: str = "community"  # official | community | derived
    collected_by: str = ""
    method: str = ""
    collected_at: str = ""
    endpoint: str = ""
    source_url: str = ""
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "tier": self.tier,
            "collected_by": self.collected_by,
            "method": self.method,
            "collected_at": self.collected_at,
            "endpoint": self.endpoint,
            "source_url": self.source_url,
            "note": self.note,
        }


@dataclass
class BaselineEntry:
    schema_version: str = SCHEMA_VERSION
    model_id: str = ""
    aliases: list[str] = field(default_factory=list)
    identity: Identity = field(default_factory=Identity)
    channels: dict[str, Any] = field(default_factory=dict)
    pricing: dict[str, Any] = field(default_factory=dict)
    provenance: Provenance = field(default_factory=Provenance)

    # -- 便捷访问 ---------------------------------------------------------

    @property
    def vendor(self) -> str:
        return self.identity.vendor

    @property
    def family(self) -> str:
        return self.identity.family

    @property
    def series(self) -> str:
        return self.identity.series

    def layer_value(self, layer: str) -> str | None:
        if layer == "vendor":
            return self.identity.vendor or None
        if layer == "family":
            return self.identity.family or None
        if layer == "series":
            return self.identity.series or None
        if layer == "version":
            return self.model_id
        return None

    # -- 序列化 -----------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "model_id": self.model_id,
            "aliases": list(self.aliases),
            "identity": self.identity.to_dict(),
            "channels": self.channels,
            "pricing": self.pricing,
            "provenance": self.provenance.to_dict(),
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "BaselineEntry":
        identity = raw.get("identity") or {}
        provenance = raw.get("provenance") or {}
        return cls(
            schema_version=raw.get("schema_version", SCHEMA_VERSION),
            model_id=raw.get("model_id", ""),
            aliases=raw.get("aliases") or [],
            identity=Identity(
                vendor=identity.get("vendor", ""),
                family=identity.get("family", ""),
                series=identity.get("series", ""),
                version=identity.get("version", ""),
                release_date=identity.get("release_date", ""),
                knowledge_cutoff=identity.get("knowledge_cutoff", ""),
                context_window=identity.get("context_window"),
                max_output_tokens=identity.get("max_output_tokens"),
                modalities=identity.get("modalities") or [],
            ),
            channels=raw.get("channels") or {},
            pricing=raw.get("pricing") or {},
            provenance=Provenance(
                tier=provenance.get("tier", "community"),
                collected_by=provenance.get("collected_by", ""),
                method=provenance.get("method", ""),
                collected_at=provenance.get("collected_at", ""),
                endpoint=provenance.get("endpoint", ""),
                source_url=provenance.get("source_url", ""),
                note=provenance.get("note", ""),
            ),
        )


def validate(entry: BaselineEntry) -> list[str]:
    """返回问题列表；空列表表示通过。不抛异常，方便批量校验。"""
    problems: list[str] = []

    if not entry.model_id:
        problems.append("缺少 model_id")
    if not entry.identity.vendor:
        problems.append("缺少 identity.vendor")
    if not entry.identity.family:
        problems.append("缺少 identity.family")

    tier = entry.provenance.tier
    if tier not in ("official", "community", "derived"):
        problems.append(f"provenance.tier 非法：{tier}")
    if not entry.provenance.method:
        problems.append("缺少 provenance.method（数据是怎么来的）")

    # 探针版本必须与当前内核一致，否则这条数据不能参与比对。
    for channel, expected in CURRENT_PROBE_VERSIONS.items():
        payload = entry.channels.get(channel)
        if not payload:
            continue
        actual = payload.get("probe_version")
        if actual != expected:
            problems.append(
                f"channels.{channel}.probe_version={actual!r} 与内核要求 "
                f"{expected!r} 不一致，需重新采集或迁移"
            )

    # 内部一致性：自述的身份要和标注的家族对得上
    behavior = (entry.channels.get("behavior") or {}).get("vector") or {}
    claimed_family = behavior.get("identity")
    if claimed_family and entry.identity.family:
        if claimed_family != entry.identity.family:
            problems.append(
                f"自述家族 {claimed_family!r} 与标注家族 "
                f"{entry.identity.family!r} 不一致"
            )
    return problems
