"""C1 —— 协议层词表指纹。

原理：不看模型「说了什么」，只看它把一段固定文本「数成了多少个 token」。
词表在训练时固定，改词表等于重训，所以系统提示词、temperature、量化、
LoRA、甚至在外层套一个壳都改不了它。这是全部通道里抗干扰最强的一条。

边界：同一个家族的不同版本通常共用词表，所以这条通道**只能证明到
family 层**。想定版本要换 distribution / behavior / capability。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..types import Candidate, CostRange, Evidence, TargetLayer
from .base import BaseChannel, RunContext

# 探针串变动时必须改版本号，否则新旧向量不可比。
PROBE_VERSION = "tk1-37"

# 判定阈值（相对差）。
EXACT_TOLERANCE = 0.005
NEAR_TOLERANCE = 0.06

# 单 token 基线：用它测出「请求本身的固定开销」，再减掉得到边际成本。
BASELINE_TEXT = "a"
# "a" 在几乎所有 BPE 词表里都是 1 个 token。开销里含着它，减的时候要留出来。
BASELINE_TOKENS = 1

PROBES: list[dict[str, str]] = [
    {"id": "en_pangram", "label": "英文全字母句", "text": "The quick brown fox jumps over the lazy dog."},
    {"id": "en_sentence", "label": "英文长句", "text": "Distributed systems fail in ways that are hard to reproduce locally."},
    {"id": "en_lowercase_run", "label": "小写连续串", "text": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
    {"id": "digits_run", "label": "连续数字", "text": "123456789012345678901234567890"},
    {"id": "digits_spaced", "label": "空格分隔数字", "text": "1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18"},
    {"id": "long_float", "label": "长小数", "text": "3.14159265358979323846264338327950288"},
    {"id": "zh_common", "label": "中文常用句", "text": "今天的天气很好，我们一起去公园散步吧。"},
    {"id": "zh_tech", "label": "中文技术句", "text": "分布式系统的一致性协议需要在可用性与延迟之间做权衡。"},
    {"id": "zh_rare", "label": "生僻汉字", "text": "靐齉爩龘虋鱻麤淼掱燚"},
    {"id": "zh_idiom", "label": "成语连排", "text": "画蛇添足守株待兔刻舟求剑掩耳盗铃"},
    {"id": "ja_mixed", "label": "日文混排", "text": "日本語のひらがなとカタカナと漢字が混ざっています。"},
    {"id": "ko_hangul", "label": "韩文", "text": "한국어 문장은 띄어쓰기로 단어를 구분합니다."},
    # 韩文组合字母（U+11xx 初声 / U+116x 中声 / U+11Ax 终声），与预组合音节
    # 的切分方式不同，是区分韩文友好词表的关键探针。
    {"id": "ko_jamo", "label": "韩文组合字母", "text": "\u1112\u1161\u11ab\u1100\u1173\u11af \u110c\u1161\u1106\u1169 \u1107\u116e\u1105\u1175"},
    {"id": "ar_arabic", "label": "阿拉伯文", "text": "اللغة العربية تكتب من اليمين إلى اليسار."},
    {"id": "he_hebrew", "label": "希伯来文", "text": "השפה העברית נכתבת מימין לשמאל."},
    {"id": "th_thai", "label": "泰文", "text": "ภาษาไทยไม่มีการเว้นวรรคระหว่างคำ"},
    {"id": "ru_cyrillic", "label": "西里尔文", "text": "Современные языковые модели обучаются на больших корпусах."},
    {"id": "hi_devanagari", "label": "天城文", "text": "यह एक हिन्दी वाक्य है जिसमें संयुक्त अक्षर हैं।"},
    {"id": "el_greek", "label": "希腊文", "text": "Η ελληνική γλώσσα έχει μακρά ιστορία."},
    {"id": "emoji_skin", "label": "肤色 emoji", "text": "👋 👋🏻 👋🏼 👋🏽 👋🏾 👋🏿"},
    {"id": "emoji_zwj_family", "label": "ZWJ 家庭 emoji", "text": "👨‍👩‍👧‍👦 👩‍👩‍👦 👨‍👨‍👧"},
    {"id": "emoji_flags", "label": "国旗 emoji", "text": "🇨🇳 🇺🇸 🇯🇵 🇩🇪 🇪🇺 🇧🇷"},
    {"id": "emoji_profession", "label": "职业 emoji", "text": "👩‍⚕️ 👨‍🍳 👩‍🚀 👨‍🏫"},
    {"id": "combining", "label": "组合附加符", "text": "á̂̃ é̂̃ í̂̃ ó̂̃ ú̂̃"},
    {"id": "astral_math", "label": "星平面字符", "text": "𝕳𝖊𝖑𝖑𝖔 𝖂𝖔𝖗𝖑𝖉 𝕯𝖔𝖚𝖇𝖑𝖊"},
    {"id": "spaces", "label": "空白字符", "text": "a b\tc d e​f"},
    {"id": "newlines", "label": "连续换行", "text": "line1\n\n\n\nline2\n\n\n\nline3"},
    {"id": "indent4", "label": "四空格缩进", "text": "def f():\n    if x:\n        return [\n            1,\n            2,\n        ]"},
    {"id": "indent2", "label": "两空格缩进", "text": "def f():\n  if x:\n    return [\n      1,\n    ]"},
    {"id": "code_python", "label": "Python 片段", "text": "result = [transform(item) for item in payload if item.is_valid()]"},
    {"id": "code_json", "label": "JSON 片段", "text": '{"model":"x","messages":[{"role":"user","content":"hi"}],"temperature":0.7}'},
    {"id": "base64", "label": "Base64 串", "text": "VGhpcyBpcyBhIGJhc2U2NCBlbmNvZGVkIHN0cmluZyBmb3IgdGVzdGluZy4="},
    {"id": "url", "label": "URL", "text": "https://api.example.com/v1/chat/completions?stream=true&temperature=0.7"},
    {"id": "camel_case", "label": "驼峰标识符", "text": "getHTTPResponseCodeFromServerAndParseJSONBody"},
    {"id": "snake_case", "label": "下划线标识符", "text": "get_http_response_code_from_server_and_parse_json_body"},
    {"id": "repeated_word", "label": "单词重复", "text": "buffalo buffalo buffalo buffalo buffalo buffalo buffalo buffalo"},
    {"id": "mixed_scripts", "label": "多文种混排", "text": "Hello 世界 안녕 مرحبا Привет Γειά"},
]

PROBES_BY_ID = {p["id"]: p for p in PROBES}

# 本地词表参考库：直接用 tiktoken / HF tokenizer 数出来的 token 数，
# 不是端点观测。它的价值在于「词表到底是哪一家的」这个问题有确定答案 ——
# 蒸馏搬得走答案分布和说话风格，搬不走词表。
REFERENCE_PATH = Path(__file__).resolve().parents[3] / "baselines" / "fingerprints" / "tokenizer_references.json"

_reference_cache: list[dict[str, Any]] | None = None


def load_references(path: str | Path | None = None) -> list[dict[str, Any]]:
    """读取本地词表参考库。没有就返回空列表——不能靠猜。"""
    global _reference_cache
    target = Path(path) if path else REFERENCE_PATH
    if path is None and _reference_cache is not None:
        return _reference_cache
    if not target.is_file():
        return []
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return []
    if payload.get("probe_version") != PROBE_VERSION:
        # 探针格变了就不能比：宁可不用，也不能拿错位的向量骗出高置信度。
        return []
    references = list(payload.get("references") or [])
    if path is None:
        _reference_cache = references
    return references


def rank_vocabularies(
    counts: dict[str, int | None]
) -> list[tuple[dict[str, Any], float, int]]:
    """与本地词表参考库比对，返回 [(参考条目, 相对差, 相同格数)]。"""
    results: list[tuple[dict[str, Any], float, int]] = []
    for reference in load_references():
        diff, same = compare_counts(counts, reference)
        if diff is None:
            continue
        results.append((reference, diff, same))
    results.sort(key=lambda row: row[1])
    return results


def analyze_mixture(
    counts: dict[str, int | None], tolerance: int = 2
) -> dict[str, Any] | None:
    """判断这批探针是不是命中了**多个不同词表**。

    为什么需要它：中转商会把请求分派到不同后端。这时 37 格向量是
    「混着来的」，跟任何一个单一词表都对不上，整体相对差会很大，
    看上去就像「库里没有这个词表」—— 于是通道弃权，人只能干瞪眼。

    判据必须**两条同时成立**，只靠逐格投票会误报：不同词表在很多格上数值
    本来就接近，纯 Qwen 自己的向量逐格投票也会散成
    qwen 15 / deepseek 9 / gpt 9 —— 但它到 Qwen 参考的整体相对差是 0.0000。

      1. **没有任何单一词表能解释整体**（整体相对差 > NEAR_TOLERANCE）；
      2. **但每一格都能被某个词表解释**（与最近参考的偏差 ≤ tolerance）。

    1 说明「不是库里已有的某个词表」，2 说明「也不是全新词表」——
    合起来只能是「这些格子来自不同的词表」。

    2026-10-18 那个端点正是如此：整体最近参考 0.222（远超 6% 容差），
    但 36 格里有 36 格都能在某个已知词表的 ±2 之内找到解释，
    投票散成 gpt 14 / deepseek 13 / qwen 4 / minimax 3 / mistral 2。
    """
    references = load_references()
    if not references:
        return None

    # 前提 1：没有任何单一词表能解释整体向量
    best_reference, best_diff, _ = (rank_vocabularies(counts) or [(None, 1.0, 0)])[0]

    votes: dict[str, int] = {}
    per_cell: list[dict[str, Any]] = []
    unresolved = 0
    considered = 0

    for key, observed in counts.items():
        if observed is None:
            continue
        considered += 1
        best = min(
            references,
            key=lambda r: abs(int(r["counts"].get(key) or 0) - int(observed)),
        )
        gap = abs(int(best["counts"].get(key) or 0) - int(observed))
        family = best.get("family") or "?"
        votes[family] = votes.get(family, 0) + 1
        if gap > tolerance:
            unresolved += 1
        per_cell.append(
            {"cell": key, "observed": observed, "nearest": best.get("id"),
             "family": family, "gap": gap}
        )

    if considered < 8:
        return None

    top_family, top_count = max(votes.items(), key=lambda kv: kv[1])
    top_share = top_count / considered
    others = sum(1 for f, n in votes.items() if f != top_family and n >= considered * 0.15)

    # 判混合：没有单一词表解释得了整体，且绝大多数格子都能被某个词表解释
    mixed = (
        best_diff > NEAR_TOLERANCE
        and top_share < 0.70
        and others >= 1
        and unresolved <= considered * 0.30
    )
    return {
        "considered": considered,
        "best_diff": round(best_diff, 6),
        "best_reference": (best_reference or {}).get("id"),
        "votes": dict(sorted(votes.items(), key=lambda kv: -kv[1])),
        "top_family": top_family,
        "top_share": round(top_share, 3),
        "unresolved": unresolved,
        "tolerance": tolerance,
        "mixed": mixed,
        "per_cell": sorted(per_cell, key=lambda row: -row["gap"])[:12],
    }


# --------------------------------------------------------------------------
# 家族先验（公开调研，仅作提示，不参与判定）
# --------------------------------------------------------------------------

FAMILY_PRIORS: dict[str, dict[str, str]] = {
    "gpt": {
        "label": "GPT",
        "vocab": "cl100k 100,256（GPT-3.5/4）／o200k 约 200,000（GPT-4o 起）",
        "note": "东亚文字占比低，中韩日文本会被切得很碎。",
    },
    "claude": {
        "label": "Claude",
        "vocab": "未公开，社区重建约 49,152（≤4.6）／约 16,384（4.7+）",
        "note": "词表未公开，需实测采集，不能用本地词表推算。",
    },
    "gemini": {
        "label": "Gemini / Gemma",
        "vocab": "SentencePiece 262,144",
        "note": "词表最大的一家，多语言效率好；与 Gemma 共享 tokenizer。",
    },
    "qwen": {
        "label": "Qwen",
        "vocab": "151,643（Qwen 2.5/3）／248,077（Qwen 3.5+）",
        "note": "东亚占比高，中文切分明显比 GPT 省。",
    },
    "deepseek": {
        "label": "DeepSeek",
        "vocab": "129,280（V3/V4）",
        "note": "东亚占比约 29%，代码与数学符号有专门扩充。",
    },
    "kimi": {
        "label": "Kimi",
        "vocab": "163,840（K2/K3，tiktoken 格式）",
        "note": "东亚占比最高，欧洲语言占比反而最低。",
    },
    "glm": {
        "label": "GLM",
        "vocab": "151,329（GLM-4）",
        "note": "中文优化明显。",
    },
    "llama": {
        "label": "Llama",
        "vocab": "128,256（Llama 3/4）",
        "note": "多语言一般，东亚文本偏费。",
    },
    "mistral": {
        "label": "Mistral",
        "vocab": "131,072（Mistral Large / Small）",
        "note": "欧洲语言效率高。",
    },
    "grok": {
        "label": "Grok",
        "vocab": "131,072",
        "note": "与 GPT 系接近但不相同。",
    },
    "minimax": {
        "label": "MiniMax",
        "vocab": "未公开",
        "note": "需实测采集。",
    },
}


class TokenizerChannel(BaseChannel):
    id = "tokenizer"
    yields = {"vendor", "family"}
    reliability = 1.00  # 硬信号；由标定脚本复核

    # -- 采集 -------------------------------------------------------------

    def collect(self, ctx: RunContext) -> list[Evidence]:
        item = ctx.item("tokenizer.marginal_cost")
        if item is None:
            return []

        adapter = ctx.adapter
        overhead = adapter.count_tokens(BASELINE_TEXT)
        if overhead is None:
            first = adapter.complete(BASELINE_TEXT, max_tokens=1)
            return [
                self._evidence(
                    "tokenizer.marginal_cost",
                    None,
                    "无法获取 usage.prompt_tokens，本通道不可用",
                    match=None,
                    note=(
                        (first.error or "端点未返回 token 计数")
                        + "。订阅反代（ChatGPT/Codex/Claude 订阅后端）通常不返回 "
                        "token 计数，此时只能依赖其他通道。"
                    ),
                    weight_scale=0.0,
                )
            ]

        # 开销里含着基线串自己那 1 个 token，减掉它会让每一格都少 1 ——
        # 相对差看着不大，但小格子（3~10 token）上足以把「完全一样」打成
        # 「不一样」。参考库存的是纯边际数，所以这里要减的是 overhead-1。
        marginal_overhead = max(0, overhead - BASELINE_TOKENS)

        counts: dict[str, int | None] = {}
        total = len(PROBES)
        tracker = ctx.tracker("tokenizer", "词表探针", total=total,
                              detail=f"基线开销 {overhead} token")
        for probe in PROBES:
            tracker.begin(probe["label"])
            raw = adapter.count_tokens(probe["text"])
            counts[probe["id"]] = (
                None if raw is None else max(0, raw - marginal_overhead)
            )
            tracker.advance()
        tracker.finish()

        usable = {k: v for k, v in counts.items() if v is not None}
        if len(usable) < total // 2:
            return [
                self._evidence(
                    "tokenizer.marginal_cost",
                    None,
                    f"仅 {len(usable)}/{total} 条探针可用，数据不足",
                    match=None,
                    note="端点可能限流或部分拒绝，建议稍后重试。",
                    weight_scale=0.0,
                )
            ]

        observed = {"probe_version": PROBE_VERSION, "overhead": overhead, "counts": counts}

        if not ctx.baselines:
            return [
                self._evidence(
                    "tokenizer.marginal_cost",
                    "family",
                    f"采集到 {len(usable)}/{total} 格向量（开销 {overhead}）",
                    metric={"overhead": overhead, "cells": len(usable)},
                    match=None,
                    note="基准库为空，无法闭集判定。可用 baseline import 录入参考。",
                    weight_scale=0.0,
                )
            ]

        # 两条对照同时用：库里实测过的条目 + 本地词表参考库。
        # 参考库是「词表真理」，蒸馏改不了；库内条目能落到具体型号。
        scored = self.rank(observed, ctx.baselines)
        vocab_ranked = rank_vocabularies(counts)
        vocab_hit = vocab_ranked[0] if vocab_ranked else None
        vocab_note = ""
        if vocab_hit is not None:
            reference, vocab_diff, vocab_same = vocab_hit
            if vocab_diff <= NEAR_TOLERANCE:
                vocab_note = (
                    f"本地词表参考库命中「{reference.get('label')}」"
                    f"（相对差 {vocab_diff:.1%}，{vocab_same}/{len(usable)} 格相同；"
                    f"{reference.get('covers') or ''}）"
                )
            else:
                vocab_note = (
                    f"与本地 {len(vocab_ranked)} 个词表参考均不符"
                    f"（最接近「{reference.get('label')}」相对差 {vocab_diff:.1%}）"
                )

        scores: dict[str, float] = {}
        for model_id, score, _, _ in scored:
            scores[model_id] = max(scores.get(model_id, 0.0), score)
        if vocab_hit is not None and vocab_hit[1] <= NEAR_TOLERANCE:
            # 词表命中 → 该家族的库内条目都能拿到这个分（词表只到 family）
            family = vocab_hit[0].get("family") or ""
            vocab_score = max(0.0, 1.0 - vocab_hit[1] / NEAR_TOLERANCE)
            for entry in ctx.baselines:
                if family and getattr(entry, "family", "") == family:
                    scores[entry.model_id] = max(scores.get(entry.model_id, 0.0), vocab_score)

        # 多后端分派检测与「库里有没有对照」无关：它是关于**观测本身**的陈述。
        # 所以必须放在 scores 判定之前 —— 否则只要库里躺着任意一条带词表指纹
        # 的条目（哪怕分是 0），这个发现就永远不会被报出来。
        mixture = analyze_mixture(counts)
        if mixture and mixture["mixed"]:
            # 这一条比「库里没有」重要得多：它意味着请求被打散到了多个后端，
            # **根本不存在一个单独的身份可以鉴定**。
            spread = "、".join(
                f"{family} {n} 格" for family, n in list(mixture["votes"].items())[:4]
            )
            return [
                self._evidence(
                    "tokenizer.marginal_cost",
                    "family",
                    f"{total} 格命中多个不同词表（{spread}）—— "
                    f"疑似请求被分派到不同后端",
                    metric={
                        "overhead": overhead,
                        "cells": len(usable),
                        "counts": counts,
                        "mixture": mixture,
                        "vocab_best": (
                            {
                                "id": vocab_hit[0].get("id"),
                                "label": vocab_hit[0].get("label"),
                                "family": vocab_hit[0].get("family"),
                                "diff": round(vocab_hit[1], 6),
                                "same_cells": vocab_hit[2],
                            }
                            if vocab_hit else None
                        ),
                    },
                    match=None,
                    note=(
                        "逐格看，每一格都离**某个**已知词表很近（偏差 ≤"
                        f"{mixture['tolerance']}），但近的不是同一个："
                        f"{spread}。这不是「库里没有这个词表」，而是"
                        "**同一批探针落到了不同后端**——中转商按请求分派时"
                        "就是这个样子。此时不存在一个可以鉴定的单一身份，"
                        "本通道不参与身份打分（这个发现本身比身份更有价值，"
                        "见「请求一致性」与「接入链路」）。"
                    ),
                    weight_scale=0.0,
                )
            ]

        if not scores:
            with_tokenizer = sum(
                1 for e in ctx.baselines if (e.channels or {}).get("tokenizer")
            )
            detail = (
                f"采集到 {len(usable)}/{total} 格向量（开销 {overhead}），"
                f"但基准库与词表参考库都无可用对照"
            )
            note = (
                f"基准库共 {len(ctx.baselines)} 条，其中带 tokenizer 数据的 "
                f"{with_tokenizer} 条；本地词表参考 {len(vocab_ranked)} 个。"
                "没有对照数据就不做闭集判定 —— 请用 modelproof baseline import 采集。"
            )
            if vocab_note:
                detail = f"采集到 {len(usable)}/{total} 格向量（开销 {overhead}）"
                note = (
                    vocab_note + "。该词表在库内没有对应条目，本通道弃权。"
                    "可能是库的代次没收录（用 scripts/build_tokenizer_references.py "
                    "--fetch 补），也可能是一个我们还没有参考的词表。"
                )
            return [
                self._evidence(
                    "tokenizer.marginal_cost",
                    "family",
                    detail,
                    metric={
                        "overhead": overhead,
                        "cells": len(usable),
                        "counts": counts,
                        "mixture": mixture,
                        "vocab_best": (
                            {
                                "id": vocab_hit[0].get("id"),
                                "label": vocab_hit[0].get("label"),
                                "family": vocab_hit[0].get("family"),
                                "diff": round(vocab_hit[1], 6),
                                "same_cells": vocab_hit[2],
                            }
                            if vocab_hit else None
                        ),
                    },
                    match=None,
                    note=note,
                    weight_scale=0.0,
                )
            ]

        # 以「参考库命中分」或「库内最佳分」里更强的那个为准
        merged = sorted(scores.items(), key=lambda kv: -kv[1])
        best_model, best_score = merged[0]
        diff = min(
            [row[2] for row in scored] or [1.0]
        ) if scored else (vocab_hit[1] if vocab_hit else 1.0)
        same_cells = max(
            [row[3] for row in scored] or [0]
        ) if scored else (vocab_hit[2] if vocab_hit else 0)
        total_cells = len(usable)

        if diff <= EXACT_TOLERANCE and same_cells >= total_cells * 0.95:
            verdict_note = "同一词表（相对差 ≤0.5% 且 ≥95% 格子相同）"
        elif diff <= EXACT_TOLERANCE:
            verdict_note = "高度接近（相对差 ≤0.5%）"
        elif diff <= NEAR_TOLERANCE:
            verdict_note = "同家族附近（相对差 ≤6%）"
        else:
            verdict_note = "超出近邻范围（相对差 >6%）"

        return [
            self._evidence(
                "tokenizer.marginal_cost",
                "family",
                f"{total_cells} 格，与 {best_model} 参考 {same_cells} 格相同，"
                f"相对差 {diff:.1%}",
                metric={
                    "name": "relative_diff",
                    "value": round(diff, 6),
                    "threshold": EXACT_TOLERANCE,
                    "same_cells": same_cells,
                    "overhead": overhead,
                    "cells": len(usable),
                    # 观测向量存档：事后能复算，也能把一次验证直接转成基准。
                    "counts": counts,
                    "vocab_best": (
                        {
                            "id": vocab_hit[0].get("id"),
                            "label": vocab_hit[0].get("label"),
                            "family": vocab_hit[0].get("family"),
                            "diff": round(vocab_hit[1], 6),
                            "same_cells": vocab_hit[2],
                        }
                        if vocab_hit else None
                    ),
                },
                candidates=[(mid, score) for mid, score in merged[:8]],
                match=diff <= NEAR_TOLERANCE,
                note=(
                    f"{verdict_note}。{vocab_note}。"
                    "同一词表无法区分版本，本条只到 family。"
                    if vocab_note else
                    f"{verdict_note}。同一词表无法区分版本，本条只到 family。"
                ),
            )
        ]

    # -- 比对 -------------------------------------------------------------

    def rank(
        self, observed: dict[str, Any], baselines: list[Any]
    ) -> list[tuple[str, float, float, int]]:
        """对每个有 tokenizer 基准的模型打分，返回按分数降序的列表。"""
        results: list[tuple[str, float, float, int]] = []
        for entry in baselines:
            reference = (entry.channels or {}).get("tokenizer")
            if not reference:
                continue
            if reference.get("probe_version") != PROBE_VERSION:
                continue
            diff, same = compare_counts(observed.get("counts") or {}, reference)
            if diff is None:
                continue
            score = max(0.0, 1.0 - diff / NEAR_TOLERANCE)
            results.append((entry.model_id, score, diff, same))
        results.sort(key=lambda row: row[1], reverse=True)
        return results


def compare_counts(
    observed: dict[str, int | None], reference: dict[str, Any]
) -> tuple[float | None, int]:
    """比对观测向量与参考向量，返回 (相对差, 相同格数)。"""
    ref_counts = reference.get("counts") or {}
    ref_overhead = reference.get("overhead")
    keys = [k for k in ref_counts if observed.get(k) is not None]
    if len(keys) < 8:
        return None, 0

    total_obs = sum(int(observed[k]) for k in keys)  # type: ignore[arg-type]
    total_ref = sum(int(ref_counts[k]) for k in keys)
    # 参考可能记录了不同的请求开销，用总量归一后再比，消除开销口径差异。
    if total_ref <= 0 or total_obs <= 0:
        return None, 0

    diff = 0.0
    same = 0
    for key in keys:
        obs_v = float(observed[key])  # type: ignore[arg-type]
        ref_v = float(ref_counts[key])
        # 按各自总量归一，消除 overhead 口径差异带来的整体偏移。
        obs_n = obs_v / total_obs
        ref_n = ref_v / total_ref
        diff += abs(obs_n - ref_n)
        if abs(obs_v - ref_v) <= max(1.0, 0.02 * ref_v):
            same += 1
    return diff, same


def summarize(counts: dict[str, int | None]) -> str:
    usable = [f"{k}={v}" for k, v in counts.items() if v is not None]
    head = ", ".join(usable[:6])
    return f"{len(usable)} 格：{head}…"
