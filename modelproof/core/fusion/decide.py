"""分层判定 / 冲突检测 / 弃权。

三条纪律在这里兑现：
  1. 每层独立判定，通道不许越层发言。
  2. 达不到阈值就弃权，绝不硬猜一个。
  3. 通道打架时记录冲突 —— 冲突往往是最有价值的发现（模型替换、套壳），
     而不是要被平滑掉的噪声。
"""

from __future__ import annotations

import math
from typing import Any

from ..baseline.provenance import tier_weight
from ..types import (
    DEFAULT_LAYER_BETAS,
    DEFAULT_LAYER_THRESHOLDS,
    Candidate,
    Conflict,
    Evidence,
    LAYER_ORDER,
    LayerVerdict,
    TargetLayer,
)

# 冲突惩罚系数
PENALTY_HARD = 0.4     # 两条硬信号互斥：极可能是上游做了模型替换
PENALTY_SOFT = 0.6     # 硬信号与软信号互斥
PENALTY_IGNORE = 1.0   # 软信号之间互斥：忽略

# 硬信号通道（改不了的那几类）
HARD_CHANNELS = {"tokenizer", "backend_fp", "distribution"}

# 一条证据在某层要发言，必须在该层有区分力。
# 若它对所有候选给分几乎相同（比如 behavior 问了两个模型回答一样的问题），
# 它在这一层就是无信息的 —— 让它发言只会稀释硬信号，还会被误判成冲突。
MIN_SPREAD = 0.05

# 相对候选地板：低于「本条证据最高分 × 这个比例」的候选不进假设空间。
# 为什么需要它：softmax 对候选**个数**敏感。18 个候选里 1 个满分、17 个
# 1e-7，层内 softmax 会把满分稀释到 0.28，version 层阈值 0.70 永远过不去
# （2026-10-18 在 gpt-5.4 真实参考样本上实测到）。那些 1e-7 不是「很像但
# 排第二」，是「这条证据根本没指向它」，不该参与归一化。
# 用相对值而不是绝对值：证据整体很弱时（最高分 0.001）不能把候选全砍光。
CANDIDATE_FLOOR_RATIO = 0.02


