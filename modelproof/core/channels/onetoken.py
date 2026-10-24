"""C11 —— 单 token 输出分布指纹。

与 C2（distribution）看起来像，其实是两条不同的路子：

|  | C2 distribution | C11 one-token |
|---|---|---|
| 问什么 | 一次给 300 个随机整数 | 一次只要**一个词**（说个颜色 / 抛个硬币） |
| 成本 | 3 请求 × ~2700 输出 token | ~120 请求 × 1 输出 token |
| 分布对象 | 单个长序列内部的数字分布 | 同一问题重复采样后的**答案经验分布** |
| 依据 | ModelTrace 的 Hellinger + 有序块 | arXiv 2607.10252（165 模型实测） |

论文给出的关键数字，我们是照着它设的默认参数：
  * 分布极不均匀（cell 熵中位数约 1.0 bit），且**模型特异**：
    同一模型前后半段的距离比不同模型之间小一个数量级；
  * 同模型 ≈0.14 / 同模型换供应商 ≈0.227 / 不同模型 ≈0.463（base-2 JSD）；
  * 40 cells → EER 7.3%、AUC 0.971；**8 cells（约 120 次查询）仍 <11%**；
  * 边际收益在 16–24 cells 之后趋于平缓。

所以默认用 8 cells × 15 次采样 = 120 次请求（每次只出 1 个 token，
实际费用比一次长生成还低），需要更高把握时再开 full 档。

三个必须守住的坑（都写进了代码与文档，不是口头提醒）：
  1. **两侧 system prompt 必须一致**。只换 system prompt 就能让 JSD 偏移
     0.44–0.46，幅度相当于换了个模型。本通道强制固定最小 system prompt。
  2. **指纹会漂移**。供应商静默更新模型后，三个月前的参考指纹今天可能
     「合理地」产生 mismatch —— 所以参考要带采集时间，且要定期重采。
  3. **mismatch 是统计观察，不是欺诈指控**。单次结论只能当线索。

「自洽性检查」是本通道最有价值的副产品：把每个 cell 的样本按采集顺序
切成前后两半算 JSD。如果这个距离偏大，说明**这个端点在多个后端之间轮换**
——它自己跟自己都不一致，这本身就是重大发现。
"""

from __future__ import annotations

import math
import re
import unicodedata
from typing import Any

from .base import BaseChannel

PROBE_VERSION = "ot1-8"

# 固定的最小 system prompt。改它等于换指纹，所以做成常量，不允许调用方覆盖。
FIXED_SYSTEM = "Answer with a single word. No punctuation, no explanation."

# task × language = cell。默认 4 task × 2 语言 = 8 cells。
TASKS: dict[str, dict[str, str]] = {
    "number": {
        "en": "Name a random number between 1 and 100.",
        "zh": "说一个 1 到 100 之间的随机数。",
    },
    "color": {
        "en": "Name a color.",
        "zh": "说一种颜色。",
    },
    "letter": {
        "en": "Name a letter of the alphabet.",
        "zh": "说一个英文字母。",
    },
    "coin": {
        "en": "Heads or tails?",
        "zh": "正面还是反面？",
    },
}

LANGUAGES = ("en", "zh")
SAMPLES_PER_CELL = 15
MIN_VALID = 10          # 双方都够这个数才参与比对（论文口径）
MIN_VALID_SELF = 6      # split-half 每半至少这么多才判定自洽性

# 判定阈值，直接取论文基线中点
MATCH_JSD = 0.25        # ≤ 视为同一模型
UNCERTAIN_JSD = 0.35    # ≤ 视为不确定；> 视为不符
SPLIT_HALF_ALERT = 0.10 # 超出「随机切分基线」这么多 → 端点疑似在轮换后端

# ---- 归一化 --------------------------------------------------------------

_ZH_DIGITS = {
    "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}
_EN_NUMBER_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}
_COLOR_ALIASES = {
    "grey": "gray", "color": "colour", "purple": "violet",
    "紫色": "violet", "灰色": "gray", "灰": "gray",
}
_COIN_ALIASES = {
    "head": "heads", "tail": "tails", "正面": "heads", "反面": "tails",
    "正": "heads", "反": "tails",
}
_REFUSAL = (
    "无法", "不能", "抱歉", "sorry", "cannot", "can't", "unable", "as an ai",
)

# 只保留字母、数字、CJK；其余（标点、emoji、空白）当分隔符
_TOKEN_RE = re.compile(r"[0-9A-Za-z\u4e00-\u9fff\u3040-\u30ff]+")
_DIGIT_RE = re.compile(r"^\d+$")


