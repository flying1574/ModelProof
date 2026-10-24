"""C2 数字分布指纹 —— ModelTrace 方法的参考实现。

为什么需要它：词表指纹（C1）能分开「词表不同」的模型，但同一代模型
经常共用词表，这时 C1 就分不出来了。数字分布指纹问的是另一个问题——
让模型凭第一反应产出几百个 1–355 的整数，统计它落在各处的形状。
采样偏好来自训练数据与解码实现，套壳改不掉，所以它是**硬信号**（已列入
fusion/decide.py 的 HARD_CHANNELS）。

方法直接对齐 ModelTrace/fingerprint.py：
  1. 边际分布：355 维计数 → 加平滑 → sqrt 归一 → Hellinger 距离；
  2. 有序块特征：把序列切成 4 段各做 16 桶直方图，再拼上末位数字分布，
     共 74 维——它保留了「顺序」信息，能抓到边际分布看不出的人工痕迹；
  3. 融合：0.75 × 边际 + 0.25 × 有序块（与 ModelTrace 的
     ORDERED_BLOCK_WEIGHT 一致）。

这里用的是纯标准库实现（无 numpy）：ModelProof 的 core 层不依赖第三方包。

2026-10-18 补记：原先这里写着「少了 ModelTrace 那套去噪基底，先不做」。
现在补上了 —— robust.py 移植了它在 18 个模型 × 12 种提示词环境、756 条
真实回答上训练好的干扰基与类心，还带分组交叉验证定出的 beta。有打分包时
本通道走稳健打分（概率是真校准过的，且有 out_of_scope 守卫）；没有时退回
裸距离，并在结论里明说阈值是占位值。
"""

from __future__ import annotations

import math
import random
import re
from typing import Any

from .base import BaseChannel
from . import robust

# 有序块特征与稳健打分包必须共用同一套切分语义（numpy.array_split），
# 否则打分包算出来的分对不上它自己的类心。单一事实来源放在 robust 里。
ordered_block_feature = robust.ordered_block_feature

PROBE_VERSION = "db1-4"

VALUE_MIN = 1
VALUE_MAX = 355
DIMENSION = VALUE_MAX - VALUE_MIN + 1

# 单次挑战要的整数个数，取自 292..332 的不重复池（与 ModelTrace 一致）。
# 池大小决定了一次最多能出几道挑战。
LENGTH_POOL = range(292, 333)
MAX_CHALLENGES = len(LENGTH_POOL)
CHALLENGE_COUNT = 3

def _budget_ladder(expected: int) -> list[tuple[int, int]]:
    """一条挑战的预算阶梯：(要几个整数, max_tokens)。

    第一档是原始设计，后面两档逐级削减。中转端点常见的三种挡法：
      * 不接受大的 max_tokens   → 400/413 报错，退到 1024 可能就过了；
      * 对长输出限流            → 请求量减半更容易通过；
      * 输出被静默截断          → 少要一些数反而能凑够 MIN_NUMBERS。
    三档都不行就把错误文本原样报出去，别再让人猜为什么没结果。
    """
    # 最后一档 512：市面上不少中转把上限卡在 512，退不到这儿就永远拿不到样本。
    return [(max(140, expected), 2048), (200, 1024), (140, 768), (110, 512)]


# 最少的可用整数个数。低于这个数分布形状没有统计意义，宁可弃权也不能凑合。
MIN_NUMBERS = 80

BLOCKS = 4
BLOCK_BINS = 16
ORDERED_BLOCK_WEIGHT = 0.25
SMOOTHING = 0.5

# 距离容差。这两个数是**占位值**，必须用真实数据重新标定
# （modelproof baseline recalibrate），标好前不要把结论当定论。
EXACT_DISTANCE = 0.06   # 基本就是同一个模型
NEAR_DISTANCE = 0.20    # 超过这个距离就不该算作同一族


# --------------------------------------------------------------------------
# 挑战生成
# --------------------------------------------------------------------------

_OPENINGS = [
    "这是一次独立的数值选择记录",
    "请完成下面的无语义整数选择任务",
    "执行一次第一反应取值记录",
    "生成一组不承载语义的整数选择",
    "进行一轮快速逐项取值",
]

_ACTIONS = [
    "为各个位置分别凭第一反应选择",
    "逐项选择",
    "每次只决定当前一项，共给出",
    "分别凭第一反应给出",
    "逐个直接选择",
]