def decide(
    evidence: list[Evidence],
    baselines: list[Any],
    target_layer: TargetLayer = "family",
    thresholds: dict[TargetLayer, float] | None = None,
    betas: dict[TargetLayer, float] | None = None,
) -> tuple[dict[TargetLayer, LayerVerdict], list[Conflict]]:
    """对各层独立判定，返回 (层结论, 冲突列表)。"""
    thresholds = thresholds or DEFAULT_LAYER_THRESHOLDS
    betas = betas or DEFAULT_LAYER_BETAS

    # 只保留参与身份打分的证据（gateway 不在内）
    scored = [e for e in evidence if e.weight > 0 and e.candidates]

    # 目标层之上的层都判；超过 target_layer 的不判（用户没要求）
    wanted = list(LAYER_ORDER[: LAYER_ORDER.index(target_layer) + 1])

    verdicts: dict[TargetLayer, LayerVerdict] = {}
    conflicts: list[Conflict] = []

    for layer in wanted:
        # 先算每条证据在该层的取值分数，并剔除「这一层分不出来」的证据。
        # 分不出来的证据不该发言：它既无法支持任何候选，又会被误判成冲突。
        per_evidence: list[tuple[Evidence, dict[str, float]]] = []
        skipped: list[str] = []
        for item in scored:
            if not _supports(item, layer):
                continue
            scores = dict(_layer_candidates(item, layer, baselines))
            if not scores:
                continue
            if len(scores) >= 2:
                ordered = sorted(scores.values(), reverse=True)
                if ordered[0] - ordered[1] < MIN_SPREAD:
                    skipped.append(item.channel)
                    continue
            per_evidence.append((item, scores))

        if not per_evidence:
            verdicts[layer] = LayerVerdict(
                abstained=True,
                abstain_reason="no_discriminating_evidence",
                detail=(
                    f"{layer} 层没有能区分候选的证据"
                    + (f"（{('、').join(skipped)} 在该层分不出差异）" if skipped else "")
                ),
                threshold=thresholds[layer],
            )
            continue

        # 计算每个层取值的加权原始分
        raw: dict[str, float] = {}
        weight_sum: dict[str, float] = {}
        for item, scores in per_evidence:
            for value, score in scores.items():
                raw[value] = raw.get(value, 0.0) + item.weight * score
                weight_sum[value] = weight_sum.get(value, 0.0) + item.weight

        if not raw:
            verdicts[layer] = LayerVerdict(
                abstained=True,
                abstain_reason="no_candidate",
                detail="证据未能映射到任何基准条目",
                threshold=thresholds[layer],
            )
            continue

        for value in raw:
            raw[value] = raw[value] / max(1e-9, weight_sum[value])

        # 层内 softmax
        beta = betas.get(layer, 6.0)
        probabilities = _softmax(raw, beta)

        # 冲突检测（同样只考虑有区分力的证据）
        layer_conflicts = _detect_conflicts(layer, per_evidence)
        conflicts.extend(layer_conflicts)
        penalty = 1.0
        if layer_conflicts:
            penalty = min(c.penalty for c in layer_conflicts)
            # 冲突时把该层证据数记下来，报告里要说明
        for value in probabilities:
            probabilities[value] *= penalty

        ranked = sorted(probabilities.items(), key=lambda kv: kv[1], reverse=True)
        top_value, top_p = ranked[0]
        threshold = thresholds[layer]

        top_candidates = [Candidate(v, p) for v, p in ranked[:5]]

        if top_p >= threshold:
            verdicts[layer] = LayerVerdict(
                value=top_value,
                confidence=round(top_p, 4),
                abstained=False,
                top_candidates=top_candidates,
                threshold=threshold,
                detail=_detail(layer, [e for e, _ in per_evidence], top_value, ranked),
            )
        else:
            verdicts[layer] = LayerVerdict(
                abstained=True,
                abstain_reason="top_score_below_threshold",
                top_candidates=top_candidates,
                threshold=threshold,
                detail=(
                    f"最高分 {top_p:.2f} < 阈值 {threshold:.2f}；"
                    f"{_why_close(ranked)}"
                ),
            )

    return verdicts, conflicts


def _supports(evidence: Evidence, layer: TargetLayer) -> bool:
    """这条证据能不能在该层发言。yields 里没写的层不许说话。"""
    if evidence.channel in ("gateway",):
        return False
    # 证据自带的 target_layer 是我们信任的声明
    if evidence.target_layer is None:
        return False
    order = {layer: i for i, layer in enumerate(LAYER_ORDER)}
    declared_i = order.get(evidence.target_layer)
    layer_i = order.get(layer)
    if declared_i is None or layer_i is None:
        return False
    # 允许在「自己能证明的层及更粗的层」发言，不允许越细
    return layer_i <= declared_i


def _layer_candidates(
    evidence: Evidence, layer: TargetLayer, baselines: list[Any]
) -> list[tuple[str, float]]:
    """把证据里的 model_id 候选映射成该层的取值。

    零分候选不进假设空间。这不是为了好看：层内 softmax 对**候选个数**敏感，
    18 个候选里 17 个是 0 分，会把 1 个满分稀释到 0.28，version 层阈值 0.70
    就永远过不去（2026-10-18 在 gpt-5.4 的真实参考样本上实测到的）。
    一个没拿到任何支持的假设，本来就不该参与归一化 —— 它不是「很像但排第二」，
    它是「这条证据根本没指向它」。
    """
    # 别名也要进索引：打分包用的是 ModelTrace 的 ID（claude-sonnet-5-5），
    # 库里的条目是 claude-sonnet-5.5。只按 model_id 查会让整批 Claude 候选
    # 查不到条目、退化成「拿 ID 当取值」，vendor / family 层直接判不出来。
    index: dict[str, Any] = {}
    for entry in baselines:
        index.setdefault(entry.model_id, entry)
        for alias in (entry.aliases or []):
            index.setdefault(alias, entry)

    peak = max((c.score for c in evidence.candidates), default=0.0)
    floor = peak * CANDIDATE_FLOOR_RATIO

    results: dict[str, float] = {}
    for candidate in evidence.candidates:
        if candidate.score <= 0.0 or candidate.score < floor:
            continue
        entry = index.get(candidate.id)
        if entry is None:
            # 没有基准条目时退化：直接用 model_id 当取值
            value = candidate.id
            weight = 0.5
        else:
            value = entry.layer_value(layer)
            if not value:
                continue
            weight = tier_weight(entry.provenance.tier)
        score = candidate.score * weight
        if value not in results or score > results[value]:
            results[value] = score
    return list(results.items())


