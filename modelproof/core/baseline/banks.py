"""基准库（bank）管理。

「自定义基准库」的落点：不同用途的对照数据应该分开存放 ——
你自己采的公司内部端点、社区贡献的公开模型、官方元数据骨架，
混在一起会让「这次到底跟谁比」变得模糊。

每个 bank 是一个独立目录：
    baselines/banks/{bank_id}/models/*.json

默认库 `default` 由旧的单目录 baselines/models 自动迁移而来，
保证已有数据不丢、CLI 行为不变。
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

BASELINES_DIR = Path(__file__).resolve().parents[3] / "baselines"
BANKS_ROOT = BASELINES_DIR / "banks"
LEGACY_MODELS_DIR = BASELINES_DIR / "models"

DEFAULT_BANK = "default"


def bank_models_dir(bank_id: str) -> Path:
    return BANKS_ROOT / _safe(bank_id) / "models"


def _safe(bank_id: str) -> str:
    keep = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in (bank_id or "").strip())
    return keep or DEFAULT_BANK


def ensure_migrated() -> None:
    """把旧的单目录模型迁到 banks/default/models（只做一次）。"""
    target = bank_models_dir(DEFAULT_BANK)
    if target.exists() and any(target.glob("*.json")):
        _write_meta(DEFAULT_BANK, "默认库", "内置骨架 + 你采到的条目")
        return
    if not LEGACY_MODELS_DIR.is_dir():
        target.mkdir(parents=True, exist_ok=True)
        return
    target.mkdir(parents=True, exist_ok=True)
    moved = 0
    for path in LEGACY_MODELS_DIR.glob("*.json"):
        destination = target / path.name
        if not destination.exists():
            shutil.move(str(path), str(destination))
            moved += 1
    if moved:
        # 旧目录留着但已空；不删除，避免误伤用户手动放进去的东西
        pass
    _write_meta(DEFAULT_BANK, "默认库", "内置骨架 + 你采到的条目")


def _write_meta(bank_id: str, name: str, description: str) -> None:
    """写 bank.json；已存在就不覆盖，避免把用户改过的显示名冲掉。"""
    import json
    from datetime import datetime, timezone

    directory = bank_models_dir(bank_id)
    directory.mkdir(parents=True, exist_ok=True)
    meta = directory.parent / "bank.json"
    if meta.exists():
        return
    meta.write_text(
        json.dumps(
            {
                "id": _safe(bank_id),
                "name": name or bank_id,
                "description": description,
                "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def create_bank(bank_id: str, name: str = "", description: str = "",
                origin: str = "self") -> Path:
    """创建一个新基准库，并写入 meta。

    `origin` 区分数据从哪来 —— 界面要能分清三件事：
      builtin   随仓库分发的实测指纹
      community 社区项目导入（口径已校验，但来源不可控）
      self      你自己采的
    这三者的话语权不同，混标会让「我采的」替「社区导入的」背锅。
    """
    directory = bank_models_dir(bank_id)
    directory.mkdir(parents=True, exist_ok=True)
    _write_meta(bank_id, name or bank_id, description)
    meta_path = directory.parent / "bank.json"
    if meta_path.exists() and origin != "self":
        try:
            import json

            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            meta["origin"] = origin
            meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
        except (ValueError, OSError):
            pass
    return directory


def list_banks() -> list[dict[str, Any]]:
    """列出所有基准库及其条目数。"""
    ensure_migrated()
    if not BANKS_ROOT.is_dir():
        return []
    banks: list[dict[str, Any]] = []
    for entry in sorted(BANKS_ROOT.iterdir()):
        if not entry.is_dir():
            continue
        models_dir = entry / "models"
        count = len(list(models_dir.glob("*.json"))) if models_dir.is_dir() else 0
        meta_path = entry / "bank.json"
        meta: dict[str, Any] = {}
        if meta_path.exists():
            try:
                import json

                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                meta = {}
        banks.append(
            {
                "id": entry.name,
                "name": meta.get("name") or entry.name,
                "description": meta.get("description", ""),
                "model_count": count,
                "with_fingerprints": _count_with_fingerprints(entry.name),
                "builtin": entry.name == DEFAULT_BANK,
                # 缺省规则：default 库是内置的，其余没标注的一律算「自采」
                "origin": ("builtin" if entry.name == DEFAULT_BANK
                           else meta.get("origin") or "self"),
                "created_at": meta.get("created_at", ""),
            }
        )
    # 迁移后旧目录可能还在，也当作一个库展示
    if LEGACY_MODELS_DIR.is_dir():
        leftover = list(LEGACY_MODELS_DIR.glob("*.json"))
        if leftover and not any(b["id"] == DEFAULT_BANK for b in banks):
            banks.append(
                {
                    "id": DEFAULT_BANK,
                    "name": "默认",
                    "description": "旧目录遗留",
                    "model_count": len(leftover),
                    "with_fingerprints": _count_with_fingerprints(DEFAULT_BANK),
                    "builtin": True,
                    "created_at": "",
                }
            )
    return banks


def _count_with_fingerprints(bank_id: str) -> int:
    """这个库里有多少条**真的带实测指纹**。

    选数据来源时这是最该看的数字：只有身份元数据的条目一条证据也给不出。
    延迟 import 是为了避免 banks → store → banks 的循环。
    """
    try:
        from .store import BaselineStore

        store = BaselineStore(bank=bank_id)
        store.load()
        return int(store.stats().get("with_fingerprints") or 0)
    except Exception:  # noqa: BLE001  统计失败不该让列表页打不开
        return 0


def delete_bank(bank_id: str) -> bool:
    """删除一个自定义库。默认库不允许删。"""
    if _safe(bank_id) == DEFAULT_BANK:
        return False
    import shutil as _shutil

    directory = BANKS_ROOT / _safe(bank_id)
    if directory.is_dir():
        _shutil.rmtree(directory)
        return True
    return False
