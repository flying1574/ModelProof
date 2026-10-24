"""C2 能力悬崖。

同一系列的不同版本，能力不是平滑变化的——某些任务上会出现台阶：
上一个版本还答得上来，下一个版本突然答不上来（或反过来）。
这种「在哪几个任务上掉下去」的模式比总分更有身份信息：
总分会被采样温度和 prompt 措辞冲掉，掉下去的位置不会。

探针全部**可自动判分**（不用 LLM 当裁判），所以结果可复现。
6 条探针构成一个 6 位能力向量，比对用重合率（Hamming 相似度）。

需要说清楚的限制：
  * 能力会随上游负载、量化、截断变化，波动比词表指纹大；
  * 所以它是软信号（reliability 0.35），只用于同族内区分，不能单独定论；
  * 建议 temperature=0，并在同一时段内比较。
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable

from .base import BaseChannel

PROBE_VERSION = "cp1-6"


def _has_number(text: str, *values: float) -> bool:
    return any(re.search(rf"(?<!\d){re.escape(str(v))}(?!\d)", text) for v in values)


def _check_arithmetic(text: str) -> bool:
    """((17 × 23) − 91) ÷ 6 = 50"""
    return _has_number(text, 50)


def _check_letter_count(text: str) -> bool:
    """strawberry 里 r 出现 3 次"""
    return _has_number(text, 3)


def _check_sort(text: str) -> bool:
    return bool(re.search(r"3\s*[、,，]\s*7\s*[、,，]\s*19\s*[、,，]\s*42\s*[、,，]\s*61\s*[、,，]\s*88", text))


def _check_json(text: str) -> bool:
    try:
        start = text.index("{")
        end = text.rindex("}") + 1
        payload = json.loads(text[start:end])
    except (ValueError, TypeError):
        return False
    return all(key in payload for key in ("name", "age", "city"))


def _check_logic(text: str) -> bool:
    """「有些 A 是 C」不必然成立 → 否"""
    head = text.strip()[:40]
    return ("否" in head or "不" in head or "no" in head.lower()) and "是" not in head[:2]


def _check_constraint(text: str) -> bool:
    """含『风』、不含『春天』、长度 ≤ 20"""
    return "风" in text and "春天" not in text and len(text.strip()) <= 20


PROBES: list[dict[str, Any]] = [
    {
        "id": "arithmetic",
        "label": "多步算术",
        "prompt": "请直接计算并把结果写成一个数字：((17 × 23) − 91) ÷ 6 = ?",
        "check": _check_arithmetic,
    },
    {
        "id": "letter_count",
        "label": "字母计数",
        "prompt": "单词 strawberry 中，字母 r 一共出现了几次？只回答一个数字。",
        "check": _check_letter_count,
    },
    {
        "id": "sorting",
        "label": "排序",
        "prompt": "把下列数字按从小到大排列，用逗号分隔，不要写解释：42, 7, 19, 3, 88, 61",
        "check": _check_sort,
    },
    {
        "id": "json_extract",
        "label": "结构化抽取",
        "prompt": "把下面内容转成 JSON，键为 name / age / city，值用对应内容，"
                  "只输出 JSON：张三月，31 岁，住在杭州。",
        "check": _check_json,
    },
    {
        "id": "logic",
        "label": "逻辑蕴涵",
        "prompt": "已知：所有 A 都是 B，且有些 B 不是 C。那么「有些 A 是 C」必然成立吗？"
                  "只回答「是」或「否」。",
        "check": _check_logic,
    },
    {
        "id": "constraint",
        "label": "约束遵循",
        "prompt": "写一句话描写风，要求：不超过 20 个字，必须包含「风」字，"
                  "且不能出现「春天」二字。只输出这句话。",
        "check": _check_constraint,
    },
]


def grade(answers: dict[str, str]) -> dict[str, Any]:
    """把 6 条回答判成 0/1 向量。"""
    vector: dict[str, int] = {}
    for probe in PROBES:
        checker: Callable[[str], bool] = probe["check"]
        try:
            vector[probe["id"]] = 1 if checker(answers.get(probe["id"]) or "") else 0
        except Exception:  # noqa: BLE001 - 判分失败按答错处理，不毁掉整条通道
            vector[probe["id"]] = 0
    passed = sum(vector.values())
    return {
        "probe_version": PROBE_VERSION,
        "vector": vector,
        "passed": passed,
        "total": len(PROBES),
        "level": round(passed / len(PROBES), 4),
    }


def compare_capability(observed: dict[str, Any], reference: dict[str, Any]) -> float | None:
    """两组能力向量的重合率，0..1。"""
    left = observed.get("vector") or {}
    right = reference.get("vector") or {}
    shared = [k for k in left if k in right]
    if not shared:
        return None
    same = sum(1 for k in shared if left[k] == right[k])
    return same / len(shared)


class CapabilityChannel(BaseChannel):
    id = "capability"
    yields = {"series", "version"}
    reliability = 0.35

    def collect(self, ctx) -> list[Any]:
        adapter = ctx.adapter
        if adapter is None:
            return []

        total = len(PROBES)
        tracker = ctx.tracker("capability", "能力探针", total=total,
                              detail="6 条可自动判分的任务")
        answers: dict[str, str] = {}
        for probe in PROBES:
            tracker.begin(probe["label"])
            completion = adapter.complete(probe["prompt"], max_tokens=600, temperature=0)
            answers[probe["id"]] = completion.text if completion.ok else ""
            tracker.advance()
        tracker.finish()

        if not any(answers.values()):
            return [
                self._evidence(
                    "capability.cliff",
                    None,
                    "能力探针全部无响应",
                    match=None,
                    note="端点不可用或全部拒绝，本通道不参与判定。",
                    weight_scale=0.0,
                )
            ]

        observed = grade(answers)
        if not ctx.baselines or not self.rank(observed, ctx.baselines):
            return [
                self._evidence(
                    "capability.cliff",
                    "series",
                    f"6 条探针答对 {observed['passed']}/{observed['total']}",
                    metric={"vector": observed["vector"], "level": observed["level"]},
                    match=None,
                    note="基准库里还没有带 capability 数据的条目，无法闭集比对。",
                    weight_scale=0.0,
                )
            ]

        scored = self.rank(observed, ctx.baselines)
        best_model, best_score = scored[0]
        failed = [k for k, v in (observed["vector"] or {}).items() if not v]
        return [
            self._evidence(
                "capability.cliff",
                "series",
                f"答对 {observed['passed']}/{observed['total']}，"
                f"与 {best_model} 能力向量重合 {best_score:.0%}",
                metric={
                    "name": "capability_overlap",
                    "value": round(best_score, 4),
                    "vector": observed["vector"],
                    "level": observed["level"],
                    "failed": failed,
                },
                candidates=scored[:8],
                match=best_score >= 0.83,
                note=(
                    "能力会随上游负载与量化波动，属软信号。"
                    + (f"本次失手：{'、'.join(failed)}。" if failed else "本次全部答对。")
                ),
            )
        ]

    def rank(self, observed: dict[str, Any], baselines: list[Any]) -> list[tuple[str, float]]:
        results: list[tuple[str, float]] = []
        for entry in baselines:
            reference = (entry.channels or {}).get("capability")
            if not reference or reference.get("probe_version") != PROBE_VERSION:
                continue
            score = compare_capability(observed, reference)
            if score is None:
                continue
            results.append((entry.model_id, score))
        results.sort(key=lambda row: row[1], reverse=True)
        return results

    def estimate(self, model: str | None = None):
        from ..types import CostRange

        return CostRange(requests=6, input_tokens=(800, 2000), output_tokens=(600, 1500))