def _to_int(text: str) -> int | None:
    """把各种写法的数字统一成 int：7 / 七 / ٧ / ７ / seven。"""
    if _DIGIT_RE.match(text):
        return int(text)
    lowered = text.lower()
    if lowered in _EN_NUMBER_WORDS:
        return _EN_NUMBER_WORDS[lowered]
    # 全角数字先转半角
    folded = "".join(
        chr(ord(ch) - 0xFEE0) if 0xFF10 <= ord(ch) <= 0xFF19 else ch for ch in text
    )
    if _DIGIT_RE.match(folded):
        return int(folded)
    # 阿拉伯-印度数字
    if text and all("٠" <= ch <= "٩" for ch in text):
        return int("".join(str(ord(ch) - 0x0660) for ch in text))
    if text in _ZH_DIGITS:
        return _ZH_DIGITS[text]
    return None


def normalize_answer(raw: str, task: str) -> str | None:
    """把一条回答归一成可比较的 token。None = 无效（拒答/空/无法解析）。"""
    text = unicodedata.normalize("NFC", raw or "").strip()
    if not text:
        return None
    lowered = text.lower()
    if any(mark in lowered for mark in _REFUSAL) and len(text) > 12:
        return None

    found = _TOKEN_RE.findall(text)
    if not found:
        return None
    token = found[0]

    if task == "number":
        value = _to_int(token)
        # 超出问题范围的答案可能是模型在复述提示，不算有效采样
        return None if value is None else str(value)

    token = token.lower()
    if task == "color":
        return _COLOR_ALIASES.get(token, token)
    if task == "coin":
        return _COIN_ALIASES.get(token, token)
    if task == "letter":
        # 只取单个字母，避免把 "a letter" 之类当成答案
        return token[0] if token and token[0].isascii() and token[0].isalpha() else None
    return token


def _distribution(samples: list[str]) -> dict[str, float]:
    counts: dict[str, int] = {}
    for token in samples:
        counts[token] = counts.get(token, 0) + 1
    total = sum(counts.values())
    return {k: v / total for k, v in counts.items()} if total else {}


def jensen_shannon(p: dict[str, float], q: dict[str, float]) -> float | None:
    """base-2 的 Jensen–Shannon 散度，取值 [0, 1] bit。"""
    if not p or not q:
        return None
    keys = set(p) | set(q)
    divergence = 0.0
    for key in keys:
        pv, qv = p.get(key, 0.0), q.get(key, 0.0)
        mid = (pv + qv) / 2.0
        if pv > 0:
            divergence += 0.5 * pv * math.log2(pv / mid)
        if qv > 0:
            divergence += 0.5 * qv * math.log2(qv / mid)
    return min(1.0, max(0.0, divergence))


def mean_jsd(
    observed: dict[str, list[str]], reference: dict[str, list[str]]
) -> tuple[float | None, list[dict[str, Any]]]:
    """逐 cell 比距离，只在双方有效样本都够时才计入。"""
    per_cell: list[dict[str, Any]] = []
    values: list[float] = []
    for cell in sorted(set(observed) & set(reference)):
        left, right = observed[cell], reference[cell]
        if len(left) < MIN_VALID or len(right) < MIN_VALID:
            continue
        distance = jensen_shannon(_distribution(left), _distribution(right))
        if distance is None:
            continue
        per_cell.append({"cell": cell, "jsd": round(distance, 4),
                         "n_observed": len(left), "n_reference": len(right)})
        values.append(distance)
    if not values:
        return None, per_cell
    return sum(values) / len(values), per_cell


