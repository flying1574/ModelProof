#!/usr/bin/env python
"""把 ModelTrace 的实测指纹库融合进 ModelProof 的基准库。

ModelTrace 有一批我们已经花钱花时间采好的真实数据，ModelProof 此前只移植了
它的**方法**（distribution.py），没拿到**数据**，于是开箱状态下所有身份通道
只能弃权——这正是 vfy_b4255d725023 那类任务「什么都测不出来」的原因。

本脚本搬三样东西：

  1. **词表参考库**（6 个真实词表）
     gpt-o200k / gpt-oss-harmony / gpt-cl100k / qwen2-5 / deepseek-v3 /
     minimax-m2。抗蒸馏最强的通道，能直接回答「这个词表到底是不是 Anthropic
     的」。探针格与 ModelProof 的 tk1-37 完全一致（同键同序），
     只有版本号字符串写作 tk1-36，导入时改写。

  2. **数字分布指纹**（18 个模型的 355 维计数 + 74 维有序块）
     从参考 jsonl 的原始回答重新计算，用的是 ModelProof 自己的特征代码，
     保证采集端与比对端是同一套数学。

  3. **稳健打分包**（训练好的判别器 + 标定好的 beta）
     18 类 × 12 种提示词环境、756 条真实回答训练出来的类心与干扰基，
     带分组交叉验证定出的 beta（1/2/3 条 CV 准确率 0.958/0.998/1.000）
     和 out_of_scope=1.8 库外守卫。有了它，概率才是概率。

导入后必须记住的三条边界：
  * 这 18 个模型**全是 GPT 和 Claude**。遇到 Qwen / DeepSeek / Gemini，
    闭集归因只能硬塞最像的候选 —— 靠的是 out_of_scope 守卫与
    fusion/openset 的尺度无关复核来弃权，不是靠它认出来。
  * tier 一律标 `community`（第三方实测，非厂商官方发布）。
  * 采集时间 2026-08，模型可能已经换过版本。溯源带了 as_of，
    报告里会提示指纹的时效。

用法：
    python scripts/import_modeltrace.py                    # 自动找 ModelTrace
    python scripts/import_modeltrace.py --source /path/to/ModelTrace
    python scripts/import_modeltrace.py --bank default --dry-run
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modelproof.core.baseline.banks import ensure_migrated  # noqa: E402
from modelproof.core.baseline.schema import BaselineEntry, Identity, Provenance  # noqa: E402
from modelproof.core.baseline.store import BaselineStore  # noqa: E402
from modelproof.core.channels.distribution import (  # noqa: E402
    DIMENSION,
    PROBE_VERSION as DB_PROBE_VERSION,
    count_numbers,
    ordered_block_feature,
    parse_numbers,
)
from modelproof.core.channels.tokenizer import PROBE_VERSION as TK_PROBE_VERSION  # noqa: E402

FINGERPRINTS_DIR = ROOT / "baselines" / "fingerprints"
# 自己归档的 ModelTrace 数据副本（data/ 下的五个文件），复现路径的第一选择
ARCHIVE_DIR = ROOT / "baselines" / "imports" / "modeltrace"

# ModelTrace 里写作 tk1-36，但实际格数与我们的 tk1-37 完全相同（37 格、
# 同键同序）—— 只是版本字符串没跟上。导入时统一改写，别让校验把好数据挡掉。
LEGACY_TOKENIZER_VERSIONS = ("tk1-36",)

SOURCE_LABEL = "ModelTrace 实测指纹库"
SOURCE_URL = ""


# --------------------------------------------------------------------------
# 模型 ID 对齐
# --------------------------------------------------------------------------

_CLAUDE_DATED = re.compile(r"^(claude-(?:opus|sonnet|haiku))-(\d+)-(\d+)(?:-(\d{8}))?$")


def normalize_model_id(model_id: str) -> str:
    """ModelTrace 的 ID 用连字符，ModelProof 骨架用点。

    claude-haiku-4-5-20251001 -> claude-haiku-4.5
    claude-opus-4-6           -> claude-opus-4.6
    gpt-5.4                   -> gpt-5.4（不变）
    """
    match = _CLAUDE_DATED.match(model_id)
    if match:
        return f"{match.group(1)}-{match.group(2)}.{match.group(3)}"
    return model_id


def resolve_target(store: BaselineStore, model_id: str) -> BaselineEntry | None:
    """按 精确 ID → 别名 → 归一化 ID → 点杠互换 的顺序找已有条目。"""
    for candidate in (
        model_id,
        normalize_model_id(model_id),
        model_id.replace("-", "."),
        normalize_model_id(model_id).replace(".", "-"),
    ):
        found = store.get(candidate)
        if found is not None:
            return found
    return None


def infer_identity(model_id: str) -> Identity:
    """给库里还没有的条目补一份身份元数据。"""
    normalized = normalize_model_id(model_id)
    if normalized.startswith("claude-"):
        parts = normalized.split("-")
        series = parts[1] if len(parts) > 1 else ""
        version = parts[2] if len(parts) > 2 else ""
        return Identity(vendor="anthropic", family="claude", series=series, version=version)
    if normalized.startswith("gpt-"):
        return Identity(vendor="openai", family="gpt", series="standard",
                        version=normalized[len("gpt-"):])
    return Identity(family=normalized.split("-")[0] if "-" in normalized else normalized)


# --------------------------------------------------------------------------
# 三样数据的导入
# --------------------------------------------------------------------------


def import_tokenizer_references(source: Path, dry_run: bool) -> dict[str, Any]:
    raw = json.loads((source / "data" / "tokenizer_references.json").read_text(encoding="utf-8"))
    entries = []
    for item in raw:
        counts = item.get("counts") or {}
        version = item.get("probe_version")
        if version in LEGACY_TOKENIZER_VERSIONS:
            version = TK_PROBE_VERSION
        entries.append(
            {
                "id": item["id"],
                "label": item.get("label", item["id"]),
                "family": item.get("family", ""),
                "covers": item.get("covers", ""),
                "source": item.get("source", ""),
                "probe_version": version,
                "overhead": item.get("overhead", 0),
                "counts": counts,
                "imported_from": "ModelTrace data/tokenizer_references.json",
            }
        )
    payload = {
        "schema": "tokenizer-references",
        "probe_version": TK_PROBE_VERSION,
        "cells": len(next(iter(entries), {}).get("counts") or {}),
        "imported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "note": (
            "本地词表实测参考（tiktoken / HF tokenizer 直接数出来的 token 数），"
            "不是端点观测。命中即锁定词表，进而锁定家族；无法区分同词表的近亲版本。"
        ),
        "references": entries,
    }
    target = FINGERPRINTS_DIR / "tokenizer_references.json"

    # 合并，不是覆盖。词表参考不止一个来源：ModelTrace 给了 6 份，
    # scripts/build_tokenizer_references.py 又从 HF/tiktoken 自建了另外 6 份
    # （gemma3 / glm4 / kimi-k2 / llama3 / mistral-v3 / qwen3）。
    # 早先这里是整体 write_text，一跑导入就把自建那批冲掉过 —— 10-21 真的
    # 从 12 份掉回 6 份。按 id 合并：同名以新导入的为准，其余原样保留。
    merged = _merge_tokenizer_references(target, payload)
    if not dry_run:
        FINGERPRINTS_DIR.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "path": str(target),
        "count": len(merged["references"]),
        "imported": len(entries),
        "cells": merged["cells"],
    }


def _merge_tokenizer_references(target: Path, incoming: dict[str, Any]) -> dict[str, Any]:
    """把新导入的词表参考按 id 合进现有文件，保留其它来源的条目。"""
    try:
        existing = json.loads(target.read_text(encoding="utf-8"))
    except (ValueError, OSError, FileNotFoundError):
        return incoming

    by_id = {item["id"]: item for item in existing.get("references") or []}
    for item in incoming.get("references") or []:
        by_id[item["id"]] = item          # 同名覆盖，异名保留
    merged = dict(incoming)
    merged["references"] = list(by_id.values())
    merged["imported_at"] = incoming["imported_at"]
    merged["note"] = incoming["note"]
    return merged


def import_robust_pack(source: Path, dry_run: bool) -> dict[str, Any]:
    bank = json.loads((source / "data" / "unified_bank.json").read_text(encoding="utf-8"))
    robust = bank.get("robust") or {}
    model_order = robust.get("model_order") or []
    families = sorted(
        {
            (m.get("family") or "")
            for m in bank.get("models") or []
            if m.get("family")
        }
    )
    payload = {
        "schema": "modeltrace-robust-pack",
        "model_order": model_order,
        "robust": robust,
        "calibration": bank.get("calibration") or {},
        "method": bank.get("method") or {},
        "minimum_valid_numbers": bank.get("minimum_valid_numbers"),
        "recommended_queries": bank.get("recommended_queries"),
        "coverage": {
            "model_count": len(model_order),
            "families": families,
            "vendors": sorted(
                {"openai" if f == "gpt" else "anthropic" if f == "claude" else f for f in families}
            ),
            "training_rows": robust.get("training_rows"),
            "complete_environments": len(robust.get("complete_environments") or []),
            "note": (
                "只在 GPT / Claude 两个家族上训练过。库外模型不会被认出来，"
                "只会触发 out_of_scope 守卫 —— 这是设计，不是缺陷。"
            ),
        },
        "imported_from": "ModelTrace data/unified_bank.json",
        "imported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_as_of": bank.get("built_at", ""),
    }
    target = FINGERPRINTS_DIR / "robust_pack.json"
    if not dry_run:
        FINGERPRINTS_DIR.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "path": str(target),
        "models": len(model_order),
        "families": families,
        "ready": bool(robust.get("robust_ready")),
    }


def _load_reference_rows(source: Path) -> dict[str, list[dict[str, Any]]]:
    """读全部参考 jsonl，按 model_id 归堆。"""
    buckets: dict[str, list[dict[str, Any]]] = {}
    for name in ("gpt_reference.jsonl", "claude_reference.jsonl",
                 "claude-openrouter_reference.jsonl"):
        path = source / "data" / name
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("error") or not row.get("text"):
                continue
            buckets.setdefault(row.get("model_id") or "", []).append(row)
    return buckets


def _observation(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """把若干条原始回答合成一条可存档的分布观测。"""
    samples: list[list[int]] = []
    for row in rows:
        expected = int(row.get("requested_count") or 0)
        numbers = parse_numbers(str(row.get("text") or ""))
        minimum = max(80, -(-int(expected * 0.55) // 1)) if expected else 80
        if len(numbers) >= minimum:
            samples.append(numbers)
    if not samples:
        return None

    pooled = [0] * DIMENSION
    for numbers in samples:
        counts = count_numbers(numbers)
        for i in range(DIMENSION):
            pooled[i] += counts[i]

    width = len(ordered_block_feature(samples[0]))
    ordered = [0.0] * width
    for numbers in samples:
        feature = ordered_block_feature(numbers)
        for i in range(width):
            ordered[i] += feature[i] / len(samples)

    providers = sorted({str(r.get("provider") or "") for r in rows if r.get("provider")})
    collected = sorted({str(r.get("collected_at") or "") for r in rows if r.get("collected_at")})
    return {
        "probe_version": DB_PROBE_VERSION,
        "counts": pooled,
        "ordered": [round(v, 6) for v in ordered],
        "samples": len(samples),
        "total_numbers": sum(pooled),
        "source": {
            "tier": "community",
            "origin": "ModelTrace 参考采集",
            "providers": providers,
            "collected_at": collected[0] if collected else "",
            "as_of": (collected[-1] if collected else "")[:10],
            "note": "第三方实测，非厂商官方发布；模型版本可能已变更。",
        },
    }


def import_distribution(source: Path, bank_name: str, dry_run: bool) -> dict[str, Any]:
    unified = json.loads((source / "data" / "unified_bank.json").read_text(encoding="utf-8"))
    rows_by_model = _load_reference_rows(source)

    store = BaselineStore(bank=bank_name)
    store.load()
    merged: list[str] = []
    created: list[str] = []
    skipped: list[str] = []

    for model in unified.get("models") or []:
        mt_id = model.get("id") or ""
        if not mt_id:
            continue
        observation = _observation(rows_by_model.get(mt_id) or [])
        if observation is None:
            # 没有原始回答就退回库里的汇总计数（少了有序块，但边际仍可比）
            pooled = model.get("counts") or []
            if len(pooled) != DIMENSION:
                skipped.append(mt_id)
                continue
            observation = {
                "probe_version": DB_PROBE_VERSION,
                "counts": pooled,
                "ordered": [],
                "samples": int(model.get("response_count") or 0),
                "total_numbers": int(model.get("valid_number_count") or 0),
                "source": {
                    "tier": "community",
                    "origin": "ModelTrace 汇总计数（无原始序列，缺少有序块特征）",
                    "providers": [],
                    "collected_at": "",
                    "as_of": "2026-08-27",
                    "note": "仅有边际分布，有序块不可用。",
                },
            }

        entry = resolve_target(store, mt_id)
        if entry is None:
            entry = BaselineEntry(
                model_id=normalize_model_id(mt_id),
                aliases=[mt_id] if normalize_model_id(mt_id) != mt_id else [],
                identity=infer_identity(mt_id),
                provenance=Provenance(
                    tier="community",
                    collected_by="ModelTrace 参考采集",
                    method="1–355 整数选择任务 × "
                           f"{observation['samples']} 条回答，有序块 + 干扰剔除 Hellinger",
                    collected_at=observation["source"].get("collected_at") or "",
                    source_url=SOURCE_URL,
                    note="由 scripts/import_modeltrace.py 从 ModelTrace 融合而来。",
                ),
            )
            created.append(entry.model_id)
        else:
            if mt_id not in entry.aliases and mt_id != entry.model_id:
                entry.aliases = list(entry.aliases) + [mt_id]
            merged.append(entry.model_id)

        entry = store.upsert_from_observation(entry, "distribution", observation)

        # 溯源跟着数据走：条目级 tier 只描述「最弱的一环」，所以只有当条目
        # 原先只是骨架（derived）时才升到 community。
        if entry.provenance.tier == "derived":
            entry.provenance.tier = "community"
            entry.provenance.collected_by = "ModelTrace 参考采集"
            entry.provenance.method = (
                "1–355 整数选择任务 × "
                f"{observation['samples']} 条回答，有序块 + 干扰剔除 Hellinger"
            )
            entry.provenance.collected_at = observation["source"].get("collected_at") or ""
        entry.provenance.note = (
            (entry.provenance.note or "").rstrip()
            + " 数字分布指纹来自 " + SOURCE_LABEL + "（2026-08 采集，第三方实测）。"
            "仅覆盖 GPT / Claude 两个家族，库外模型靠 out_of_scope 守卫弃权。"
        ).strip()

        if not dry_run:
            store.save(entry)

    return {
        "bank": bank_name,
        "merged": merged,
        "created": created,
        "skipped": skipped,
    }


def find_source(explicit: str | None) -> Path | None:
    """定位 ModelTrace 的数据源。

    顺序：显式 --source > 自己归档的副本 > 外部的 ModelTrace 仓库。

    为什么优先归档副本：ModelTrace 是个独立仓库，随时可能被 reset、被拉新、
    被重构，复现路径挂在它上面就等于随时会断。10-21 就真的断过一次 ——
    ModelTrace 被 `git reset --hard` + `clean` 回上游，本地那批
    `data/*.json` / `data/*.jsonl` 全没了，而这个脚本立刻跑不了。
    把数据收进自己的 `baselines/imports/modeltrace/` 之后，
    「重新导入」这件事只取决于 ModelProof 自己。
    """
    if explicit:
        path = Path(explicit)
        return path if (path / "data" / "unified_bank.json").is_file() else None

    if (ARCHIVE_DIR / "data" / "unified_bank.json").is_file():
        return ARCHIVE_DIR

    for candidate in (
        ROOT.parent / "ModelTrace",
        Path("D:/data/working/ModelTrace"),
        Path.home() / "ModelTrace",
    ):
        if (candidate / "data" / "unified_bank.json").is_file():
            return candidate
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--source", help="ModelTrace 仓库根目录")
    parser.add_argument("--bank", default="default", help="目标基准库")
    parser.add_argument("--dry-run", action="store_true", help="只报告不写盘")
    args = parser.parse_args(argv)

    source = find_source(args.source)
    if source is None:
        print("找不到 ModelTrace：请用 --source 指定它的根目录"
              "（需要 data/unified_bank.json 与 data/tokenizer_references.json）")
        return 2

    ensure_migrated()
    print(f"源：{source}")
    print(f"目标库：{args.bank}{'（dry-run，不写盘）' if args.dry_run else ''}\n")

    tokenizer = import_tokenizer_references(source, args.dry_run)
    print(f"[1/3] 词表参考库  合并后 {tokenizer['count']} 个词表 × {tokenizer['cells']} 格"
          f"（本次导入 {tokenizer['imported']} 个）-> {tokenizer['path']}")

    pack = import_robust_pack(source, args.dry_run)
    print(f"[2/3] 稳健打分包  {pack['models']} 类 / 家族 {pack['families']} "
          f"/ ready={pack['ready']} -> {pack['path']}")

    dist = import_distribution(source, args.bank, args.dry_run)
    print(f"[3/3] 数字分布指纹 合并 {len(dist['merged'])} 条、新建 {len(dist['created'])} 条"
          f"{'、跳过 ' + str(dist['skipped']) if dist['skipped'] else ''}")
    if dist["created"]:
        print("      新建：" + ", ".join(dist["created"]))

    print("\n覆盖提醒：这批数据只有 GPT / Claude 两个家族。测其他家族时，")
    print("闭集归因仍会给出「最像的候选」，必须看报告里的「开集识别」与")
    print("「参考库覆盖」，不要直接读顶层百分比。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
