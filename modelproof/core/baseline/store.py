"""基准库存储。

默认文件系统后端：一个模型一个 JSON，直接 git 版本化，可直接 PR，
这也是把数据独立成一个仓库的原因。接口保持简单，将来换 SQLite
或远端索引只要实现同一组方法。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .banks import DEFAULT_BANK, bank_models_dir, ensure_migrated
from .provenance import tier_weight
from .schema import BaselineEntry, validate

BASELINES_DIR = Path(__file__).resolve().parents[3] / "baselines"
MODELS_DIR = BASELINES_DIR / "models"   # 旧目录，保留兼容


class BaselineStore:
    """基准库读写。默认操作 `default` 库；传 bank 即可切换。"""

    def __init__(
        self,
        directory: str | Path | None = None,
        bank: str | None = None,
    ) -> None:
        if directory:
            self.directory = Path(directory)
            self.bank = bank or DEFAULT_BANK
        else:
            ensure_migrated()
            self.bank = _normalize_bank(bank)
            self.directory = bank_models_dir(self.bank)
        self._entries: dict[str, BaselineEntry] = {}
        self._loaded = False
        self._errors: list[str] = []

    # -- 加载 -------------------------------------------------------------

    def load(self, force: bool = False) -> None:
        if self._loaded and not force:
            return
        self._entries.clear()
        self._errors.clear()
        if not self.directory.is_dir():
            self._loaded = True
            return
        for path in sorted(self.directory.glob("*.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, OSError) as error:
                self._errors.append(f"{path.name}: 解析失败 {error}")
                continue
            entry = BaselineEntry.from_dict(raw)
            if entry.model_id:
                self._entries[entry.model_id] = entry
        self._loaded = True

    @property
    def errors(self) -> list[str]:
        self.load()
        return list(self._errors)

    # -- 查询 -------------------------------------------------------------

    def all(self) -> list[BaselineEntry]:
        self.load()
        return list(self._entries.values())

    def get(self, model_id: str) -> BaselineEntry | None:
        self.load()
        entry = self._entries.get(model_id)
        if entry:
            return entry
        lowered = model_id.lower()
        for entry in self._entries.values():
            candidates = [entry.model_id.lower()] + [a.lower() for a in entry.aliases]
            if lowered in candidates:
                return entry
        return None

    def search(
        self,
        vendor: str | None = None,
        family: str | None = None,
        series: str | None = None,
        has_channel: str | None = None,
    ) -> list[BaselineEntry]:
        self.load()
        results = self.all()
        if vendor:
            results = [e for e in results if e.identity.vendor == vendor]
        if family:
            results = [e for e in results if e.identity.family == family]
        if series:
            results = [e for e in results if e.identity.series == series]
        if has_channel:
            results = [e for e in results if (e.channels.get(has_channel) or {}).get("enabled", True)]
        return results

    def vendors(self) -> list[str]:
        return sorted({e.identity.vendor for e in self.all() if e.identity.vendor})

    def count(self) -> int:
        self.load()
        return len(self._entries)

    # -- 写入 -------------------------------------------------------------

    def save(self, entry: BaselineEntry) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f"{_safe_name(entry.model_id)}.json"
        path.write_text(entry.to_json(), encoding="utf-8")
        self._entries[entry.model_id] = entry
        return path

    def upsert_from_observation(
        self, entry: BaselineEntry, channel: str, payload: dict[str, Any]
    ) -> BaselineEntry:
        """把一次采集结果并入条目，并更新溯源。"""
        channels = dict(entry.channels)
        merged = dict(channels.get(channel) or {})
        merged.update(payload)
        merged["enabled"] = True
        channels[channel] = merged
        entry.channels = channels
        return entry

    # -- 校验 -------------------------------------------------------------

    def validate_all(self) -> list[tuple[str, list[str]]]:
        report = []
        for entry in self.all():
            problems = validate(entry)
            if problems:
                report.append((entry.model_id, problems))
        return report

    def stats(self) -> dict[str, Any]:
        entries = self.all()
        by_vendor: dict[str, int] = {}
        by_tier: dict[str, int] = {}
        by_channel: dict[str, int] = {}
        with_fingerprints = 0
        for entry in entries:
            by_vendor[entry.identity.vendor] = by_vendor.get(entry.identity.vendor, 0) + 1
            tier = entry.provenance.tier
            by_tier[tier] = by_tier.get(tier, 0) + 1
            usable = False
            for channel, payload in entry.channels.items():
                if payload and payload.get("enabled", True):
                    by_channel[channel] = by_channel.get(channel, 0) + 1
                    usable = True
            if usable:
                with_fingerprints += 1
        return {
            "total": len(entries),
            # 「有多少条真的带实测指纹」比「一共多少条」更能说明这个库能不能
            # 拿来做闭集比对 —— 只有身份元数据的条目一条证据也给不出。
            "with_fingerprints": with_fingerprints,
            "by_vendor": dict(sorted(by_vendor.items())),
            "by_tier": dict(sorted(by_tier.items())),
            "by_channel": dict(sorted(by_channel.items())),
            "weight_hint": {t: tier_weight(t) for t in by_tier},
        }


def _normalize_bank(bank: str | None) -> str:
    if not bank or bank == MODELS_DIR.name or bank == "models":
        return DEFAULT_BANK
    return bank


def _safe_name(model_id: str) -> str:
    keep = "".join(
        ch if ch.isalnum() or ch in "-_." else "_" for ch in model_id.strip()
    )
    return keep or "unnamed"


STORE = BaselineStore()