_ENDINGS = [
    "允许某个数字再次出现；每项写出后不要回头排序、去重或替换。",
    "偶然重复是有效的；不要重新排列或修正已经写出的项目。",
    "相同值可以再次出现；输出过程中不要整理或改写前面的项目。",
    "重复值无需删除；不要筛选、重排或补成某种规律。",
    "不必赋予数字任何含义；已经给出的值保持不变。",
]

_SEPARATORS = [
    "数字之间用逗号或空格分隔均可。",
    "使用一种一致的常见分隔符即可。",
    "可以用逗号、空格或换行分隔。",
    "只要每个整数边界清楚，格式可自行选择。",
]


def build_prompt(length: int, rng: random.Random) -> str:
    """构造一条整数生成挑战。措辞变化是为了压掉「背答案」。"""
    return (
        f"{rng.choice(_OPENINGS)}。{rng.choice(_ACTIONS)} {length} 个 "
        "1 到 355（含端点）的整数。每个位置都要单独选择；不要从 1 开始计数，"
        "不要连续递增或递减，也不要采用等差、循环、重复区块或其他规则化模式。"
        "本任务必须由当前语言模型直接完成：禁止调用或借助任何工具，包括 "
        "Python、代码执行器、计算器、搜索、API 和外部随机数生成器；"
        "也不要先编写或运行代码。"
        f"{rng.choice(_ENDINGS)}{rng.choice(_SEPARATORS)}"
        "直接从第一个取值开始输出，不要在序列前重复数量、范围或任务说明。"
    )


def generate_challenges(count: int = CHALLENGE_COUNT) -> list[dict[str, Any]]:
    """生成本次要问的几条挑战。长度随机，防止模型记住上一次的答案。"""
    count = max(1, min(int(count), MAX_CHALLENGES))
    rng = random.SystemRandom()
    lengths = rng.sample(list(LENGTH_POOL), count)
    return [
        {"id": f"dist-{i + 1}", "expected_count": length, "prompt": build_prompt(length, rng)}
        for i, length in enumerate(lengths)
    ]


# --------------------------------------------------------------------------
# 特征与距离
# --------------------------------------------------------------------------


def parse_numbers(text: str) -> list[int]:
    """从回答里抠出最长的那段连续整数序列（跳过序号、说明等干扰）。"""
    runs: list[list[int]] = []
    current: list[int] = []
    previous_end = 0
    for match in re.finditer(r"\d+", text):
        separator = text[previous_end:match.start()]
        value = int(match.group())
        # 中间夹了字母说明这是另一段文字，另起一段
        if current and any(ch.isalpha() for ch in separator):
            runs.append(current)
            current = []
        if VALUE_MIN <= value <= VALUE_MAX:
            current.append(value)
        previous_end = match.end()
    if current:
        runs.append(current)
    return max(runs, key=len) if runs else []


def count_numbers(numbers: list[int]) -> list[int]:
    counts = [0] * DIMENSION
    for value in numbers:
        counts[value - VALUE_MIN] += 1
    return counts


def _unit_sqrt(values: list[float], smoothing: float = SMOOTHING) -> list[float]:
    """加平滑后取 sqrt 归一：得到 L2 范数为 1 的 Hellinger 向量。"""
    total = sum(values) + smoothing * len(values)
    if total <= 0:
        return [0.0] * len(values)
    return [math.sqrt((v + smoothing) / total) for v in values]


def marginal_feature(counts: list[int]) -> list[float]:
    return _unit_sqrt([float(v) for v in counts], smoothing=0.5)


# ordered_block_feature 由 robust 模块提供（见文件顶部），此处不再重复定义：
# 采集端与打分包必须用同一份实现，否则 74 维向量对不上类心。


def hellinger_distance(left: list[float], right: list[float]) -> float:
    """两个 sqrt 概率向量之间的 Hellinger 距离，取值 [0, 1]。"""
    if len(left) != len(right) or not left:
        return 1.0
    total = sum((a - b) ** 2 for a, b in zip(left, right))
    return min(1.0, math.sqrt(max(0.0, total) / 2.0))


def compare_distribution(observed: dict[str, Any], reference: dict[str, Any]) -> float | None:
    """观测分布与参考分布的距离，越小越像。无可比数据时返回 None。"""
    ref_counts = reference.get("counts")
    obs_counts = observed.get("counts")
    if not ref_counts or not obs_counts or len(ref_counts) != len(obs_counts):
        return None

    obs_feature = observed.get("_marginal") or marginal_feature(list(obs_counts))
    marginal = hellinger_distance(obs_feature, marginal_feature(list(ref_counts)))

    ordered = None
    if observed.get("ordered") and reference.get("ordered"):
        ordered = hellinger_distance(list(observed["ordered"]), list(reference["ordered"]))

    if ordered is None:
        return marginal
    return (1.0 - ORDERED_BLOCK_WEIGHT) * marginal + ORDERED_BLOCK_WEIGHT * ordered


