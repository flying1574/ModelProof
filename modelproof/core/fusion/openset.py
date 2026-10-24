"""开集识别：这个端点到底是「库里的某个模型」还是「库外的新东西」。

闭集归因有一个数学上绕不开的性质：**候选池里必须选一个**。所以库外模型
一定会被硬塞给最相似的候选，而且 softmax 还会给它一个看起来很自信的分数
（这正是 2026-10 那次「DeepSeek 蒸馏 GPT-6 Astra」误判的机制，见
DESIGN.md 附录 G）。

解法在机器学习里是成熟的一套东西：open-set recognition（开集识别）。

**依据来自 LLMmap（USENIX Security '25，arXiv 2407.15847）附录 E**：
他们发现「最近距离」这个单一阈值的尺度随模型而变，**固定阈值不可靠**；
但距离向量的**形状**很稳定 —— 库内模型的最佳匹配一枝独秀，库外模型则
一片扁平。于是用 mean / min / max / variance / std / entropy 六个统计量
喂一个随机森林判别 unseen，平均准确率 82.3%。

我们照搬这组统计量，但**不训分类器**，原因有二：
  1. 没有带标注的库外样本（真正的库外模型长什么样，取决于你没采过谁）；
  2. 几十条基准上训出来的分类器是过拟合，不是能力。

所以本模块产出的是**可读的判据**，不是伪装成概率的分数：

    top     最佳候选的绝对相似度      —— 它到底像不像
    z       最佳候选比其余突出几个 σ  —— 它是不是一枝独秀
    entropy 候选得分的归一化熵        —— 整片分数有多平

判据要**两个都看**：库外模型也可能有突出的近邻（它像某个模型，只是不是
那个模型），所以单看 z 会把「像但不等于」误判成「在库里」。

三个概念要分清（与附录 G 一致）：

  1. **闭集判定**：在库内选最像的 —— 一定给答案，可能给错。
  2. **开集识别**：先问「它在不在库里」 —— 说不准时明确弃权。
  3. **预测集**：给一组候选 + 覆盖率保证 —— 比单点标签更诚实。
"""

from __future__ import annotations

import math
from typing import Any

# 未标定时的保守默认值。标定过之后以 thresholds.json 里的值为准。
# top_floor：最佳相似度低于此值，无论多突出都当不了「库内」。
DEFAULT_TOP_FLOOR = 0.85
# gap_floor：最佳候选比次佳高出不到这个比例，就算「没有明显赢家」。
DEFAULT_GAP_FLOOR = 0.10

# 预测集的默认覆盖率：在「模型确实在库里」的前提下，
# 真实答案有 90% 的概率落在集合内。
DEFAULT_COVERAGE = 0.90


def vector_stats(scores: list[float]) -> dict[str, float]:
    """候选得分向量的形状统计 —— 对齐 LLMmap 附录 E 的六个量。

    注意：LLMmap 用的是**距离**（越小越近），我们用**相似度**（越大越近），
    所以 min/max 的语义正好相反。保留同样的名字是为了让人能对照原论文。
    """
    values = [float(v) for v in scores if v is not None]
    n = len(values)
    if n == 0:
        return {}
    total = sum(values)
    mean = total / n
    top = max(values)
    low = min(values)
    variance = (
        sum((v - mean) ** 2 for v in values) / n if n >= 2 else 0.0
    )
    std = math.sqrt(variance)

    # 归一化熵：候选得分有多「平」。全相等 → 1；一枝独秀 → 接近 0。
    entropy = 0.0
    if n >= 2 and std > 1e-12:
        exps = [math.exp((v - mean) / std) for v in values]
        norm = sum(exps)
        for e in exps:
            p = e / norm
            if p > 0:
                entropy -= p * math.log(p)
        entropy /= math.log(n)

    stats = {
        "n": float(n),
        "mean": round(mean, 6),
        "min": round(low, 6),
        "max": round(top, 6),
        "variance": round(variance, 6),
        "std": round(std, 6),
        "entropy": round(max(0.0, min(1.0, entropy)), 6),
    }
    # 峰度：最佳候选比平均高出几个标准差。
    # 注意它**随候选数增长**（n 个候选时最大约 sqrt(n-1)），
    # 所以不能直接当跨库可比的判据，只用于报告与对照原论文。
    stats["z"] = round((top - mean) / std, 4) if std > 1e-9 else 0.0
    stats["z_norm"] = (
        round(min(1.0, stats["z"] / math.sqrt(n - 1)), 4) if n >= 2 else 0.0
    )
    # 相对领先度：最佳比次佳高出的比例。尺度无关、与候选数基本无关，
    # 这是本模块的主判据之一。
    if n >= 2:
        ordered = sorted(values, reverse=True)
        stats["gap"] = round(ordered[0] - ordered[1], 6)
        stats["gap_ratio"] = (
            round((ordered[0] - ordered[1]) / abs(ordered[0]), 4)
            if abs(ordered[0]) > 1e-9
            else 0.0
        )
    else:
        stats["gap"] = 0.0
        stats["gap_ratio"] = 0.0
    return stats