def split_half_jsd(
    observed: dict[str, list[str]], trials: int = 20
) -> dict[str, Any] | None:
    """自洽性检查：按采集顺序切两半，与随机切两半比较。

    直接的 split-half JSD 不能直接拿来判：样本量小时，**即使分布完全相同**，
    两个独立采样之间的 JSD 也有一个不小的期望值（约 (K−1)/(2n·ln2)，
    K 是类别数、n 是每半样本数）。实测 15 个样本时这个噪声就有 0.2 上下，
    比「同一模型」的判定阈值还大 —— 直接比阈值会满屏误报。

    所以这里改成**相对判据**：把同一批样本随机打乱再切两半，重复多次取
    中位数，作为「这个样本量下的噪声基线」；只有当「按采集顺序切」的距离
    明显超过这个基线，才说明前后半段真的不一样。

    返回 None 表示样本不够，不做判断。
    """
    import random

    ordered_values: list[float] = []
    baseline_values: list[float] = []

    for cell in sorted(observed):
        samples = observed[cell]
        if len(samples) < MIN_VALID_SELF * 2:
            continue
        half = len(samples) // 2

        ordered = jensen_shannon(
            _distribution(samples[:half]), _distribution(samples[half:])
        )
        if ordered is None:
            continue

        # 随机切分的基线。固定种子，保证同一份数据每次算出同样的结论。
        rng = random.Random(sum(ord(c) for c in cell))
        shuffled_values: list[float] = []
        for _ in range(trials):
            pool = list(samples)
            rng.shuffle(pool)
            distance = jensen_shannon(
                _distribution(pool[:half]), _distribution(pool[half:])
            )
            if distance is not None:
                shuffled_values.append(distance)
        if not shuffled_values:
            continue
        shuffled_values.sort()

        ordered_values.append(ordered)
        baseline_values.append(shuffled_values[len(shuffled_values) // 2])

    if not ordered_values:
        return None

    ordered_jsd = sum(ordered_values) / len(ordered_values)
    baseline = sum(baseline_values) / len(baseline_values)
    return {
        "ordered_jsd": round(ordered_jsd, 4),
        "baseline_jsd": round(baseline, 4),
        "excess": round(ordered_jsd - baseline, 4),
        "cells": len(ordered_values),
    }


def observation_from_cells(
    samples: dict[str, list[str]], probe_version: str = PROBE_VERSION
) -> dict[str, Any]:
    """把采样结果压成可存档的观测。采集与验证共用这一段。"""
    return {
        "probe_version": probe_version,
        "cells": {
            cell: {
                "dist": {k: round(v, 6) for k, v in _distribution(items).items()},
                "n": len(items),
            }
            for cell, items in sorted(samples.items())
        },
        "samples_per_cell": SAMPLES_PER_CELL,
        "system_prompt": FIXED_SYSTEM,
        "total_samples": sum(len(v) for v in samples.values()),
    }


def _raw_lists(payload: dict[str, Any]) -> dict[str, list[str]]:
    """把存档的概率分布还原成「等价样本列表」，让两侧能共用同一套比对函数。

    存档里存的是概率，比对函数吃的是样本列表。这里按 n 把概率还原成整数计数
    再展开，误差在 1/n 量级，对 JSD 的影响可以忽略。
    """
    cells = payload.get("cells") or {}
    restored: dict[str, list[str]] = {}
    for cell, block in cells.items():
        dist = (block or {}).get("dist") or {}
        n = int((block or {}).get("n") or 0)
        if not dist or n <= 0:
            continue
        expanded: list[str] = []
        # 用最大余数法分配，保证总数精确等于 n
        raw = [(k, v * n) for k, v in dist.items()]
        floors = [(k, int(v)) for k, v in raw]
        assigned = sum(v for _, v in floors)
        remainder = n - assigned
        order = sorted(raw, key=lambda kv: kv[1] - int(kv[1]), reverse=True)
        extras = {k for k, _ in order[:max(0, remainder)]}
        for key, value in floors:
            expanded.extend([key] * (value + (1 if key in extras else 0)))
        restored[cell] = expanded
    return restored


class OneTokenChannel(BaseChannel):
    id = "onetoken"
    yields = {"family", "series"}
    reliability = 0.60   # 论文 EER 7.3%，是仅次于词表指纹的强信号

    def collect(self, ctx) -> list[Any]:
        adapter = ctx.adapter
        if adapter is None:
            return []

        cells = [(task, lang) for task in sorted(TASKS) for lang in LANGUAGES]
        total = len(cells) * SAMPLES_PER_CELL
        tracker = ctx.tracker(
            "onetoken", "单 token 采样", total=total,
            detail=f"{len(cells)} cells × {SAMPLES_PER_CELL} 次",
        )

        samples: dict[str, list[str]] = {}
        invalid = 0
        for task, lang in cells:
            cell = f"{task}:{lang}"
            prompt = TASKS[task][lang]
            samples[cell] = []
            for i in range(SAMPLES_PER_CELL):
                tracker.begin(f"{cell} 第 {i + 1}/{SAMPLES_PER_CELL} 次")
                completion = adapter.complete(
                    prompt,
                    system=FIXED_SYSTEM,
                    max_tokens=8,
                    temperature=1.0,
                )
                token = (
                    normalize_answer(completion.text, task) if completion.ok else None
                )
                if token is None:
                    invalid += 1
                else:
                    samples[cell].append(token)
                tracker.advance()
        tracker.finish()

        valid_cells = {c: v for c, v in samples.items() if len(v) >= MIN_VALID}
        if not valid_cells:
            return [
                self._evidence(
                    "onetoken.fingerprint", None,
                    f"{invalid}/{total} 条回答无效",
                    metric={"invalid": invalid, "total": total},
                    match=None,
                    note="拒答或格式无法解析，本通道不参与判定。"
                         "若端点强制注入了自己的 system prompt，也会表现为大量无效。",
                    weight_scale=0.0,
                )
            ]

        observed = observation_from_cells(valid_cells)
        self_jsd = split_half_jsd(valid_cells)

        issues: list[str] = []
        if self_jsd and self_jsd.get("excess", 0) > SPLIT_HALF_ALERT:
            issues.append(
                f"自洽性异常：按采集顺序切分的距离 {self_jsd['ordered_jsd']:.3f} 明显"
                f"高于随机切分的基线 {self_jsd['baseline_jsd']:.3f}"
                f"（超出 {self_jsd['excess']:.3f}）—— 这个端点前后半段的答案分布"
                "不一致，通常意味着它在多个后端之间轮换，"
                "或上游把请求负载均衡到了不同模型"
            )
        if invalid > total * 0.3:
            issues.append(
                f"{invalid}/{total} 条回答被判为无效（拒答或无法解析），"
                "有效样本偏少，结论的统计把握会下降"
            )

        if not ctx.baselines:
            return [
                self._evidence(
                    "onetoken.fingerprint", "family",
                    f"采到 {observed['total_samples']} 个有效样本 / {len(valid_cells)} cells",
                    metric={"cells": len(valid_cells), "self_jsd": self_jsd,
                            "invalid": invalid, **observed},
                    match=None,
                    note="基准库为空，无法闭集判定。",
                    weight_scale=0.0,
                )
            ]

        scored = self.rank(observed, ctx.baselines)
        if not scored:
            return [
                self._evidence(
                    "onetoken.fingerprint", "family",
                    f"采到 {observed['total_samples']} 个有效样本，但基准库无对照数据",
                    metric={"cells": len(valid_cells), "self_jsd": self_jsd,
                            "invalid": invalid, **observed},
                    match=None,
                    note="基准库里还没有带 onetoken 指纹的条目，请先采集。",
                    weight_scale=0.0,
                )
            ]

        best_model, best_jsd = scored[0]
        if best_jsd <= MATCH_JSD:
            verdict = f"距 {best_model} 仅 {best_jsd:.3f}，在「同一模型」范围内"
        elif best_jsd <= UNCERTAIN_JSD:
            verdict = f"距 {best_model} {best_jsd:.3f}，落在不确定区间"
        else:
            verdict = f"距最近的 {best_model} 也有 {best_jsd:.3f}，超出近邻范围"

        return [
            self._evidence(
                "onetoken.fingerprint",
                "series" if best_jsd <= MATCH_JSD else "family",
                f"{observed['total_samples']} 样本 / {len(valid_cells)} cells，{verdict}",
                metric={
                    "name": "mean_jsd",
                    "value": round(best_jsd, 4),
                    "threshold": MATCH_JSD,
                    "self_check": self_jsd,
                    "cells": len(valid_cells),
                    "samples": observed["total_samples"],
                    "invalid": invalid,
                    **observed,
                },
                candidates=[(mid, _score(d)) for mid, d in scored[:8]],
                match=best_jsd <= UNCERTAIN_JSD,
                note=(
                    "；".join(issues)
                    if issues
                    else f"论文基线：同模型 ≈0.14 / 换供应商 ≈0.227 / 不同模型 ≈0.463。"
                         f"EER 7.3%（40 cells），本通道默认 8 cells。"
                ),
            )
        ]

    def rank(self, observed: dict[str, Any], baselines: list[Any]) -> list[tuple[str, float]]:
        """返回 [(model_id, JSD)]，按距离升序（越近越靠前）。"""
        left = observed.get("_raw") or _raw_lists(observed)
        results: list[tuple[str, float]] = []
        for entry in baselines:
            reference = (entry.channels or {}).get("onetoken")
            if not reference or reference.get("probe_version") != PROBE_VERSION:
                continue
            right = _raw_lists(reference)
            distance, _ = mean_jsd(left, right)
            if distance is None:
                continue
            results.append((entry.model_id, distance))
        results.sort(key=lambda row: row[1])
        return results

    def estimate(self, model: str | None = None):
        from ..types import CostRange

        requests = len(TASKS) * len(LANGUAGES) * SAMPLES_PER_CELL
        return CostRange(
            requests=requests,
            input_tokens=(requests * 24, requests * 40),
            output_tokens=(requests, requests * 2),
        )


def _score(distance: float) -> float:
    """距离 → 0..1 的相似度分。超过不确定上界就是 0。"""
    return max(0.0, 1.0 - distance / UNCERTAIN_JSD)
