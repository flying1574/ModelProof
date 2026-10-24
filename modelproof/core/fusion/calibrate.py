"""留一法标定。

可靠性权重不是拍脑袋定的，是算出来的：把某个模型的基准从候选集里
移走，拿它自己的观测去打分，看这条通道把真值排在第几位。排得越准，
reliability 越高。

基准库一变（新增模型、新增通道）就重跑一次：
    modelproof baseline recalibrate

输出直接写到 core/channels 各通道的 reliability，以及本目录的
thresholds.json。不允许手工改这些数字 —— 手改过的权重没有意义。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..baseline.store import BaselineStore
from ..channels.tokenizer import compare_counts
from ..types import LAYER_ORDER, TargetLayer

CALIBRATION_FILE = Path(__file__).resolve().parent / "thresholds.json"


def leave_one_out(
    store: BaselineStore, channel: str, layer: TargetLayer = "family"
) -> dict[str, Any]:
    """对某通道在指定层做留一法，返回统计结果。"""
    entries = [
        e for e in store.all()
        if (e.channels or {}).get(channel)
        and (e.channels or {}).get(channel, {}).get("enabled", True)
    ]
    if len(entries) < 3:
        return {
            "channel": channel,
            "layer": layer,
            "n": len(entries),
            "top1_rate": None,
            "note": "基准条目少于 3 条，无法标定",
        }

    top1 = 0
    reciprocal_ranks = 0.0
    margins: list[float] = []

    for held in entries:
        observed = held.channels[channel]
        candidates = [e for e in entries if e.model_id != held.model_id]
        scored: list[tuple[str, float]] = []

        for candidate in candidates:
            reference = candidate.channels[channel]
            score = _score_pair(channel, observed, reference)
            if score is None:
                continue
            scored.append((candidate.model_id, score))

        if not scored:
            continue
        scored.sort(key=lambda kv: kv[1], reverse=True)

        # 真值在「同层取值」意义下是否排第一
        truth = held.layer_value(layer)
        rank = None
        for index, (model_id, _score) in enumerate(scored, start=1):
            cand = store.get(model_id)
            if cand and cand.layer_value(layer) == truth:
                rank = index
                break
        if rank is None:
            rank = len(scored) + 1
        if rank == 1:
            top1 += 1
        reciprocal_ranks += 1.0 / rank
        if len(scored) >= 2:
            margins.append(scored[0][1] - scored[1][1])

    n = len(entries)
    return {
        "channel": channel,
        "layer": layer,
        "n": n,
        "top1_rate": round(top1 / n, 4) if n else None,
        "mrr": round(reciprocal_ranks / n, 4) if n else None,
        "mean_margin": round(sum(margins) / len(margins), 4) if margins else None,
        "suggested_reliability": _suggest(top1 / n if n else 0.0),
    }


def _score_pair(channel: str, observed: dict, reference: dict) -> float | None:
    if channel == "tokenizer":
        diff, _same = compare_counts(observed.get("counts") or {}, reference)
        if diff is None:
            return None
        return max(0.0, 1.0 - diff / 0.06)
    if channel == "behavior":
        from ..channels.behavior import compare_behavior

        matched, checked = compare_behavior(
            observed.get("vector") or {}, reference.get("vector") or {}
        )
        if checked == 0:
            return None
        return matched / checked
    if channel == "distribution":
        from ..channels.distribution import NEAR_DISTANCE, compare_distribution

        distance = compare_distribution(observed, reference)
        if distance is None:
            return None
        return max(0.0, 1.0 - distance / NEAR_DISTANCE)
    if channel == "onetoken":
        from ..channels.onetoken import (
            PROBE_VERSION as OT_VERSION,
            UNCERTAIN_JSD,
            _raw_lists,
            mean_jsd,
        )

        if (reference.get("probe_version") != OT_VERSION):
            return None
        left = observed.get("_raw") or _raw_lists(observed)
        value, _cells = mean_jsd(left, _raw_lists(reference))
        if value is None:
            return None
        return max(0.0, 1.0 - value / UNCERTAIN_JSD)
    return None


def _suggest(top1_rate: float) -> float:
    """把 top1 率映射成可靠性权重。硬信号上限更高。"""
    if top1_rate >= 0.95:
        return 1.00
    if top1_rate >= 0.85:
        return 0.85
    if top1_rate >= 0.70:
        return 0.55
    if top1_rate >= 0.55:
        return 0.40
    return 0.25


def _candidate_scores(
    held: Any, pool: list[Any], channels: tuple[str, ...]
) -> dict[str, float]:
    """把一条条目的观测和 pool 里每一条比一遍，按候选聚合各通道得分。

    多通道取平均：这样候选得分向量既用了硬信号也用了软信号，
    与 decide() 的融合口径一致（只是这里不做 softmax，要保留原始尺度）。
    """
    per_candidate: dict[str, list[float]] = {}
    for channel in channels:
        observed = (held.channels or {}).get(channel) or {}
        if not observed or observed.get("enabled") is False:
            continue
        for candidate in pool:
            reference = (candidate.channels or {}).get(channel) or {}
            if not reference:
                continue
            score = _score_pair(channel, observed, reference)
            if score is None:
                continue
            per_candidate.setdefault(candidate.model_id, []).append(score)
    return {
        mid: sum(values) / len(values)
        for mid, values in per_candidate.items()
        if values
    }


def fit_openset(
    store: BaselineStore,
    channels: tuple[str, ...] = ("tokenizer", "behavior", "distribution", "onetoken"),
) -> dict[str, Any]:
    """用留一法造出「库内 / 库外」两组样本，标定开集判据。

    这是 LLMmap 附录 E 里那个 50/50 平衡训练集的等价物，只是我们不需要
    外部数据 —— 基准库自己就能同时扮演两个角色：

      * **库内**：X 的观测 vs 包含 X 的库（自比，确定性通道会偏乐观）
      * **库外**：X 的观测 vs 去掉 X 的库（从候选池看，就是一个库外模型来了）

    产出两个阈值：
      * `z_floor`   峰度阈值（最佳候选比平均高几个 σ）
      * `top_floor` 绝对相似度阈值（最佳候选到底像不像）

    两个都失败才判「库外」，只失败一个判「ambiguous」——
    理由见 `openset.assess` 的文档。
    """
    from .openset import fit_threshold, vector_stats

    entries = list(store.all())
    if len(entries) < 4:
        return {
            "fitted": False,
            "note": f"基准条目只有 {len(entries)} 条，不足以标定开集判据（至少 4 条）",
        }

    seen_stats: list[dict[str, float]] = []
    unseen_stats: list[dict[str, float]] = []
    for held in entries:
        full = _candidate_scores(held, entries, channels)
        if len(full) < 2:
            continue
        others = [
            e for e in entries if e.model_id != held.model_id
        ]
        reduced = _candidate_scores(held, others, channels)
        if len(reduced) < 2:
            continue
        seen_stats.append(vector_stats(list(full.values())))
        unseen_stats.append(vector_stats(list(reduced.values())))

    if not seen_stats:
        return {"fitted": False, "note": "没有条目同时带可比的通道数据"}

    top_fit = fit_threshold(
        [s.get("max", 0.0) for s in seen_stats],
        [s.get("max", 0.0) for s in unseen_stats],
        label="top",
    )
    gap_fit = fit_threshold(
        [s.get("gap_ratio", 0.0) for s in seen_stats],
        [s.get("gap_ratio", 0.0) for s in unseen_stats],
        label="gap",
    )

    # 「库外时第二名落后多少」—— 用来决定哪些候选用经验预测集无法排除。
    # 取 90 分位而不是均值：宁可宽一点，也别把真正答案排除在外。
    from .openset import quantile

    gaps = sorted(float(s.get("gap") or 0.0) for s in unseen_stats if s)
    delta = quantile(gaps, 0.90) if gaps else None

    return {
        "fitted": bool(top_fit.get("fitted") and gap_fit.get("fitted")),
        # 阈值是按这一个库标定的，换库就失效 —— 调用方要核对
        "bank": store.bank,
        "baseline_count": store.count(),
        "top_floor": top_fit.get("floor"),
        "top_balanced_accuracy": top_fit.get("balanced_accuracy"),
        "gap_floor": gap_fit.get("floor"),
        "gap_balanced_accuracy": gap_fit.get("balanced_accuracy"),
        # 经验预测集的半宽：候选得分落在 top - delta 以内就无法排除
        "set_delta": round(delta, 4) if delta is not None else None,
        "seen_n": top_fit.get("seen_n"),
        "unseen_n": top_fit.get("unseen_n"),
        "note": (
            "库内组来自自比，对确定性通道（词表）偏乐观，"
            "balanced_accuracy 只能当上界；接近 0.5 说明库太小、"
            "形状判据分不开，此时开集结论应直接弃权。"
            "set_delta 是经验半宽，不构成共形预测的覆盖率保证 —— "
            "形式保证需要每个模型有多份独立观测，目前一条基准只存一份"
        ),
    }


def recalibrate(store: BaselineStore | None = None) -> dict[str, Any]:
    """跑全部可标定通道，写出 thresholds.json。"""
    store = store or BaselineStore()
    store.load(force=True)

    result: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "baseline_count": store.count(),
        "channels": {},
    }
    for channel in ("tokenizer", "behavior", "distribution", "onetoken"):
        for layer in ("family", "series", "version"):
            stats = leave_one_out(store, channel, layer)  # type: ignore[arg-type]
            result["channels"].setdefault(channel, {})[layer] = stats

    # 开集判据：与通道可靠性一起标定，因为两者都依赖同一个基准库
    result["openset"] = fit_openset(store)

    CALIBRATION_FILE.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return result


def load_calibration() -> dict[str, Any]:
    if not CALIBRATION_FILE.exists():
        return {}
    try:
        return json.loads(CALIBRATION_FILE.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}


def current_reliability(channel: str, fallback: float) -> float:
    """优先用标定值，没标定过时用通道自带的默认值。"""
    data = load_calibration()
    per_layer = (data.get("channels") or {}).get(channel) or {}
    best = None
    for layer in LAYER_ORDER:
        stats = per_layer.get(layer) or {}
        rate = stats.get("top1_rate")
        if rate is not None and (best is None or rate > best):
            best = rate
    if best is None:
        return fallback
    return _suggest(best)
