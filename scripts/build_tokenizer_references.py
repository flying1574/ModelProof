#!/usr/bin/env python
"""扩充本地词表参考库（C1 通道的对照组）。

为什么要有这个脚本：C1 是全部通道里**抗蒸馏最强**的一条 —— 蒸馏搬得走
答案分布、说话风格、行为口径，搬不走词表。但前提是库里得有那个词表。

2026-10-18 的教训：一个疑似 Qwen 的端点，最接近的参考是 qwen2-5，
相对差 24.2%（远超 6% 的近邻容差），第一反应是「缺 Qwen 3 的词表」。
查下来不是：Qwen 3 与 2.5 的词表**完全相同**（config 里都是 151,936），
补进来也一格不差。真正的原因是那批 37 格探针被分派到了多个后端 ——
每一格都能被某个词表解释，但近的不是同一个（详见 analyze_mixture）。
**补参考能解决「库里没有」，解决不了「请求不在同一个后端上」。**

依赖说明（重要）：
  * 本脚本需要 `tokenizers`（HF 官方 Rust 实现）与 `tiktoken`，
    它们**只在生成参考时用**，不进 core —— ModelProof 的内核保持零第三方依赖。
  * 生成结果是纯 JSON，运行时只读 JSON。
  * 联网只访问 huggingface.co 公开仓库；被门控（gated）的仓库会失败并跳过。

用法：
    python scripts/build_tokenizer_references.py            # 只验证已有参考
    python scripts/build_tokenizer_references.py --fetch    # 联网补齐
    python scripts/build_tokenizer_references.py --fetch --only qwen3,glm4.6
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modelproof.core.channels.tokenizer import (  # noqa: E402
    PROBE_VERSION,
    PROBES,
    REFERENCE_PATH,
)

CACHE_DIR = ROOT / "baselines" / "fingerprints" / "tokenizers"


# 词表清单。repo 可能取不到（门控 / 改名 / 网络），失败就跳过，不硬撑。
# 挑选原则：覆盖「用户真的会去测」的家族，且同一家族内只收**词表不同**的代。
SOURCES: list[dict] = [
    # --- GPT 系：走 tiktoken，无需联网 ---
    {"kind": "tiktoken", "id": "gpt-o200k", "label": "GPT o200k",
     "family": "gpt", "covers": "GPT-4o / o 系列 / GPT-5 / GPT-6 家族", "name": "o200k_base"},
    {"kind": "tiktoken", "id": "gpt-oss-harmony", "label": "GPT-oss (harmony)",
     "family": "gpt", "covers": "gpt-oss 系列", "name": "o200k_harmony"},
    {"kind": "tiktoken", "id": "gpt-cl100k", "label": "GPT cl100k",
     "family": "gpt", "covers": "GPT-3.5 / GPT-4", "name": "cl100k_base"},

    # --- Qwen：2.5 与 3 是两代不同词表，都要有 ---
    {"kind": "hf", "id": "qwen2-5", "label": "Qwen 2.5",
     "family": "qwen", "covers": "Qwen 2.5（151,643 词表）",
     "repo": "Qwen/Qwen2.5-0.5B-Instruct", "file": "tokenizer.json"},
    # 实测：Qwen 3 与 2.5 的 vocab_size 都是 151,936，词表完全同构 ——
    # 留着这条是为了「库里确实查过」这件事有据可查，不是为了多一个区分维度。
    {"kind": "hf", "id": "qwen3", "label": "Qwen 3 / 3.5+",
     "family": "qwen", "covers": "Qwen 3 及之后（151,936，与 2.5 同一套词表）",
     "repo": "Qwen/Qwen3-8B", "file": "tokenizer.json"},

    # --- DeepSeek ---
    {"kind": "hf", "id": "deepseek-v3", "label": "DeepSeek V3 / V4",
     "family": "deepseek", "covers": "DeepSeek V3、V4（约 129,000 词表）",
     "repo": "deepseek-ai/DeepSeek-V3", "file": "tokenizer.json"},

    # --- GLM / 智谱 ---
    # 注意用 -hf 后缀那个仓库：THUDM/glm-4-9b-chat 只有 sentencepiece 的
    # tokenizer.model（还要额外依赖），-hf 才有标准 tokenizer.json。
    {"kind": "hf", "id": "glm4", "label": "GLM-4 / 4.5 / 4.6",
     "family": "glm", "covers": "智谱 GLM-4 系列（约 151,000 词表）",
     "repo": "THUDM/glm-4-9b-chat-hf", "file": "tokenizer.json"},

    # --- Kimi / 月之暗面 ---
    # 官方仓库没有 tokenizer.json，只有 tiktoken.model（base64 token + rank）。
    # 分词正则不在 config 里，得从仓库的 tokenization_kimi.py 抄过来 ——
    # 抄在下面，省得每次生成都要再去解析一遍源码。
    {"kind": "tiktoken_model", "id": "kimi-k2", "label": "Kimi K2 / K3",
     "family": "kimi", "covers": "Moonshot Kimi K2、K3（163,840 词表）",
     "repo": "moonshotai/Kimi-K2-Instruct", "file": "tiktoken.model",
     "pat_str": "|".join([
         r"[\p{Han}]+",
         r"[^\r\n\p{L}\p{N}]?[\p{Lu}\p{Lt}\p{Lm}\p{Lo}\p{M}&&[^\p{Han}]]*[\p{Ll}\p{Lm}\p{Lo}\p{M}&&[^\p{Han}]]+(?i:'s|'t|'re|'ve|'m|'ll|'d)?",
         r"[^\r\n\p{L}\p{N}]?[\p{Lu}\p{Lt}\p{Lm}\p{Lo}\p{M}&&[^\p{Han}]]+[\p{Ll}\p{Lm}\p{Lo}\p{M}&&[^\p{Han}]]*(?i:'s|'t|'re|'ve|'m|'ll|'d)?",
         r"\p{N}{1,3}",
         r" ?[^\s\p{L}\p{N}]+[\r\n]*",
         r"\s*[\r\n]+",
         r"\s+(?!\S)",
         r"\s+",
     ])},

    # --- MiniMax ---
    {"kind": "hf", "id": "minimax-m2", "label": "MiniMax M2",
     "family": "minimax", "covers": "MiniMax M2 全系列（200,000 词表）",
     "repo": "MiniMaxAI/MiniMax-M2", "file": "tokenizer.json"},

    # --- Llama（官方仓库有门控，用社区镜像） ---
    {"kind": "hf", "id": "llama3", "label": "Llama 3 / 3.1 / 4",
     "family": "llama", "covers": "Meta Llama 3、3.1、4（128,256 词表）",
     "repo": "unsloth/Meta-Llama-3.1-8B-Instruct", "file": "tokenizer.json"},

    # --- Mistral ---
    {"kind": "hf", "id": "mistral-v3", "label": "Mistral v0.3 / Small 3",
     "family": "mistral", "covers": "Mistral 7B v0.3、Mistral Small 3（131,072 词表）",
     "repo": "mistralai/Mistral-7B-Instruct-v0.3", "file": "tokenizer.json"},

    # --- Gemma / Gemini 同宗 ---
    # google/* 是门控仓库（要同意条款才能下），用 unsloth 的镜像拿同一个词表。
    {"kind": "hf", "id": "gemma3", "label": "Gemma 3 / Gemini",
     "family": "gemini", "covers": "Google Gemma 3；Gemini 与其共享词表（262,144）",
     "repo": "unsloth/gemma-3-4b-it", "file": "tokenizer.json"},
]


def _counter(source: dict, fetch: bool):
    """返回一个 count(text) -> int，取不到就返回 None。"""
    if source["kind"] == "tiktoken":
        try:
            import tiktoken
        except ImportError:
            print("  跳过：未安装 tiktoken（pip install tiktoken）")
            return None
        try:
            encoding = tiktoken.get_encoding(source["name"])
        except Exception as error:
            print(f"  跳过 {source['id']}：{error}")
            return None
        return lambda text: len(encoding.encode(text, disallowed_special=()))

    if source["kind"] == "tiktoken_model":
        # 官方只发 tiktoken.model：每行是「base64 编码的 token 空格 rank」。
        # 用 tiktoken.Encoding 直接拼一个出来，pat_str 由清单给出。
        try:
            import tiktoken
        except ImportError:
            print("  跳过：未安装 tiktoken（pip install tiktoken）")
            return None
        target = CACHE_DIR / f"{source['id']}.model"
        if not target.is_file():
            if not fetch:
                print(f"  跳过 {source['id']}：本地无缓存（加 --fetch 下载）")
                return None
            if not _download(source, target):
                return None
        try:
            import base64

            ranks: dict[bytes, int] = {}
            for line in target.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                token_b64, _, rank = line.rpartition(" ")
                ranks[base64.b64decode(token_b64)] = int(rank)
            encoding = tiktoken.Encoding(
                source["id"], pat_str=source["pat_str"],
                mergeable_ranks=ranks, special_tokens={},
            )
        except Exception as error:
            print(f"  跳过 {source['id']}：{error}")
            return None
        return lambda text: len(encoding.encode(text, disallowed_special=()))

    # HF tokenizer.json
    try:
        from tokenizers import Tokenizer
    except ImportError:
        print("  跳过：未安装 tokenizers（pip install tokenizers）")
        return None

    target = CACHE_DIR / f"{source['id']}.json"
    if not target.is_file():
        if not fetch:
            print(f"  跳过 {source['id']}：本地无缓存（加 --fetch 下载）")
            return None
        if not _download(source, target):
            return None
    try:
        tokenizer = Tokenizer.from_file(str(target))
    except Exception as error:
        print(f"  跳过 {source['id']}：词表无法加载 {error}")
        return None
    return lambda text: len(tokenizer.encode(text).ids)


def _download(source: dict, target: Path) -> bool:
    url = f"https://huggingface.co/{source['repo']}/resolve/main/{source['file']}"
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with urllib.request.urlopen(url, timeout=180) as response:
            data = response.read()
    except Exception as error:
        print(f"  下载失败 {source['repo']}：{error}（仓库可能已门控或改名）")
        return False
    if len(data) < 10_000:
        print(f"  内容过小（{len(data)} 字节），可能仓库受限：{source['repo']}")
        return False
    target.write_bytes(data)
    print(f"  已下载 {source['repo']} → {len(data) / 1024 / 1024:.1f} MB")
    return True


def build(source: dict, fetch: bool) -> dict | None:
    counter = _counter(source, fetch)
    if counter is None:
        return None
    counts: dict[str, int] = {}
    for probe in PROBES:
        try:
            counts[probe["id"]] = int(counter(probe["text"]))
        except Exception as error:
            print(f"  {source['id']} 在 {probe['id']} 上失败：{error}")
            return None
    return {
        "id": source["id"],
        "label": source["label"],
        "family": source["family"],
        "covers": source.get("covers", ""),
        "source": f"local:{source['kind']}/"
                  f"{source.get('name') or source.get('repo')}",
        "probe_version": PROBE_VERSION,
        "overhead": 0,
        "counts": counts,
        "captured_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def validate(existing: list[dict], built: dict) -> str:
    """与已有参考逐格比对。已存在的条目必须**完全一致**，否则说明生成路径变了。"""
    for item in existing:
        if item.get("id") != built["id"]:
            continue
        left = item.get("counts") or {}
        diffs = {
            key: (left.get(key), built["counts"].get(key))
            for key in built["counts"]
            if left.get(key) != built["counts"].get(key)
        }
        if not diffs:
            return "一致"
        return f"不一致 {len(diffs)} 格：{list(diffs.items())[:3]}"
    return "新增"


def pairwise(references: list[dict]) -> list[tuple[str, str, float]]:
    """列出互相靠得太近的参考对 —— 它们会互相混淆，必须知道。"""
    from modelproof.core.channels.tokenizer import compare_counts

    collisions: list[tuple[str, str, float]] = []
    for i in range(len(references)):
        for j in range(i + 1, len(references)):
            diff, _ = compare_counts(
                references[i]["counts"], references[j]
            )
            if diff is not None and diff < 0.06:
                collisions.append((references[i]["id"], references[j]["id"], diff))
    return sorted(collisions, key=lambda row: row[2])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--fetch", action="store_true", help="允许联网下载词表")
    parser.add_argument("--only", default="", help="只处理这些 id（逗号分隔）")
    args = parser.parse_args(argv)

    wanted = {s.strip() for s in args.only.split(",") if s.strip()}
    sources = [s for s in SOURCES if not wanted or s["id"] in wanted]

    existing: list[dict] = []
    if REFERENCE_PATH.is_file():
        payload = json.loads(REFERENCE_PATH.read_text(encoding="utf-8"))
        existing = list(payload.get("references") or [])
    print(f"现有参考 {len(existing)} 条，目标清单 {len(sources)} 条\n")

    by_id = {item["id"]: item for item in existing}
    added = 0
    for source in sources:
        print(f"[{source['id']}] {source['label']}")
        result = build(source, args.fetch)
        if result is None:
            continue
        status = validate(existing, result)
        print(f"    37 格合计 {sum(result['counts'].values())} token · {status}")
        if status == "一致":
            continue
        by_id[source["id"]] = result
        added += 1

    merged = [by_id[key] for key in sorted(by_id)]
    payload = {
        "schema": "tokenizer-references",
        "probe_version": PROBE_VERSION,
        "cells": len(PROBES),
        "imported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "note": (
            "本地词表实测参考（tiktoken / HF tokenizer 直接数出来的 token 数），"
            "不是端点观测。命中即锁定词表，进而锁定家族；无法区分同词表的近亲版本。"
            "由 scripts/build_tokenizer_references.py 生成，可重复生成。"
        ),
        "references": merged,
    }
    REFERENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
    REFERENCE_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n已写入 {REFERENCE_PATH.name}：{len(merged)} 条参考（新增/更新 {added}）")

    collisions = pairwise(merged)
    if collisions:
        print("\n互相分不开的参考对（相对差 <6%，会混淆，属已知边界）：")
        for left, right, diff in collisions:
            print(f"    {left} ≈ {right}  相对差 {diff:.4f}")
    else:
        print("\n参考之间全部能分开（最近的一对相对差 ≥6%）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
