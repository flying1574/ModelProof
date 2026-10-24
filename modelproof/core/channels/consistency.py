"""C12 —— 请求一致性通道（参数完整性 + 路由一致性）。

C5 gateway 回答的是「这个入口静态看起来是谁搭的」（响应头、TLS、模型列表）。
这一条回答的是另一件事：**它对你发出的每一个请求，是不是始终如一。**

中转商最常见的做法不是「全程用假模型」——那样太容易被抓——而是动态切换：
简单请求丢给便宜模型，复杂请求才动用真货；或者把 max_tokens 悄悄压小、
把 temperature 锁死来省钱。这类行为静态探测看不见，只能在**请求之间**比对。

四组检测，全部可自动判定，不含任何对抗性内容：

| 检测 | 做法 | 异常意味着什么 |
|---|---|---|
| 参数完整性 | 要一段确定长度的列表，看 finish_reason 与返回长度 | max_tokens 被缩减 |
| 采样自由度 | temperature=1.0 下同一 prompt 采样 5 次 | 5 次完全一致 = temperature 被锁死 |
| 能力倒挂 | 简单题 / 复杂题各 3 次，比正确率 | 简单题反而更差 = 简单请求被路由到弱模型 |
| 事实一致性 | 同一可判分事实问 3 次，同时比对对错与数值 | 答案翻转或同题不同值 = 后面可能不是同一个模型 |

设计上刻意保守：**每一条都只报「疑似」，不指认作弊。**
能力倒挂有很多种良性解释（采样波动、题目碰巧），所以它只在
两组题目样本量都足够、且差距明显时才提示。
"""

from __future__ import annotations

import re
from typing import Any

from .base import BaseChannel

PROBE_VERSION = "cs1-4"

# ---- 1) 参数完整性：要一个确定长度的序列，看有没有被截断 ------------------

LIST_PROMPT = (
    "请输出一个包含 60 个项目的清单，每行一个，格式为「1. 项目一」。"
    "从 1 编号到 60，不要省略，不要使用省略号，也不要提前结束。"
)
LIST_LENGTH = 60
LIST_MAX_TOKENS = 600
# 返回行数少于这个比例就算被截断（正常模型要么老老实实输出 60 行，要么明确拒答）
TRUNCATION_RATIO = 0.6

# ---- 2) 采样自由度 ------------------------------------------------------

FREEDOM_PROMPT = "请说出一种你喜欢的动物，只回答一个词，不要解释。"
FREEDOM_N = 5
FREEDOM_MAX_TOKENS = 16

# ---- 3) 能力倒挂 --------------------------------------------------------

EASY_PROBE = {
    "id": "easy",
    "prompt": "在标准大气压下，水的沸点是多少摄氏度？只回答一个数字。",
    "expect": 100.0,
    "tolerance": 0.5,
}
HARD_PROBE = {
    "id": "hard",
    "prompt": "请直接计算：((17 × 23) − 91) ÷ 6 的结果是多少？只回答一个数字。",
    "expect": 50.0,
    "tolerance": 0.5,
}
DIFFICULTY_N = 3           # 各问 3 次
INVERSION_MARGIN = 0.34    # 简单正确率比复杂低这么多才提示（3 次里差 1 次）

# ---- 4) 事实一致性 ------------------------------------------------------

FACT_PROBE = {
    "id": "fact",
    "prompt": "光在真空中的速度大约是多少千米每秒？只回答一个整数。",
    "expect": 299792.0,
    "tolerance": 1000.0,
}
FACT_N = 3

_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")


def extract_number(text: str) -> float | None:
    match = _NUMBER_RE.search(text or "")
    return float(match.group(0)) if match else None


def _is_correct(value: float | None, expect: float, tolerance: float) -> bool:
    return value is not None and abs(value - expect) <= tolerance


def _finish_reason(completion: Any) -> str | None:
    """从原始响应体里取 finish_reason；拿不到就返回 None。"""
    raw = getattr(completion, "raw", None) or {}
    choices = raw.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            return first.get("finish_reason")
    return None


def _count_lines(text: str) -> int:
    return len([line for line in (text or "").splitlines() if line.strip()])


