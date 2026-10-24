"""C4 排版风格密度。

同一家族的模型在「怎么组织一段回答」上高度一致：用不用小标题、
爱不爱上项目符号、加不加粗、代码块用不用围栏、段落多长。
这些是后训练对齐留下的习惯，换模型就换风格，改 prompt 也压不掉。

这条通道不测内容，只测**形状**。所以它：
  * 不能单独定身份（风格可以被系统提示词改写）；
  * 但很擅长发现「同一个入口前后风格突变」——那通常意味着上游换了模型。

特征全部是计数与比值，可比性强；比对用相对差，并给数值型特征留 20% 容差，
避免把「同一模型两次采样的小波动」误判成不同模型。
"""

from __future__ import annotations

import re
from typing import Any

from .base import BaseChannel

PROBE_VERSION = "st1-1"

TOPIC = (
    "请用中文向一位刚接触大模型的工程师解释：模型为什么会「幻觉」，"
    "以及工程上可以从哪些环节降低它。写 300 字左右，按你习惯的方式排版。"
)

# 相对差容差：数值型特征允许 20% 的漂移
TOLERANCE = 0.20
MIN_CHARS = 120          # 太短的回答统计不出稳定风格
NUMERIC_KEYS = (
    "headers", "bullets", "numbered", "bold", "code_fences", "tables",
    "emoji", "paragraphs", "avg_sentence_len", "avg_paragraph_len",
    "list_ratio", "punct_ratio", "connectives",
)

_RE_HEADER = re.compile(r"^\s{0,3}#{1,6}\s", re.M)
_RE_BULLET = re.compile(r"^\s{0,3}[-*•]\s+", re.M)
_RE_NUMBERED = re.compile(r"^\s{0,3}\d+[.)]\s+", re.M)
_RE_BOLD = re.compile(r"\*\*[^*\n]{1,80}\*\*")
_RE_FENCE = re.compile(r"^\s{0,3}```", re.M)
_RE_TABLE = re.compile(r"^\s{0,3}\|.*\|\s*$", re.M)
_RE_EMOJI = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF]"
)
_RE_SENTENCE = re.compile(r"[^。！？!?.；;\n]+[。！？!?.；;]?")
_CONNECTIVES = ("首先", "其次", "再次", "最后", "综上", "总的来说", "需要注意", "值得一提")


def extract(text: str) -> dict[str, Any]:
    """把一段回答压成风格向量。"""
    lines = [line for line in text.splitlines()]
    body = [line for line in lines if line.strip()]
    paragraphs = [p for p in re.split(r"\n\s*\n", text) if p.strip()] or ([text] if text.strip() else [])

    sentences = [s for s in _RE_SENTENCE.findall(text) if len(s.strip()) > 1]
    avg_sentence = (sum(len(s.strip()) for s in sentences) / len(sentences)) if sentences else 0.0
    avg_paragraph = (sum(len(p.strip()) for p in paragraphs) / len(paragraphs)) if paragraphs else 0.0

    return {
        "chars": len(text),
        "headers": len(_RE_HEADER.findall(text)),
        "bullets": len(_RE_BULLET.findall(text)),
        "numbered": len(_RE_NUMBERED.findall(text)),
        "bold": len(_RE_BOLD.findall(text)),
        "code_fences": len(_RE_FENCE.findall(text)) // 2,
        "tables": len(_RE_TABLE.findall(text)),
        "emoji": len(_RE_EMOJI.findall(text)),
        "paragraphs": len(paragraphs),
        "lines": len(body),
        "avg_sentence_len": round(avg_sentence, 2),
        "avg_paragraph_len": round(avg_paragraph, 2),
        "list_ratio": round((len(_RE_BULLET.findall(text)) + len(_RE_NUMBERED.findall(text))) / max(1, len(body)), 3),
        "punct_ratio": round(sum(text.count(c) for c in "，,；;：:") / max(1, len(text)) * 100, 3),
        "connectives": sum(text.count(word) for word in _CONNECTIVES),
    }


def compare_style(observed: dict[str, Any], reference: dict[str, Any]) -> tuple[float, int]:
    """返回 (平均相对差, 特征数)。差异越大越不像。"""
    vector = reference.get("vector") or {}
    if not vector:
        return 1.0, 0

    diffs: list[float] = []
    for key in NUMERIC_KEYS:
        if key not in vector or key not in observed:
            continue
        left = float(observed[key])
        right = float(vector[key])
        scale = max(1.0, abs(right))
        diffs.append(min(1.0, abs(left - right) / scale))
    if not diffs:
        return 1.0, 0
    return sum(diffs) / len(diffs), len(diffs)


class StyleChannel(BaseChannel):
    id = "style"
    yields = {"family", "series"}
    reliability = 0.30   # 软信号：系统提示词能改写风格，不能单独定论

    def collect(self, ctx) -> list[Any]:
        adapter = ctx.adapter
        if adapter is None:
            return []

        tracker = ctx.tracker("style", "排版风格采集", total=1, detail="一段 300 字左右的解释")
        tracker.begin("生成风格样本")
        completion = adapter.complete(TOPIC, max_tokens=900)
        tracker.advance()
        tracker.finish()

        if not completion.ok or len(completion.text or "") < MIN_CHARS:
            return [
                self._evidence(
                    "style.density",
                    None,
                    "风格样本不可用",
                    match=None,
                    note=(completion.error or "回答过短，统计不出稳定风格") + "。本通道不参与判定。",
                    weight_scale=0.0,
                )
            ]

        vector = extract(completion.text or "")
        observed = {"probe_version": PROBE_VERSION, "vector": vector}

        if not ctx.baselines:
            return [
                self._evidence(
                    "style.density",
                    "family",
                    f"采到 {vector['chars']} 字样本",
                    metric={"vector": vector},
                    match=None,
                    note="基准库为空，无法闭集判定。",
                    weight_scale=0.0,
                )
            ]

        scored = self.rank(observed, ctx.baselines)
        if not scored:
            return [
                self._evidence(
                    "style.density",
                    "family",
                    f"采到 {vector['chars']} 字样本，但基准库无对照数据",
                    metric={"vector": vector},
                    match=None,
                    note="基准库里还没有带 style 数据的条目，请先采集。",
                    weight_scale=0.0,
                )
            ]

        best_model, best_score = scored[0]
        return [
            self._evidence(
                "style.density",
                "family",
                f"与 {best_model} 风格相似度 {best_score:.2f}",
                metric={"name": "style_similarity", "value": round(best_score, 4),
                        "threshold": 1.0 - TOLERANCE, "vector": vector},
                candidates=scored[:8],
                match=best_score >= 1.0 - TOLERANCE,
                note="风格是软信号：系统提示词可改写，只用于同族内区分与突变检测。",
            )
        ]

    def rank(self, observed: dict[str, Any], baselines: list[Any]) -> list[tuple[str, float]]:
        results: list[tuple[str, float]] = []
        for entry in baselines:
            reference = (entry.channels or {}).get("style")
            if not reference or reference.get("probe_version") != PROBE_VERSION:
                continue
            diff, count = compare_style(observed.get("vector") or {}, reference)
            if count == 0:
                continue
            results.append((entry.model_id, max(0.0, 1.0 - diff / TOLERANCE)))
        results.sort(key=lambda row: row[1], reverse=True)
        return results

    def estimate(self, model: str | None = None):
        from ..types import CostRange

        return CostRange(requests=1, input_tokens=(80, 150), output_tokens=(400, 900))
