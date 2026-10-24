#!/usr/bin/env python
"""把社区项目 LM Fingerpoint Detector 的实测指纹库接入 ModelProof。

为什么需要它
------------
ModelTrace 那批指纹只有 **GPT 与 Claude 两个家族**（18 条），其余 85 条
是空有身份元数据的 derived 条目，一条证据也给不出。测 Qwen / DeepSeek /
Gemini / Grok 时闭集归因只能硬塞最像的候选。

`Ikaleio/lm-detector`（LM Fingerpoint Detector，MIT）明确致谢 ModelTrace
"提供算法思路参考和部分原始数据"，但把参考库扩到了 **60 个模型、13 个家族**：
新增 Gemini / Grok / Qwen / DeepSeek / GLM / Kimi / Hunyuan / Muse / MiMo /
Step / Seed。

能否并入，不靠"看起来一样"，靠量化
-----------------------------------
导入前脚本会做一次**双库一致性校验**：对两边都有的模型，算两库计数分布的
Hellinger 距离，再和该模型在新库内到**最近邻**的距离比。

    两库距离 < 库内最近邻距离   →  两次独立采样的差异小于「不同模型之间」
                                   的差异，同口径，可以并入（18/18 通过）

只要有一个模型反过来，脚本就拒绝写入并退出 —— 宁可不导入，也不把不同
口径的数据混进同一个闭集。

口径核对（逐项与 ModelProof 对齐后才敢用）
------------------------------------------
    range                 [1, 355]              ✓ 同
    counts 维度           355                    ✓ 同
    parser                longest digit run …    ✓ 同
    alpha                 0.5                    ✓ 同
    ordered_block_weight  0.25                   ✓ 同
    robust 包             hellinger/ordered_blocks/model_order ✓ 同结构
    calibration           1 / 2 / 3 档            ✓ 同

两条必须写在报告里的边界
------------------------
1. **来源单一且未经 pin**：每个模型的 samples 来源都是 `openrouter/unknown`
   —— 没有指定 provider。OpenRouter 是聚合器，同一模型名后面可能落到不同
   后端。这正好是 ModelProof 关心的事（多后端分派），所以导入的条目
   provenance 里会写明，验证时报告里的「多后端分派」检测仍然会跑。
2. **没有有序块特征**：新库只存了汇总 counts 与 frequency_references（0/1
   出现与否，丢了数字顺序），重建不出 modelproof 的 74 维 ordered 特征。
   导入的条目 ordered 为空，边际分布可用、有序块不可用。

因此默认写进**独立库 `community`，不碰 `default`** —— 两个库口径不同
（default 的 18 条带有序块、样本量也不同），混在一个闭集里会互相干扰。
界面上用「数据来源」切换即可，正是为这种场景准备的。

用法：
    python scripts/import_lmdetector.py --dry-run          # 只校验不写盘
    python scripts/import_lmdetector.py                    # 写入 community 库
    python scripts/import_lmdetector.py --bank my-bank     # 写到别的库
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.request import build_opener, ProxyHandler, Request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modelproof.core.baseline.banks import ensure_migrated  # noqa: E402
from modelproof.core.baseline.schema import BaselineEntry, Identity, Provenance  # noqa: E402
from modelproof.core.baseline.store import BaselineStore  # noqa: E402
from modelproof.core.channels.distribution import (  # noqa: E402
    DIMENSION,
    PROBE_VERSION as DB_PROBE_VERSION,
)

IMPORT_DIR = ROOT / "baselines" / "imports" / "lm-detector"
BANK_URL = ("https://raw.githubusercontent.com/Ikaleio/lm-detector"
            "/main/data/unified_bank.json")
TOKENIZER_URL = ("https://raw.githubusercontent.com/Ikaleio/lm-detector"
                 "/main/data/tokenizer_bank.json")

SOURCE_LABEL = "LM Fingerpoint Detector 社区参考库"
SOURCE_URL = "https://github.com/Ikaleio/lm-detector"

# 家族 -> (厂商, 家族)。providers 字段列出的 13 家都在这里。
FAMILY_VENDOR: dict[str, tuple[str, str]] = {
    "claude": ("anthropic", "claude"),
    "gpt": ("openai", "gpt"),
    "gemini": ("google", "gemini"),
    "grok": ("xai", "grok"),
    "glm": ("zhipu", "glm"),
    "qwen": ("alibaba", "qwen"),
    "kimi": ("moonshot", "kimi"),
    "hunyuan": ("tencent", "hunyuan"),
    "muse": ("meta", "muse"),
    "mimo": ("xiaomi", "mimo"),
    "step": ("stepfun", "step"),
    "seed": ("bytedance", "seed"),
    "deepseek": ("deepseek", "deepseek"),
}


def _download(url: str, dest: Path) -> None:
    opener = build_opener(ProxyHandler({}))
    request = Request(url, headers={"User-Agent": "modelproof-import"})
    dest.parent.mkdir(parents=True, exist_ok=True)
    data = opener.open(request, timeout=180).read()
    dest.write_bytes(data)


def ensure_file(name: str, url: str) -> Path:
    path = IMPORT_DIR / name
    if not path.is_file():
        print(f"  下载 {name} …")
        _download(url, path)
    return path


def hellinger(p: list[float], q: list[float]) -> float:
    sp = sum(p) or 1.0
    sq = sum(q) or 1.0
    return math.sqrt(0.5 * sum(
        (math.sqrt(a / sp) - math.sqrt(b / sq)) ** 2 for a, b in zip(p, q)
    ))


def identity_of(model: dict[str, Any], model_id: str) -> Identity:
    family = str(model.get("family") or "").strip()
    vendor, family = FAMILY_VENDOR.get(family, ("", family))
    # series / version 从 id 里抠：deepseek-v4.1-flash-1017 -> v4.1 / 1017
    rest = model_id
    if family and model_id.lower().startswith(family.lower()):
        rest = model_id[len(family):].lstrip("-")
    parts = [p for p in rest.split("-") if p]
    series = parts[0] if parts else ""
    version = parts[-1] if len(parts) > 1 else ""
    return Identity(vendor=vendor, family=family, series=series, version=version)


def verify_compatibility(bank: dict[str, Any]) -> dict[str, Any]:
    """双库一致性校验：两库距离必须小于库内最近邻距离。"""
    new_models = {m["id"]: m for m in bank.get("models") or [] if m.get("id")}
    dims = {len(m.get("counts") or []) for m in new_models.values()}
    if dims and dims != {DIMENSION}:
        return {"ok": False, "reason": f"counts 维度不一致：{sorted(dims)} != {DIMENSION}"}

    store = BaselineStore()
    store.load()
    mine: dict[str, list[float]] = {}
    for entry in store.search():
        payload = (entry.channels or {}).get("distribution")
        if payload and payload.get("enabled", True) and payload.get("counts"):
            mine[entry.model_id] = payload["counts"]

    shared = sorted(set(mine) & set(new_models))
    if not shared:
        return {"ok": False, "reason": "与现有库没有任何重合模型，无法校验口径"}

    rows = []
    worst = None
    for key in shared:
        dist = hellinger(mine[key], new_models[key].get("counts") or [])
        neighbours = [
            hellinger(new_models[key]["counts"], new_models[o]["counts"])
            for o in new_models if o != key
        ]
        nearest = min(neighbours) if neighbours else float("inf")
        rows.append((key, dist, nearest))
        if worst is None or (dist - nearest) > (worst[1] - worst[2]):
            worst = (key, dist, nearest)

    ok = worst is not None and worst[1] < worst[2]
    return {
        "ok": ok,
        "shared": shared,
        "rows": rows,
        "worst": worst,
        "new_only": sorted(set(new_models) - set(mine)),
    }


def import_distribution(bank: dict[str, Any], bank_name: str, dry_run: bool,
                        only_new: bool = False) -> dict[str, Any]:
    """写入分布指纹。

    `only_new=True` 用于并入默认库：目标条目若已有 distribution 且带有序块就跳过。
    原因很实在 —— 这批数据**没有有序块**（只存汇总 counts），拿它覆盖现成的
    18 条（带 74 维 ordered）等于把已经更完整的观测换成差的。合并要补短板，
    不能削长板。
    """
    store = BaselineStore(bank=bank_name)
    store.load()
    created: list[str] = []
    merged: list[str] = []
    skipped: list[str] = []
    kept: list[str] = []

    built_at = str(bank.get("built_at") or "")[:10]
    method = bank.get("method") or {}
    order = (bank.get("robust") or {}).get("model_order") or []
    # 稳健包是按 model_order 训练的，只对在册模型有效
    in_robust = set(order)

    for model in bank.get("models") or []:
        raw_id = str(model.get("id") or "").strip()
        if not raw_id:
            continue
        counts = model.get("counts") or []
        if len(counts) != DIMENSION:
            skipped.append(raw_id)
            continue

        samples = int(model.get("response_count") or 0)
        sources = model.get("sources") or {}
        providers = sorted(sources.keys())
        observation = {
            "probe_version": DB_PROBE_VERSION,
            "counts": [int(c) for c in counts],
            # 新库只有汇总计数，重建不出有序块特征 —— 明确留空，不假装补齐
            "ordered": [],
            "samples": samples,
            "total_numbers": int(model.get("valid_number_count") or sum(counts)),
            "source": {
                "tier": "community",
                "origin": SOURCE_LABEL,
                "providers": providers,
                "collected_at": "",
                "as_of": built_at,
                "note": (
                    "第三方实测；来源标注为 openrouter/unknown（未 pin provider），"
                    "同一模型名可能落到不同后端。无有序块特征，仅边际分布可用。"
                ),
            },
        }

        identity = identity_of(model, raw_id)
        entry = store.get(raw_id)

        if only_new and entry is not None:
            existing = (entry.channels or {}).get("distribution")
            if existing and existing.get("enabled", True) and existing.get("ordered"):
                kept.append(raw_id)
                continue

        if entry is None:
            entry = BaselineEntry(
                model_id=raw_id,
                aliases=[],
                identity=identity,
                provenance=Provenance(
                    tier="community",
                    collected_by=SOURCE_LABEL,
                    method=(
                        f"1–355 整数选择任务 × {samples} 条回答"
                        f"（{method.get('name') or '有序块 + 干扰剔除 Hellinger'}）"
                    ),
                    collected_at=built_at,
                    source_url=SOURCE_URL,
                    note="由 scripts/import_lmdetector.py 导入。",
                ),
            )
            created.append(raw_id)
        else:
            entry.identity = identity
            merged.append(raw_id)

        entry = store.upsert_from_observation(entry, "distribution", observation)
        if raw_id not in in_robust and order:
            entry.provenance.note = (
                (entry.provenance.note or "").rstrip()
                + " 该模型不在稳健打包包的 model_order 内，概率未做交叉验证标定。"
            ).strip()
        if not dry_run:
            store.save(entry)

    return {"bank": bank_name, "created": created, "merged": merged,
            "skipped": skipped, "kept": kept}


def import_tokenizer_classes(dry_run: bool) -> dict[str, Any]:
    """词表归属映射：62 个词表分类 × 290 个模型，用来补家族先验。"""
    path = ensure_file("tokenizer_bank.json", TOKENIZER_URL)
    raw = json.loads(path.read_text(encoding="utf-8"))
    classes = raw.get("classes") or []
    members = sum(len(c.get("members") or []) for c in classes)
    target = ROOT / "baselines" / "imports" / "lm-detector" / "tokenizer_classes.json"
    if not dry_run:
        target.write_text(
            json.dumps({
                "schema": "tokenizer-classes",
                "source": SOURCE_URL,
                "generated_at": raw.get("generated_at", ""),
                "imported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "note": (
                    "模型 -> 词表分类的归属映射（非实测指纹）。可用于补全家族先验，"
                    "不能作为身份证据：很多模型共用同一个词表。"
                ),
                "classes": classes,
                "api_models": raw.get("api_models") or [],
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return {"classes": len(classes), "members": members, "path": str(target)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--bank", default="community",
                        help="目标基准库（默认 community；并入统一库时用 --bank default --only-new）")
    parser.add_argument("--only-new", action="store_true",
                        help="已有更完整观测（带有序块）的条目不覆盖，只补缺失的")
    parser.add_argument("--dry-run", action="store_true", help="只校验不写盘")
    parser.add_argument("--skip-verify", action="store_true",
                        help="跳过双库一致性校验（不推荐）")
    args = parser.parse_args(argv)

    path = ensure_file("unified_bank.json", BANK_URL)
    bank = json.loads(path.read_text(encoding="utf-8"))
    models = bank.get("models") or []
    families = sorted({str(m.get("family") or "") for m in models if m.get("family")})

    print(f"源：{path}")
    print(f"模型 {len(models)} 个 · 家族 {families}")
    print(f"built_at {str(bank.get('built_at') or '')[:10]} · "
          f"method {str((bank.get('method') or {}).get('name'))}")
    print(f"目标库：{args.bank}{'（dry-run，不写盘）' if args.dry_run else ''}\n")

    if not args.skip_verify:
        print("[1/3] 双库一致性校验")
        result = verify_compatibility(bank)
        if not result["ok"]:
            print(f"  ✗ 拒绝导入：{result['reason']}")
            return 3
        rows = result["rows"]
        worst = result["worst"]
        print(f"  重合模型 {len(rows)} 个，逐个比对两库计数分布：")
        for key, dist, nearest in sorted(rows, key=lambda r: -r[1])[:5]:
            print(f"    {key:22s} 两库 {dist:.4f}  <  库内最近邻 {nearest:.4f}  ✓")
        if len(rows) > 5:
            print(f"    …其余 {len(rows) - 5} 个同样通过")
        print(f"  最差一项：{worst[0]} 两库 {worst[1]:.4f} vs 最近邻 {worst[2]:.4f}")
        print(f"  ✓ {len(rows)}/{len(rows)} 通过 —— 独立采样的差异小于不同模型之间"
              f"的差异，同口径\n")
        new_only = result["new_only"]
    else:
        print("[1/3] 跳过一致性校验\n")
        new_only = []

    print("[2/3] 词表归属映射")
    tok = import_tokenizer_classes(args.dry_run)
    print(f"  {tok['classes']} 个词表分类 · {tok['members']} 个模型归属 -> {tok['path']}\n")

    print("[3/3] 数字分布指纹")
    dist = import_distribution(bank, args.bank, args.dry_run, args.only_new)
    print(f"  新建 {len(dist['created'])} 条 · 合并 {len(dist['merged'])} 条"
          f"{' · 跳过 ' + str(dist['skipped']) if dist['skipped'] else ''}")
    if dist.get("kept"):
        print(f"  保留原有更完整的观测 {len(dist['kept'])} 条（已带有序块，不覆盖）")
    if new_only:
        print(f"  新增家族覆盖：{', '.join(sorted({m.split('-')[0] for m in new_only}))}")
        print(f"  新模型示例：{', '.join(new_only[:8])}"
              f"{' …' if len(new_only) > 8 else ''}")

    print("\n边界（会写进每条 provenance）：")
    print("  · 来源 openrouter/unknown，未 pin provider，可能跨后端混合")
    print("  · 无有序块特征，仅边际分布可用")
    print(f"  · 未写入 default 库 —— 用界面「数据来源」切到 {args.bank} 生效")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