class ConsistencyChannel(BaseChannel):
    id = "consistency"
    yields = set()      # 不做身份判定，产出的是「这个入口是否始终如一」
    reliability = 0.0

    def collect(self, ctx) -> list[Any]:
        item = ctx.item("consistency.requests")
        if item is None:
            return []

        adapter = ctx.adapter
        if adapter is None:
            return []

        total = 1 + FREEDOM_N + DIFFICULTY_N * 2 + FACT_N
        tracker = ctx.tracker(
            "consistency", "请求一致性", total=total,
            detail="参数 / 采样 / 难度 / 事实 四组",
        )

        issues: list[str] = []
        metrics: dict[str, Any] = {}

        # ---- 1) 参数完整性 ----
        tracker.begin("参数完整性")
        completion = adapter.complete(LIST_PROMPT, max_tokens=LIST_MAX_TOKENS)
        lines = _count_lines(completion.text) if completion.ok else 0
        reason = _finish_reason(completion)
        truncated = bool(
            completion.ok
            and reason == "length"
            and lines < LIST_LENGTH * TRUNCATION_RATIO
        )
        metrics["list_lines"] = lines
        metrics["list_expected"] = LIST_LENGTH
        metrics["finish_reason"] = reason
        metrics["max_tokens_respected"] = not truncated
        if truncated:
            issues.append(
                f"请求 {LIST_MAX_TOKENS} 个 token 的清单，只回传了 {lines} 行且被标记为"
                " finish_reason=length —— max_tokens 疑似被上游缩减"
            )
        elif not completion.ok:
            issues.append(f"清单请求失败（{completion.error}），参数完整性未检测")
        tracker.advance()

        # ---- 2) 采样自由度 ----
        freedom: list[str] = []
        for i in range(FREEDOM_N):
            tracker.begin(f"采样自由度 {i + 1}/{FREEDOM_N}")
            result = adapter.complete(
                FREEDOM_PROMPT, max_tokens=FREEDOM_MAX_TOKENS, temperature=1.0
            )
            if result.ok:
                freedom.append((result.text or "").strip()[:40])
            tracker.advance()
        unique = len(set(freedom))
        metrics["freedom_samples"] = len(freedom)
        metrics["freedom_unique"] = unique
        # 至少要 3 个样本才谈得上「有没有差异」，否则不判断，避免误报
        metrics["temperature_locked"] = bool(len(freedom) >= 3 and unique == 1)
        if metrics["temperature_locked"]:
            issues.append(
                f"temperature=1.0 下重复 {len(freedom)} 次得到完全相同的回答 —— "
                "temperature 疑似被上游锁死（也可能只是该题收敛，需结合其他证据）"
            )

        # ---- 3) 能力倒挂 ----
        easy_hits, _ = self._repeat(
            adapter, EASY_PROBE, DIFFICULTY_N, tracker, "简单题"
        )
        hard_hits, _ = self._repeat(
            adapter, HARD_PROBE, DIFFICULTY_N, tracker, "复杂题"
        )
        easy_rate = easy_hits / DIFFICULTY_N if DIFFICULTY_N else 0.0
        hard_rate = hard_hits / DIFFICULTY_N if DIFFICULTY_N else 0.0
        metrics["easy_correct"] = easy_hits
        metrics["hard_correct"] = hard_hits
        metrics["easy_rate"] = round(easy_rate, 3)
        metrics["hard_rate"] = round(hard_rate, 3)
        inverted = easy_rate < hard_rate - INVERSION_MARGIN
        metrics["difficulty_inversion"] = bool(inverted)
        if inverted:
            issues.append(
                f"难度倒挂：简单题答对 {easy_hits}/{DIFFICULTY_N}，"
                f"复杂题反而答对 {hard_hits}/{DIFFICULTY_N}。"
                "这不符合能力单调性，疑似简单请求被路由到了更弱的模型"
                "（也可能是采样波动，建议提高重复次数再确认）"
            )

        # ---- 4) 事实一致性 ----
        fact_hits, fact_answers = self._repeat(
            adapter, FACT_PROBE, FACT_N, tracker, "事实题"
        )
        fact_variants = sorted(set(fact_answers))
        metrics["fact_correct"] = fact_hits
        metrics["fact_repeats"] = FACT_N
        metrics["fact_answers"] = fact_answers
        metrics["fact_variants"] = len(fact_variants)
        if FACT_N and 0 < fact_hits < FACT_N:
            issues.append(
                f"同一事实重复 {FACT_N} 次，{FACT_N - fact_hits} 次答案超出容差 —— "
                "答案翻转，后面可能不是同一个模型，或上层做了结果扰动"
            )
        elif len(fact_variants) >= 2:
            # 都在容差内但数值不同：299792 与 300000 都「答对」，却是两个后端
            # 最典型的痕迹。只看对错会漏掉这一类，所以单独记一笔。
            issues.append(
                f"同一事实重复 {FACT_N} 次得到 {len(fact_variants)} 种不同数值"
                f"（{' / '.join(fact_variants)}）—— 答案不稳定，疑似请求被分派到了"
                "不同后端（也可能是同一模型的表述波动，需结合其他证据判断）"
            )

        tracker.finish()

        verdict = "consistent" if not issues else (
            "unstable" if len(issues) >= 2 else "caution"
        )
        fact_part = f"事实 {fact_hits}/{FACT_N}"
        if len(fact_variants) >= 2:
            fact_part += f"（{len(fact_variants)} 种答案）"
        observed = (
            f"参数 {'正常' if metrics['max_tokens_respected'] else '异常'} · "
            f"采样 {unique}/{len(freedom)} 种 · "
            f"简单 {easy_hits}/{DIFFICULTY_N} vs 复杂 {hard_hits}/{DIFFICULTY_N} · "
            f"{fact_part}"
        )

        return [
            self._evidence(
                "consistency.requests", None, observed,
                metric={"verdict": verdict, "issues": issues, **metrics},
                match=(verdict == "consistent"),
                note=(
                    "；".join(issues)
                    if issues
                    else "四组一致性检测均通过：参数未被缩减、采样有自由度、"
                         "能力表现单调、事实答案稳定。"
                ),
                weight_scale=0.0,   # 不参与身份打分，是入口行为信号
            )
        ]

    def _repeat(self, adapter, probe: dict[str, Any], times: int, tracker, label: str):
        """同一探针重复问 times 次，返回 (答对次数, 归一化后的答案列表)。"""
        hits = 0
        answers: list[str] = []
        for i in range(times):
            tracker.begin(f"{label} {i + 1}/{times}")
            completion = adapter.complete(probe["prompt"], max_tokens=32)
            if completion.ok:
                value = extract_number(completion.text)
                if value is not None:
                    answers.append(f"{value:g}")
                if _is_correct(
                    value, float(probe["expect"]), float(probe["tolerance"])
                ):
                    hits += 1
            tracker.advance()
        return hits, answers

    def estimate(self, model: str | None = None):
        from ..types import CostRange

        requests = 1 + FREEDOM_N + DIFFICULTY_N * 2 + FACT_N
        return CostRange(
            requests=requests,
            input_tokens=(requests * 40, requests * 90),
            output_tokens=(600, 900),
        )
