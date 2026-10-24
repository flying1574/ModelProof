"""ModelTrace 稳健数字指纹打分包 —— 纯标准库移植。

为什么单独放一个模块：ModelProof 自己的 distribution 通道用的是「裸 Hellinger
距离 + 有序块」，距离阈值是占位值，没有经过标定，所以给不出能当概率用的数。
ModelTrace 在 18 个模型 × 12 种提示词环境（756 条真实回答）上训练了一套
带**干扰方向剔除**的判别器：

  1. 先把 355 维边际特征 / 74 维有序块特征按训练集均值方差 z 标准化；
  2. 减掉前 2 个「干扰基」（nuisance basis）——这两个方向是提示词措辞造成的
     漂移，不是模型本身的区别，剔掉后同一模型在不同问法下的向量才聚得起来；
  3. 归一化后与 18 个类心做内积，得到 18 个 z 分数，再 standardize；
  4. 边际 0.75 + 有序块 0.25 融合，多条回答取平均；
  5. 乘上分组交叉验证标定出的 beta，softmax 出概率。

它带来的两样东西是裸距离给不了的：

  * **校准过的概率**：beta 是按回答条数分组、用留组交叉验证定出来的，
    1/2/3 条的 CV 准确率分别是 0.958 / 0.998 / 1.000——这三组数字直接写进
    报告，让人知道这个百分数值几分信任。
  * **库外守卫**：`best_score < OUT_OF_SCOPE_SCORE` 即判「不在库内」。
    ModelTrace 自己的常量是 1.8。这一条至关重要——闭集归因一定会把库外
    模型硬塞给最像的候选，没有守卫就会造出附录 G 那种误判。

边界（必须写清楚，别拿它当万能钥匙）：
  * 这包只在 18 个 GPT / Claude 模型上训练过。**它不知道 Qwen、DeepSeek、
    Gemini 长什么样**，所以遇到库外模型只能靠守卫弃权，不能靠它辨认。
  * OUT_OF_SCOPE_SCORE=1.8 是在 18 个候选的尺度上定的。候选数变了，
    standardize 后的最大值分布也会变——所以本模块同时把 `top`（原始相似度）
    与 `gap_ratio` 报出去，交给 fusion/openset.py 做尺度无关的复核。
  * 移植必须与 ModelTrace 原版逐位一致，tests/test_robust_port.py 用 numpy
    跑原版实现比对，core 层仍然零第三方依赖。
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

# 与 ModelTrace/fingerprint.py 保持一致。
OUT_OF_SCOPE_SCORE = 1.8
MIN_MODELS_FOR_SCOPE = 8
MAX_CALIBRATED_QUERIES = 3
ALPHA = 0.5

PACK_FILENAME = "robust_pack.json"
PACK_DIR = Path(__file__).resolve().parents[3] / "baselines" / "fingerprints"

_pack_cache: dict[str, Any] | None = None


# --------------------------------------------------------------------------
# 基础运算
# --------------------------------------------------------------------------


def standardize(values: list[float]) -> list[float]:
    """总体标准化（除以 n，不是 n-1）——与 ModelTrace 一致，别改成分样估计。"""
    if not values:
        return []
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / len(values)
    scale = max(math.sqrt(variance), 1e-12)
    return [(value - mean) / scale for value in values]


def _l2(values: list[float]) -> float:
    return math.sqrt(sum(v * v for v in values))


def _dot(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right))


def _remove_nuisance(projected: list[float], basis: list[list[float]]) -> list[float]:
    """减掉干扰基张成的子空间。要求 basis 各行已正交归一（训练时如此）。"""
    if not basis:
        return projected
    out = list(projected)
    for axis in basis:
        coefficient = _dot(projected, axis)
        if coefficient == 0.0:
            continue
        for i in range(len(out)):
            out[i] -= coefficient * axis[i]
    return out


# --------------------------------------------------------------------------
# 打包读写
# --------------------------------------------------------------------------


def load_pack(path: str | Path | None = None) -> dict[str, Any] | None:
    """读取稳健打分包。不存在就返回 None——没有包就走裸距离，不硬凑。"""
    global _pack_cache
    target = Path(path) if path else PACK_DIR / PACK_FILENAME
    if path is None and _pack_cache is not None:
        return _pack_cache
    if not target.is_file():
        return None
    try:
        pack = json.loads(target.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    if path is None:
        _pack_cache = pack
    return pack


def pack_models(pack: dict[str, Any]) -> list[str]:
    return list(pack.get("model_order") or [])


def covers_bank(pack: dict[str, Any], baselines: list[Any]) -> bool:
    """这个打分包能不能用在当前基准库上。

    打分包永远对**它自己那 18 个类**打分，不管库里有什么。如果库里没有
    这些模型，那些候选就映射不到基准条目、退化成「拿 ID 当取值」，
    既污染候选池又污染归一化。所以要求 18 个类**全部**能对上
    （model_id 或别名）—— 用自定义库测 mock 模型时，这条会挡住打分包，
    老老实实退回裸距离。
    """
    model_order = pack_models(pack)
    if not model_order:
        return False
    known: set[str] = set()
    for entry in baselines or []:
        known.add(str(getattr(entry, "model_id", "") or ""))
        for alias in (getattr(entry, "aliases", None) or []):
            known.add(str(alias))
    return all(model in known for model in model_order)


# --------------------------------------------------------------------------
# 打分
# --------------------------------------------------------------------------


def hellinger_scores(
    counts: list[int], pack: dict[str, Any]
) -> tuple[list[float], list[float]]:
    """(融合分, 纯边际分)。feature 用 sqrt((c+0.5)/total)。"""
    artifact = pack["robust"]["hellinger"]
    # 注意：_unit_sqrt 内部已经加 ALPHA 平滑，这里只能传原始计数，不能预先加。
    feature = _unit_sqrt([float(v) for v in counts])
    mean = artifact["feature_mean"]
    scale = artifact["feature_scale"]
    projected = [
        (feature[i] - mean[i]) / (scale[i] if scale[i] else 1e-12)
        for i in range(len(feature))
    ]
    projected = _remove_nuisance(projected, artifact.get("nuisance_basis") or [])
    norm = max(_l2(projected), 1e-12)
    unit = [v / norm for v in projected]
    centroids = artifact["centroids"]
    scores = [_dot(unit, centroid) for centroid in centroids]
    standardized = standardize(scores)
    return standardized, standardized


def ordered_block_scores(numbers: list[int], pack: dict[str, Any]) -> list[float]:
    artifact = pack["robust"]["ordered_blocks"]
    feature = ordered_block_feature(numbers)
    mean = artifact["feature_mean"]
    scale = artifact["feature_scale"]
    standardized_feature = [
        (feature[i] - mean[i]) / (scale[i] if scale[i] else 1e-12)
        for i in range(len(feature))
    ]

    norm = max(_l2(standardized_feature), 1e-12)
    normalized = [v / norm for v in standardized_feature]

    # 模板支路：12 种提示词环境各自的类心，取最像的那一组
    template_scores: list[float] | None = None
    for centroids in artifact.get("environment_centroids") or []:
        scores = [_dot(normalized, centroid) for centroid in centroids]
        if template_scores is None:
            template_scores = scores
        else:
            template_scores = [max(a, b) for a, b in zip(template_scores, scores)]
    template = standardize(template_scores) if template_scores else None

    projected = _remove_nuisance(
        standardized_feature, artifact.get("nuisance_basis") or []
    )
    norm = max(_l2(projected), 1e-12)
    unit = [v / norm for v in projected]
    centroids = artifact["centroids"]
    nuisance = standardize([_dot(unit, centroid) for centroid in centroids])

    if template is None:
        return nuisance
    return standardize(
        [0.5 * t + 0.5 * n for t, n in zip(template, nuisance)]
    )


def score_numbers(numbers: list[int], pack: dict[str, Any]) -> dict[str, Any]:
    """一份整数样本的 18 类分数。与 ModelTrace `robust_score_numbers` 对齐。"""
    counts = count_numbers(numbers)
    marginal, _ = hellinger_scores(counts, pack)
    artifact = pack["robust"].get("ordered_blocks")
    weight = float(artifact.get("weight", 0.0)) if artifact else 0.0
    if not artifact or weight == 0.0:
        return {"fused": marginal, "marginal": marginal, "ordered": marginal}
    ordered = ordered_block_scores(numbers, pack)
    if len(ordered) != len(marginal):
        return {"fused": marginal, "marginal": marginal, "ordered": marginal}
    fused = [
        (1.0 - weight) * m + weight * o for m, o in zip(marginal, ordered)
    ]
    return {"fused": fused, "marginal": marginal, "ordered": ordered}


def softmax(values: list[float]) -> list[float]:
    if not values:
        return []
    maximum = max(values)
    weights = [math.exp(value - maximum) for value in values]
    total = sum(weights)
    if total <= 0:
        return [1.0 / len(values)] * len(values)
    return [w / total for w in weights]


def analyze(
    samples: list[list[int]], pack: dict[str, Any]
) -> dict[str, Any]:
    """多条样本合起来给结论。与 ModelTrace `analyze_outputs` 对齐。"""
    model_ids = pack_models(pack)
    if not model_ids or not samples:
        return {"available": False, "reason": "no_pack_or_samples"}

    per_sample = [score_numbers(numbers, pack) for numbers in samples]
    width = len(model_ids)
    combined = [
        sum(item["fused"][i] for item in per_sample) / len(per_sample)
        for i in range(width)
    ]
    marginal_mean = [
        sum(item["marginal"][i] for item in per_sample) / len(per_sample)
        for i in range(width)
    ]

    key = str(min(len(samples), MAX_CALIBRATED_QUERIES))
    calibration = pack.get("calibration") or {}
    entry = calibration.get(key) or {}
    beta = float(entry.get("beta", 1.0))
    probabilities = softmax([beta * value for value in combined])

    order = sorted(range(width), key=lambda i: -probabilities[i])
    best_score = max(combined)
    second = sorted(combined, reverse=True)[1] if width >= 2 else 0.0
    scope_checkable = width >= MIN_MODELS_FOR_SCOPE

    return {
        "available": True,
        "prediction": model_ids[order[0]],
        "probability": probabilities[order[0]],
        "best_score": best_score,
        "second_score": second,
        "gap": best_score - second,
        "out_of_scope": (best_score < OUT_OF_SCOPE_SCORE) if scope_checkable else None,
        "scope_checkable": scope_checkable,
        "samples": len(samples),
        "extrapolated": len(samples) > MAX_CALIBRATED_QUERIES,
        "calibration": {
            "queries": key,
            "beta": beta,
            "cv_accuracy": entry.get("cv_accuracy"),
            "cv_samples": entry.get("cv_samples"),
            "fallback": bool(entry.get("fallback", False)),
        },
        # 尺度无关的复核量：候选数变了 z 的尺度就变，这两个不会。
        "gap_ratio": (
            (best_score - second) / abs(best_score) if abs(best_score) > 1e-9 else 0.0
        ),
        "results": [
            {
                "model": model_ids[i],
                "probability": probabilities[i],
                "score": combined[i],
                "marginal_score": marginal_mean[i],
            }
            for i in order
        ],
        "method": (pack.get("method") or {}).get("name", "Ordered-block + nuisance-Hellinger"),
    }


# --------------------------------------------------------------------------
# 特征（与 ModelProof distribution.py 共用同一套数学，保持同步）
# --------------------------------------------------------------------------


def _unit_sqrt(values: list[float], smoothing: float = ALPHA) -> list[float]:
    total = sum(values) + smoothing * len(values)
    if total <= 0:
        return [0.0] * len(values)
    return [math.sqrt((v + smoothing) / total) for v in values]


def _array_split_sizes(length: int, parts: int) -> list[int]:
    """numpy.array_split 的切分语义：余数摊到前面几段。

    必须与 ModelTrace 一致——它用 np.array_split(values, 4) 切块。
    早先 ModelProof 用的是 ceil 切法（[55,55,55,53]），与 array_split
    （[55,55,54,54]）差一个元素，z 标准化后足以改变分值。
    """
    base, extra = divmod(length, parts)
    return [base + (1 if i < extra else 0) for i in range(parts)]


def ordered_block_feature(numbers: list[int]) -> list[float]:
    """4 段 × 16 桶 + 末位数字 10 桶 = 74 维。"""
    pieces: list[float] = []
    start = 0
    for size in _array_split_sizes(len(numbers), 4):
        chunk = numbers[start:start + size]
        start += size
        bins = [0.0] * 16
        for value in chunk:
            slot = int((value - 1) / 355 * 16)
            bins[min(15, max(0, slot))] += 1.0
        pieces.extend(_unit_sqrt(bins))
    last = [0.0] * 10
    for value in numbers:
        last[value % 10] += 1.0
    pieces.extend(_unit_sqrt(last))
    return pieces


def count_numbers(numbers: list[int]) -> list[int]:
    counts = [0] * 355
    for value in numbers:
        if 1 <= value <= 355:
            counts[value - 1] += 1
    return counts