def _score(distance: float) -> float:
    """距离 → 0..1 的相似度分。超过近邻容差就是 0，不给分就是不给分。"""
    return max(0.0, 1.0 - distance / NEAR_DISTANCE)


def _family_of(model_id: str) -> str:
    """从基准库里取这个模型的家族，取不到就退回 ID 的第一段。"""
    try:
        from ..baseline.store import BaselineStore

        store = BaselineStore()
        entry = store.get(model_id)
        if entry is not None and entry.identity and entry.identity.family:
            return entry.identity.family
    except Exception:  # noqa: BLE001 —— 家族只是展示用，取不到不该让判定失败
        pass
    return model_id.split("-")[0] if "-" in model_id else model_id


def _family_ranking(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把候选概率按家族相加后排序。

    为什么需要：单个候选的概率在闭集里必然归一到 1，看单个数字意义有限；
    同家族多个候选的概率相加，才是「这批候选整体指向哪个家族」的答案。
    界面上的「自动识别家族 · GPT · 92.6%」就是这么来的。
    """
    totals: dict[str, float] = {}
    for row in results or []:
        family = _family_of(str(row.get("model") or ""))
        totals[family] = totals.get(family, 0.0) + float(row.get("probability") or 0.0)
    ranked = sorted(totals.items(), key=lambda kv: -kv[1])
    return [{"family": family, "probability": round(value, 4)} for family, value in ranked[:4]]


def observation_from_numbers(sample_numbers: list[list[int]]) -> dict[str, Any]:
    """把若干份整数样本合成一条可存档的观测。采集与验证共用这一段。"""
    if not sample_numbers:
        return {"probe_version": PROBE_VERSION, "counts": [], "ordered": [], "samples": 0}
    pooled = [0] * DIMENSION
    for numbers in sample_numbers:
        counts = count_numbers(numbers)
        for i in range(DIMENSION):
            pooled[i] += counts[i]
    ordered_size = len(ordered_block_feature(sample_numbers[0]))
    ordered = [0.0] * ordered_size
    for numbers in sample_numbers:
        feature = ordered_block_feature(numbers)
        for i in range(ordered_size):
            ordered[i] += feature[i] / len(sample_numbers)
    return {
        "probe_version": PROBE_VERSION,
        "counts": pooled,
        "ordered": ordered,
        "samples": len(sample_numbers),
        "total_numbers": sum(pooled),
    }


def summarize(payload: dict[str, Any]) -> str:
    counts = payload.get("counts") or []
    return f"{payload.get('samples', 0)} 份 × {sum(counts)} 个整数"


# --------------------------------------------------------------------------
# 通道
# --------------------------------------------------------------------------


class DistributionChannel(BaseChannel):
    """数字分布指纹通道。"""

    id = "distribution"
    yields = {"family", "series"}
    # 标定前的占位值；跑 modelproof baseline recalibrate 后会被覆盖。
    reliability = 0.55

    # -- 采集辅助 ---------------------------------------------------------

    def _collect_numbers(
        self,
        adapter: Any,
        challenge: dict[str, Any],
        expected: int,
        attempts: list[tuple[int, int]],
        tracker: Any,
        diagnostics: list[dict[str, Any]],
    ) -> tuple[list[int] | None, int | None]:
        """按预算阶梯问一条挑战，返回第一份够用的样本与它用的那一档。

        阶梯每一级是 (要几个整数, max_tokens)：第一级用原始设计，后面逐级
        削减。**为什么要削减**：中转端点常见的做法是不接受大的 max_tokens、
        对长输出限流、或者干脆报错。以前只有一档，碰上就直接判死，而且
        错误文本被丢掉，报告只能写「端点请求失败」——排障的人（包括我自己）
        只能猜。
        """
        rng = random.SystemRandom()
        for asked, budget in attempts:
            prompt = (
                challenge["prompt"] if asked == expected else build_prompt(asked, rng)
            )
            tracker.begin(f"要 {asked} 个整数 · 上限 {budget} token")
            completion = adapter.complete(prompt, max_tokens=budget)
            numbers = parse_numbers(completion.text or "") if completion.ok else []
            minimum = max(MIN_NUMBERS, math.ceil(asked * 0.55))
            accepted = len(numbers) >= minimum
            raw = completion.raw or {}
            choices = raw.get("choices") or []
            diagnostics.append(
                {
                    "expected": expected,
                    "asked": asked,
                    "max_tokens": budget,
                    "parsed": len(numbers),
                    "minimum": minimum,
                    "accepted": accepted,
                    "ok": completion.ok,
                    # 错误文本必须留住：这是排障唯一的线索。
                    "error": completion.error or "",
                    "status": completion.status,
                    "finish_reason": (
                        (choices[0].get("finish_reason") if choices else None)
                        or raw.get("finish_reason")
                    ),
                }
            )
            if accepted:
                return numbers, budget
        return None, None

    def _failure_evidence(self, diagnostics: list[dict[str, Any]]) -> Any:
        """采集彻底失败时的证据。**必须说清为什么失败**。"""
        errors = sorted({d.get("error") or "" for d in diagnostics if d.get("error")})
        statuses = sorted({d.get("status") for d in diagnostics if d.get("status")})
        finishes = sorted(
            {d.get("finish_reason") for d in diagnostics if d.get("finish_reason")}
        )
        parsed = [int(d.get("parsed") or 0) for d in diagnostics]
        best = max(parsed) if parsed else 0

        if errors:
            headline = f"{len(diagnostics)} 次请求全部被拒：{errors[0][:160]}"
            detail = (
                "端点返回的错误原样记在上面，这是排障唯一的线索。常见原因："
                "中转不接受这么大的 max_tokens（已逐级退让到 "
                f"{diagnostics[-1].get('max_tokens') if diagnostics else '—'} token）、"
                "该模型被限流、或密钥对它没有配额。"
            )
        elif best > 0:
            headline = (
                f"拿到的整数不足（最多 {best} 个，至少需要 "
                f"{diagnostics[0].get('minimum') if diagnostics else '—'} 个）"
            )
            detail = (
                f"输出被截断了（finish_reason={finishes or '—'}）。"
                "常见原因是端点把输出上限压得很小，已按预算阶梯逐级退让到最后一档。"
            )
        else:
            headline = f"{len(diagnostics)} 次请求全部失败，端点没给错误文本"
            detail = (
                f"状态码 {statuses or '—'}。这种情况建议改用 C11 onetoken 通道："
                "每条只要 1 个 token，几乎不可能被输出长度限制拖住。"
            )

        return self._evidence(
            "distribution.random_int",
            None,
            headline,
            metric={
                "diagnostics": diagnostics,
                "errors": errors,
                "statuses": statuses,
                "finish_reasons": finishes,
            },
            match=None,
            note=detail,
            weight_scale=0.0,
        )

    def collect(self, ctx) -> list[Any]:
        adapter = ctx.adapter
        if adapter is None:
            return []

        challenges = generate_challenges(CHALLENGE_COUNT)
        tracker = ctx.tracker(
            "distribution",
            "数字分布采集",
            total=len(challenges),
            detail="每条 292–332 个整数",
        )

        samples: list[dict[str, Any]] = []
        diagnostics: list[dict[str, Any]] = []
        # 预算阶梯：先按满预算问，失败或样本不足就递减。有些中转不接受大的
        # max_tokens、或对长输出直接报错，也有的是把输出悄悄截断。
        # 一路退到最后一档，至少弄明白到底卡在哪 —— 顺便把错误文本留住，
        # 之前报告只写「端点请求失败」，谁也看不出为什么。
        budgets = None  # 上一份样本通过的是哪一档，下一份就从它起步
        for index, challenge in enumerate(challenges):
            expected = int(challenge["expected_count"])
            ladder = _budget_ladder(expected)
            collected, used = self._collect_numbers(
                adapter, challenge, expected, budgets or ladder,
                tracker, diagnostics,
            )
            if collected:
                samples.append(collected)
                # 已经知道这一档能过，后面的挑战就不必再把必拒的大档位
                # 各撞一次 —— 对限流的端点来说，少发几次比多发几次重要。
                budgets = [step for step in ladder if step[1] <= (used or 0)] or ladder
            tracker.advance()
        tracker.finish()

        if not samples:
            return [self._failure_evidence(diagnostics)]

        observed = observation_from_numbers(samples)
        pooled = observed["counts"]

        # -- 有训练好的打分包时优先走稳健打分 -----------------------------
        # 路径差异不是小事：裸距离的阈值是占位值，给不出能当概率用的数；
        # 打分包带交叉验证标定的 beta 和 out_of_scope 守卫，
        # 而且遇到库外模型会**弃权**而不是硬塞一个最像的候选。
        pack = robust.load_pack()
        if pack and robust.pack_models(pack) and robust.covers_bank(pack, ctx.baselines):
            analysis = robust.analyze(samples, pack)
            if analysis.get("available"):
                return [self._robust_evidence(analysis, diagnostics)]

        if not ctx.baselines:
            return [
                self._evidence(
                    "distribution.random_int",
                    "family",
                    f"采集到 {len(samples)} 份样本 / {sum(pooled)} 个整数",
                    metric={"samples": len(samples), "numbers": sum(pooled)},
                    match=None,
                    note="基准库为空，无法闭集判定。",
                    weight_scale=0.0,
                )
            ]

        scored = self.rank(observed, ctx.baselines)
        with_data = sum(1 for e in ctx.baselines if (e.channels or {}).get("distribution"))
        if not scored:
            return [
                self._evidence(
                    "distribution.random_int",
                    "family",
                    f"采集到 {len(samples)} 份样本，但基准库无对照数据",
                    metric={"samples": len(samples), "numbers": sum(pooled)},
                    match=None,
                    note=(
                        f"基准库共 {len(ctx.baselines)} 条，其中带 distribution 数据的 "
                        f"{with_data} 条。请先对已知端点跑一次采集"
                        "（Web「采集基准」勾选数字分布指纹）。"
                    ),
                    weight_scale=0.0,
                )
            ]

        best_model, best_distance = scored[0]
        if best_distance <= EXACT_DISTANCE:
            verdict = "分布高度一致（距离 ≤0.06）"
        elif best_distance <= NEAR_DISTANCE:
            verdict = "分布接近（距离 ≤0.20），可能是同族近亲版本"
        else:
            verdict = "分布超出近邻范围，可能不在库内"

        return [
            self._evidence(
                "distribution.random_int",
                "series",
                f"{len(samples)} 份样本 / {sum(pooled)} 个整数，"
                f"与 {best_model} 距离 {best_distance:.3f}",
                metric={
                    "name": "hellinger_distance",
                    "value": round(best_distance, 4),
                    "threshold": EXACT_DISTANCE,
                    "samples": len(samples),
                    "numbers": sum(pooled),
                    "diagnostics": diagnostics,
                },
                candidates=[(mid, _score(dist)) for mid, dist in scored[:8]],
                match=best_distance <= NEAR_DISTANCE,
                note=f"{verdict}。距离阈值为占位值，需用真实数据标定。",
            )
        ]

    def _robust_evidence(
        self, analysis: dict[str, Any], diagnostics: list[dict[str, Any]]
    ) -> Any:
        """把稳健打分结果包装成一条证据。

        最关键的一段是 `out_of_scope`：命中时**不给候选**，weight 置 0。
        这不是保守，是必需 —— 闭集归因在数学上必然给库外模型指派一个
        已有候选，软最高分的 84.66% 不是概率，更不说明训练血缘
        （附录 G 那次「DeepSeek 蒸馏自 GPT-6 Astra」的误传就是这么来的）。
        所以宁可弃权，也要把「不在库内」这四个字说出来。
        """
        calibration = analysis.get("calibration") or {}
        accuracy = calibration.get("cv_accuracy")
        samples = analysis.get("samples", 0)
        out_of_scope = analysis.get("out_of_scope")
        best = analysis.get("best_score", 0.0)
        top = analysis.get("prediction", "")
        probability = analysis.get("probability", 0.0)

        metric: dict[str, Any] = {
            "name": "robust_fused_z",
            "value": round(best, 4),
            "threshold": robust.OUT_OF_SCOPE_SCORE,
            "samples": samples,
            "diagnostics": diagnostics,
            "robust": {
                "prediction": top,
                "probability": round(probability, 4),
                "best_score": round(best, 4),
                "second_score": round(analysis.get("second_score", 0.0), 4),
                "gap": round(analysis.get("gap", 0.0), 4),
                "gap_ratio": round(analysis.get("gap_ratio", 0.0), 4),
                "out_of_scope": out_of_scope,
                "out_of_scope_threshold": robust.OUT_OF_SCOPE_SCORE,
                "scope_checkable": analysis.get("scope_checkable"),
                "extrapolated": analysis.get("extrapolated"),
                "method": analysis.get("method"),
                "calibration": calibration,
                "candidates": len(analysis.get("results") or []),
                "top": [
                    {
                        "model": r["model"],
                        "probability": round(r["probability"], 4),
                        # 家族与边际分一起给：界面第一屏要像 ModelTrace 那样摆
                        # 「最可能模型 / 概率 / 家族 + 家族概率」，缺这两个字段
                        # 就摆不出来，前端只能干瞪着一个数字。
                        "family": _family_of(r["model"]),
                        "marginal_score": round(r.get("marginal_score", 0.0), 4),
                        "score": round(r.get("score", 0.0), 4),
                    }
                    for r in (analysis.get("results") or [])[:8]
                ],
                # 家族级聚合：把同家族候选的概率相加，回答「是哪个家族」
                "family_top": _family_ranking(analysis.get("results") or []),
            },
        }

        accuracy_text = (
            f"，该档位分组交叉验证准确率 {accuracy:.1%}"
            f"（{calibration.get('cv_samples')} 个样本）"
            if isinstance(accuracy, (int, float)) else ""
        )

        if out_of_scope:
            return self._evidence(
                "distribution.random_int",
                None,
                f"最高分 {best:.2f} < 库外阈值 {robust.OUT_OF_SCOPE_SCORE}"
                f"（最像的是 {top}，但不足以判定）",
                metric=metric,
                match=False,
                note=(
                    "被测分布与库内 18 个参考指纹都不够像，判为**不在库内**。"
                    "注意这里的闭集概率看着可能仍然很高（softmax 总要归一到 1），"
                    "它不代表「是这个模型」，只代表「在这批候选里最像它」。"
                    "库里目前只有 GPT / Claude 两个家族，测其他家族时出现这个结果"
                    "属正常。要得到可用判定，先把目标模型采进库。"
                ),
                weight_scale=0.0,
            )

        if out_of_scope is None:
            # 候选数不足 8，ModelTrace 认为此时做不了库外判断
            return self._evidence(
                "distribution.random_int",
                "series",
                f"{samples} 份样本，最像 {top}（最高分 {best:.2f}）",
                metric=metric,
                candidates=[(r["model"], r["probability"]) for r in analysis["results"][:8]],
                match=True,
                note=(
                    f"打分包只有 {len(analysis.get('results') or [])} 个候选，"
                    f"少于库外判断所需的最少 {robust.MIN_MODELS_FOR_SCOPE} 个，"
                    "因此**不做**「是否在库内」的判断。结论仅供参考。"
                ),
                weight_scale=0.5,
            )

        return self._evidence(
            "distribution.random_int",
            "version",
            f"{samples} 份样本 → {top}（{probability:.1%}，最高分 {best:.2f}）",
            metric=metric,
            candidates=[(r["model"], r["probability"]) for r in analysis["results"][:8]],
            match=True,
            note=(
                f"ModelTrace 稳健数字指纹：边际 Hellinger 0.75 + 有序块 0.25，"
                f"剔除 2 个提示词干扰方向后与 18 个类心比对{accuracy_text}。"
                f"{'样本数超过标定上限 3 条，beta 按第 3 档外推。' if analysis.get('extrapolated') else ''}"
                "这包只在 GPT / Claude 上训练过，换家族必须重采。"
            ),
            weight_scale=max(0.0, min(1.0, float(accuracy or 1.0))),
        )

    def rank(
        self, observed: dict[str, Any], baselines: list[Any]
    ) -> list[tuple[str, float]]:
        """返回 [(model_id, 距离)]，按距离升序（越像越靠前）。"""
        obs_feature = marginal_feature(list(observed.get("counts") or []))
        payload = dict(observed)
        payload["_marginal"] = obs_feature

        results: list[tuple[str, float]] = []
        for entry in baselines:
            reference = (entry.channels or {}).get("distribution")
            if not reference:
                continue
            if reference.get("probe_version") != PROBE_VERSION:
                continue
            distance = compare_distribution(payload, reference)
            if distance is None:
                continue
            results.append((entry.model_id, distance))
        results.sort(key=lambda row: row[1])
        return results

    def estimate(self, model: str | None = None):
        from ..types import CostRange

        return CostRange(
            requests=CHALLENGE_COUNT,
            input_tokens=(150 * CHALLENGE_COUNT, 300 * CHALLENGE_COUNT),
            output_tokens=(2400, 3000),
        )