def assess(
    stats: dict[str, float],
    top_floor: float | None = None,
    gap_floor: float | None = None,
) -> dict[str, Any]:
    """由向量形状给出开集档位，**不冒充概率**。

    三档：
      * `in_bank`        最佳候选又像又领先 —— 库里有对应条目
      * `likely_unseen`  两个条件都不满足 —— 候选池里大概没有正确答案
      * `ambiguous`      只有一个满足 —— 像但不够领先，或领先但绝对水平低

    `ambiguous` 是刻意保留的：把「像但不等于」判成库内是误判的来源，
    判成库外又可能冤枉一个好模型。说不准就说说不准。

    为什么不用 `z` 当判据：`z` 的上限随候选数增长（n 个候选时约
    sqrt(n−1)），同一个 z 在 4 个候选的小库里很突出、在 100 个候选的
    大库里很平常。`gap_ratio`（最佳比次佳高出的比例）才是跨库可比的。
    """
    if not stats:
        return {
            "verdict": "unknown",
            "reasons": ["没有候选得分向量，无法做开集判断"],
            "stats": {},
        }

    top = float(stats.get("max") or 0.0)
    gap = float(stats.get("gap_ratio") or 0.0)
    floor_top = DEFAULT_TOP_FLOOR if top_floor is None else top_floor
    floor_gap = DEFAULT_GAP_FLOOR if gap_floor is None else gap_floor

    top_ok = top >= floor_top
    gap_ok = gap >= floor_gap

    reasons: list[str] = [
        f"最佳相似度 {top:.3f}（线 {floor_top:.3f}）—— "
        + ("够像" if top_ok else "不够像"),
        f"相对领先度 {gap:.3f}（线 {floor_gap:.3f}）—— "
        + ("有明显赢家" if gap_ok else "和次佳拉不开"),
    ]

    if top_ok and gap_ok:
        verdict = "in_bank"
    elif not top_ok and not gap_ok:
        verdict = "likely_unseen"
    else:
        verdict = "ambiguous"
        if top_ok and not gap_ok:
            reasons.append(
                "像是像，但和次佳几乎并列 —— 更像「同族的另一个模型」，"
                "需要更强通道或更多重复才能分开"
            )
        else:
            reasons.append(
                "虽然领先，但绝对水平低 —— 候选池里可能只是「最不差的」"
            )

    return {"verdict": verdict, "reasons": reasons, "stats": stats}


def quantile(values: list[float], coverage: float) -> float | None:
    """共形预测用的经验分位数 ceil((n+1)(1-α))/n（有限样本修正）。"""
    finite = sorted(float(v) for v in values if v is not None)
    n = len(finite)
    if n == 0:
        return None
    level = math.ceil((n + 1) * coverage) / n
    if level >= 1.0:
        return finite[-1]
    position = level * (n - 1)
    lower = int(math.floor(position))
    upper = min(n - 1, lower + 1)
    weight = position - lower
    return finite[lower] * (1 - weight) + finite[upper] * weight


def conformal_set(
    ranked: list[tuple[str, float]], qhat: float
) -> list[tuple[str, float]]:
    """按共形预测构造**预测集**。

    `ranked` 是 [(取值, 非一致性分数)]，分数越小越像。
    `qhat` 是标定得到的阈值：保留所有 score ≤ qhat 的候选。

    覆盖率保证有严格前提：**测试对象必须与标定样本来自同一分布**
    （也就是「模型确实在库里」）。库外模型不适用 —— 这正是为什么
    开集识别必须与预测集配合使用，而不能只给预测集。
    """
    if not ranked:
        return []
    chosen = [(value, score) for value, score in ranked if score <= qhat]
    # 保底：一个都进不了说明标定数据太薄，至少给出最近邻
    return chosen or [ranked[0]]


def fit_threshold(
    seen_values: list[float], unseen_values: list[float], label: str = "metric"
) -> dict[str, Any]:
    """在一维判据上找把「库内/库外」分得最开的切点（最大平衡准确率）。

    两组样本由 `calibrate.fit_openset` 用留一法造出来：
      * 库内组：X 的观测 vs **包含 X** 的库（自比，对确定性通道偏乐观）
      * 库外组：X 的观测 vs **去掉 X** 的库 —— 从候选池角度，
        这正是「一个库外模型来了」的样子

    判据都是「越大越像库内」，所以规则统一为 `value >= cut`。

    注意 `seen_values` 对确定性通道（词表）是乐观的：自比得分恒为满分。
    所以 `balanced_accuracy` 只能当**上界**看，不能当承诺。
    """
    if not seen_values or not unseen_values:
        return {
            "fitted": False,
            "name": label,
            "floor": (
                DEFAULT_TOP_FLOOR if label == "top" else DEFAULT_GAP_FLOOR
            ),
            "note": "缺少库内或库外样本，沿用默认阈值",
        }

    cuts = sorted(
        {round(v, 3) for v in list(seen_values) + list(unseen_values)}
    )
    best: tuple[float, float] | None = None
    for cut in cuts:
        tp = sum(1 for v in seen_values if v >= cut)
        tn = sum(1 for v in unseen_values if v < cut)
        balanced = 0.5 * (tp / len(seen_values) + tn / len(unseen_values))
        if best is None or balanced > best[1]:
            best = (cut, balanced)

    cut, balanced = best  # type: ignore[misc]
    return {
        "fitted": True,
        "name": label,
        "floor": round(cut, 4),
        "balanced_accuracy": round(balanced, 4),
        "seen_n": len(seen_values),
        "unseen_n": len(unseen_values),
        "note": (
            "库内组来自自比，对确定性通道偏乐观，balanced_accuracy 只能当上界；"
            "接近 0.5 说明这个库太小、形状判据分不开，此时开集结论应直接弃权"
        ),
    }