def _softmax(raw: dict[str, float], beta: float) -> dict[str, float]:
    if not raw:
        return {}
    # 候选少的时候 softmax 会过于自信，用候选数做一次温和的稀释
    diluted = beta / max(1.0, math.log2(len(raw) + 1))
    peak = max(raw.values())
    exponentials = {k: math.exp(diluted * (v - peak)) for k, v in raw.items()}
    total = sum(exponentials.values())
    return {k: v / total for k, v in exponentials.items()}


def _detect_conflicts(
    layer: TargetLayer,
    per_evidence: list[tuple[Evidence, dict[str, float]]],
) -> list[Conflict]:
    """同一层里，不同通道的 top-1 取值不一致即为冲突。

    只考虑在该层有区分力的证据（调用方已过滤），否则一条「怎么都行」
    的软信号会被当成和硬信号唱反调。
    """
    per_channel: dict[str, str] = {}
    for item, scores in per_evidence:
        top = max(scores.items(), key=lambda kv: kv[1])
        per_channel[item.channel] = top[0]

    if len(set(per_channel.values())) <= 1:
        return []

    # 按通道分组，取代表性取值
    groups: dict[str, list[str]] = {}
    for channel, value in per_channel.items():
        groups.setdefault(value, []).append(channel)

    if len(groups) < 2:
        return []

    sorted_groups = sorted(groups.items(), key=lambda kv: -len(kv[1]))
    values = [v for v, _ in sorted_groups[:2]]
    channels = [c for _, cs in sorted_groups[:2] for c in cs]
    a_hard = any(c in HARD_CHANNELS for c in sorted_groups[0][1])
    b_hard = any(c in HARD_CHANNELS for c in sorted_groups[1][1])

    if a_hard and b_hard:
        severity, penalty = "hard", PENALTY_HARD
        hint = "两条硬信号互斥，极可能是上游做了模型替换"
    elif a_hard or b_hard:
        severity, penalty = "soft", PENALTY_SOFT
        hint = "硬信号与软信号不一致，通常是自述或包装层失真"
    else:
        # 软信号之间打架：不惩罚
        return []

    return [
        Conflict(
            layer=layer,
            channels=channels,
            values=values,
            severity=severity,
            penalty=penalty,
            hint=hint,
        )
    ]


def _detail(
    layer: TargetLayer,
    evidence: list[Evidence],
    top_value: str,
    ranked: list[tuple[str, float]],
) -> str:
    contributors = [
        f"{e.channel}(w={e.weight:.2f})" for e in evidence if e.weight > 0
    ]
    runner_up = ranked[1][1] if len(ranked) > 1 else 0.0
    return (
        f"{len(evidence)} 条证据（{'、'.join(contributors)}）指向 {top_value}；"
        f"次优 {runner_up:.2f}"
    )


def _why_close(ranked: list[tuple[str, float]]) -> str:
    if len(ranked) < 2:
        return "候选不足"
    top, second = ranked[0], ranked[1]
    gap = top[1] - second[1]
    if gap < 0.05:
        return (
            f"{top[0]}({top[1]:.2f}) 与 {second[0]}({second[1]:.2f}) 几乎并列，"
            f"需补充更强通道才能定"
        )
    return f"{top[0]} 领先但低于阈值，样本或通道不足"
