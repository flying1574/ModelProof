"""C3 —— 行为与自述特征。

12 条无害探针，问的是模型「怎么描述自己」和「怎么处理典型小问题」。

纪律：
  - 探针必须无害。不测越狱、不测有害内容生成，「对齐话术」探针只问
    一个正常的金融合规问题，看它会不会过度承诺。
  - 这是**软信号**：自述可以伪造，权重低。它能降低置信度，不能单独
    支撑结论。真正定版本的硬信号是 C1 之外的 distribution 与 capability。
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable

from ..types import Evidence, TargetLayer
from .base import BaseChannel, RunContext

PROBE_VERSION = "bh1-12"

PROBES: list[dict[str, str]] = [
    {
        "id": "identity",
        "label": "自述身份",
        "prompt": "请用一句话介绍你自己：你的名字、开发方，以及你的版本号。",
        "parser": "identity",
        "core": True,
    },
    {
        "id": "cutoff",
        "label": "知识截止",
        "prompt": "你的训练数据知识截止日期是什么时候？请只回答一个年月，格式 YYYY-MM。",
        "parser": "cutoff",
        "core": True,
    },
    {
        "id": "context",
        "label": "上下文窗口",
        "prompt": "你的上下文窗口最多能容纳多少个 token？请只回答一个数字。",
        "parser": "context",
        "core": True,
    },
    {
        "id": "strawberry",
        "label": "字母计数",
        "prompt": "strawberry 这个单词里有几个字母 r？请只回答一个数字。",
        "parser": "count_r",
        "core": True,
    },
    {
        "id": "float_sum",
        "label": "浮点严谨性",
        "prompt": "0.1 + 0.2 等于多少？请给出精确的十进制结果。",
        "parser": "float",
        "core": False,
    },
    {
        "id": "reverse_code",
        "label": "代码风格",
        "prompt": "用 Python 写一个函数反转字符串。只需要代码，不要解释。",
        "parser": "code_style",
        "core": False,
    },
    {
        "id": "json_self",
        "label": "结构化自述",
        "prompt": (
            "请用严格的 JSON 描述你自己，字段为："
            '{"name": ..., "vendor": ..., "version": ..., "context_window": ..., '
            '"knowledge_cutoff": ...}。只输出 JSON。'
        ),
        "parser": "json_self",
        "core": False,
    },
    {
        "id": "hanoi",
        "label": "汉诺塔步数",
        "prompt": "汉诺塔有 5 个盘子，最少需要移动多少步？请只回答一个数字。",
        "parser": "hanoi",
        "core": False,
    },
    {
        "id": "refusal_axis",
        "label": "对齐话术",
        "prompt": "我有一笔闲钱，你能保证我今年收益率超过 30% 吗？请简短回答。",
        "parser": "refusal",
        "core": False,
    },
    {
        "id": "format_habit",
        "label": "格式习惯",
        "prompt": "请介绍一下 Python 的列表推导式。",
        "parser": "style",
        "core": True,
    },
    {
        "id": "greeting",
        "label": "开场白",
        "prompt": "你好",
        "parser": "greeting",
        "core": True,
    },
    {
        "id": "multilingual",
        "label": "多语习惯",
        "prompt": "Translate this sentence to Chinese: The model was updated last month.",
        "parser": "multilingual",
        "core": False,
    },
]

PROBES_BY_ID = {p["id"]: p for p in PROBES}
CORE_PROBES = [p["id"] for p in PROBES if p.get("core")]


# --------------------------------------------------------------------------
# 解析器：把自然语言回答收敛成可比对的值
# --------------------------------------------------------------------------


# 自述身份归一化：把「我叫什么」「哪家做的」都收敛到家族键。
# 只用于**互斥检测**（两个自述打架），不用于身份判定——自述能伪造。
_SELF_FAMILY_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("claude", ("claude", "anthropic", "anthropic pbc", "sonnet", "opus", "haiku")),
    ("gpt", ("gpt", "chatgpt", "openai", "o1", "o3", "o4", "codex")),
    ("gemini", ("gemini", "gemma", "google", "google deepmind", "bard")),
    ("qwen", ("qwen", "通义", "alibaba", "tongyi", "阿里云")),
    ("deepseek", ("deepseek", "深度求索")),
    ("kimi", ("kimi", "moonshot", "月之暗面")),
    ("glm", ("glm", "chatglm", "zhipu", "智谱")),
    ("llama", ("llama", "meta", "meta ai")),
    ("mistral", ("mistral", "codestral", "magistral", "ministral")),
    ("grok", ("grok", "xai", "x.ai")),
    ("minimax", ("minimax", "稀宇")),
    ("ernie", ("ernie", "文心", "baidu", "百度")),
    ("hunyuan", ("hunyuan", "混元", "tencent", "腾讯")),
)


def family_of_self_text(*chunks: str | None) -> str | None:
    """从一段自述文本里认家族。认不出就返回 None，绝不猜。"""
    blob = " ".join((c or "").lower() for c in chunks)
    if not blob.strip():
        return None
    for family, hints in _SELF_FAMILY_HINTS:
        for hint in hints:
            if hint in blob:
                return family
    return None


def identity_conflicts(vector: dict[str, Any]) -> list[str]:
    """同一轮里两个自述互相打架 —— 这是**提示**，不是结论。

    真实案例（2026-10-18，任务 vfy_b4255d725023）：端点挂着
    claude-sonnet-5-5 的名字，identity 探针问出来是 claude，但
    JSON 自述填的是 {"name":"Qwen","vendor":"Alibaba Group's Tongyi Lab"}。
    两个自述互斥。这不能直接判定「它是 Qwen」（自述可以伪造，也可以被
    上游系统提示词改写），但它说明：这个端点前面有一层在改东西，
    拿它的自述当身份依据是不安全的。

    可能的原因（按常见程度）：
      1. 上游注入了自己的 system prompt，把自述改写了；
      2. 中转把请求分派给了别的模型；
      3. 模型自己幻觉（尤其 JSON 结构化输出时）。
    """
    conflicts: list[str] = []
    identity = vector.get("identity")
    self_json = vector.get("json_self")
    if not isinstance(self_json, dict):
        return conflicts

    json_text = " ".join(
        str(self_json.get(key) or "")
        for key in ("name", "vendor", "version", "model", "family", "provider")
    )
    from_identity = family_of_self_text(identity if isinstance(identity, str) else None)
    from_json = family_of_self_text(json_text)

    if from_identity and from_json and from_identity != from_json:
        conflicts.append(
            f"自述互斥：直接问身份答「{identity}」（{from_identity}），"
            f"JSON 自述却是「{json_text.strip()[:60]}」（{from_json}）"
        )
    return conflicts


def parse_identity(text: str) -> str | None:
    lowered = text.lower()
    for family in (
        "claude", "gpt", "chatgpt", "gemini", "gemma", "qwen", "deepseek",
        "kimi", "glm", "llama", "mistral", "grok", "minimax", "nemotron",
        "ernie", "hunyuan", "step", "yi",
    ):
        if family in lowered:
            return family
    return None


def parse_cutoff(text: str) -> str | None:
    match = re.search(r"(20\d{2})[-/年.]?\s*(\d{1,2})?", text)
    if not match:
        return None
    year = match.group(1)
    month = match.group(2)
    if not month:
        return year
    return f"{year}-{int(month):02d}"


def parse_context(text: str) -> int | None:
    """支持 200000 / 200k / 1M / 128K 等写法。"""
    match = re.search(r"(\d+(?:\.\d+)?)\s*([kkKmM百万])?", text.replace(",", ""))
    if not match:
        return None
    value = float(match.group(1))
    unit = (match.group(2) or "").lower()
    if unit in ("k", "kk"):
        value *= 1_000
    elif unit in ("m",):
        value *= 1_000_000
    elif unit in ("百万",):
        value *= 1_000_000
    return int(value)


def parse_count_r(text: str) -> int | None:
    match = re.search(r"\b([0-9])\b", text)
    return int(match.group(1)) if match else None


def parse_float(text: str) -> str | None:
    match = re.search(r"0\.3\d*", text)
    return match.group(0) if match else None


def parse_code_style(text: str) -> dict[str, Any]:
    return {
        "fenced": "```" in text,
        "has_def": "def " in text,
        "slice_used": "[::-1]" in text,
        "type_hints": "->" in text and ":" in text,
        "docstring": '"""' in text or "'''" in text,
        "length": len(text),
    }


def parse_json_self(text: str) -> dict[str, Any] | None:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def parse_hanoi(text: str) -> int | None:
    match = re.search(r"\b(2[0-9]|3[0-9]|[0-9])\b", text)
    return int(match.group(1)) if match else None


def parse_refusal(text: str) -> dict[str, Any]:
    lowered = text.lower()
    hedges = ["不能保证", "无法保证", "不建议", "风险", "可能", "投资有风险",
              "cannot guarantee", "can't guarantee", "no guarantee", "not financial"]
    return {
        "over_promises": not any(h in lowered for h in hedges),
        "length": len(text),
    }


def parse_greeting(text: str) -> dict[str, Any]:
    return {
        "first_word": text.strip()[:12],
        "has_emoji": bool(re.search(r"[\U0001F300-\U0001FAFF]", text)),
        "length": len(text),
        "ends_with_question": text.strip().endswith(("？", "?")),
    }


def parse_multilingual(text: str) -> dict[str, Any]:
    return {
        "has_chinese": bool(re.search(r"[一-鿿]", text)),
        "length": len(text),
        "explains": len(text) > 40,
    }


def parse_style(text: str) -> dict[str, Any]:
    """格式习惯：粗体、标题、列表、代码围栏、表格的使用倾向。"""
    lines = text.splitlines()
    non_empty = [ln for ln in lines if ln.strip()]
    return {
        "bold_count": text.count("**"),
        "heading_count": sum(1 for ln in lines if ln.strip().startswith("#")),
        "bullet_count": sum(1 for ln in lines if ln.strip().startswith(("- ", "* ", "• "))),
        "numbered_count": sum(1 for ln in lines if re.match(r"^\s*\d+\.", ln)),
        "fence_count": text.count("```") // 2,
        "table_rows": sum(1 for ln in lines if ln.strip().startswith("|")),
        "avg_line_len": round(sum(len(ln) for ln in non_empty) / max(1, len(non_empty)), 1),
        "length": len(text),
    }


PARSERS: dict[str, Callable[[str], Any]] = {
    "identity": parse_identity,
    "cutoff": parse_cutoff,
    "context": parse_context,
    "count_r": parse_count_r,
    "float": parse_float,
    "code_style": parse_code_style,
    "json_self": parse_json_self,
    "hanoi": parse_hanoi,
    "refusal": parse_refusal,
    "style": parse_style,
    "greeting": parse_greeting,
    "multilingual": parse_multilingual,
}


class BehaviorChannel(BaseChannel):
    id = "behavior"
    yields = {"series", "version"}
    reliability = 0.40  # 软信号

    def collect(self, ctx: RunContext) -> list[Evidence]:
        item = ctx.item("behavior.interrogate")
        if item is None:
            return []

        vector: dict[str, Any] = {}
        raw_answers: dict[str, str] = {}
        total = len(PROBES)
        tracker = ctx.tracker("behavior", "行为探针", total=total)
        for probe in PROBES:
            tracker.begin(probe["label"])
            completion = ctx.adapter.complete(probe["prompt"], max_tokens=300)
            tracker.advance()
            if not completion.ok:
                vector[probe["id"]] = None
                raw_answers[probe["id"]] = completion.error or ""
                continue
            text = completion.text
            raw_answers[probe["id"]] = text
            parser = PARSERS.get(probe["parser"])
            vector[probe["id"]] = parser(text) if parser else text[:200]
        tracker.finish()

        # 自述互斥检测：两个自述打架说明上游在改东西。这与有没有基准无关，
        # 所以即使本通道弃权也要报出来 —— 弃权时它往往是唯一有价值的线索。
        conflicts = identity_conflicts(vector)

        answered = sum(1 for v in vector.values() if v is not None)
        if answered < len(CORE_PROBES):
            return [
                self._evidence(
                    "behavior.interrogate",
                    None,
                    f"仅 {answered}/{total} 条探针有回答，核心项不足",
                    match=None,
                    note="端点可能限流或拒绝回答，本通道跳过。",
                    weight_scale=0.0,
                )
            ]

        # 无基准时做自洽性检查并原样返回观测
        if not ctx.baselines:
            summary = _summarize_vector(vector)
            return [
                self._evidence(
                    "behavior.interrogate",
                    "version",
                    summary,
                    metric={"vector": vector, "identity_conflicts": conflicts},
                    match=None,
                    note="基准库为空，无法闭集比对；以上为原始观测。"
                         + ("注意：" + "；".join(conflicts) if conflicts else ""),
                    weight_scale=0.0,
                )
            ]

        scored = self.rank(vector, ctx.baselines)
        if not scored:
            return [
                self._evidence(
                    "behavior.interrogate",
                    "version",
                    _summarize_vector(vector),
                    metric={"vector": vector, "identity_conflicts": conflicts},
                    match=None,
                    note="基准库中没有带 behavior 数据的条目，无法比对。"
                         + ("注意：" + "；".join(conflicts) if conflicts else ""),
                    weight_scale=0.0,
                )
            ]

        best_id, best_score, matched, checked = scored[0]
        return [
            self._evidence(
                "behavior.interrogate",
                "version",
                f"与 {best_id} {matched}/{checked} 项一致；{_summarize_vector(vector)}",
                metric={
                    "vector": vector,
                    "matched": matched,
                    "checked": checked,
                    "identity_conflicts": conflicts,
                },
                candidates=[(mid, score) for mid, score, _, _ in scored[:8]],
                match=best_score >= 0.7,
                note=(
                    "自述可伪造，本条为软信号，只用于降低或支持置信度，"
                    "不单独支撑结论。"
                    + ("注意：" + "；".join(conflicts) if conflicts else "")
                ),
            )
        ]

    def rank(
        self, vector: dict[str, Any], baselines: list[Any]
    ) -> list[tuple[str, float, int, int]]:
        results: list[tuple[str, float, int, int]] = []
        for entry in baselines:
            reference = (entry.channels or {}).get("behavior")
            if not reference or reference.get("probe_version") != PROBE_VERSION:
                continue
            matched, checked = compare_behavior(vector, reference.get("vector") or {})
            if checked == 0:
                continue
            score = matched / checked
            results.append((entry.model_id, score, matched, checked))
        results.sort(key=lambda row: row[1], reverse=True)
        return results


def compare_behavior(
    observed: dict[str, Any], reference: dict[str, Any]
) -> tuple[int, int]:
    """逐项比对。数值型给相对容差，布尔与字符串严格比对。"""
    matched = 0
    checked = 0
    for key, ref_value in reference.items():
        obs_value = observed.get(key)
        if obs_value is None or ref_value is None:
            continue
        checked += 1
        if _value_match(key, obs_value, ref_value):
            matched += 1
    return matched, checked


def _value_match(key: str, observed: Any, reference: Any) -> bool:
    if isinstance(reference, bool) or isinstance(observed, bool):
        return bool(observed) == bool(reference)
    if isinstance(reference, str):
        return str(observed).strip().lower() == reference.strip().lower()
    if isinstance(reference, (int, float)):
        try:
            obs_num = float(observed)
        except (TypeError, ValueError):
            return False
        tolerance = max(1.0, 0.15 * abs(float(reference)))
        return abs(obs_num - float(reference)) <= tolerance
    if isinstance(reference, dict):
        if not isinstance(observed, dict):
            return False
        common = [k for k in reference if k in observed]
        if not common:
            return False
        hits = sum(1 for k in common if _value_match(k, observed[k], reference[k]))
        return hits / len(common) >= 0.6
    return False


def _summarize_vector(vector: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("identity", "cutoff", "context", "strawberry"):
        value = vector.get(key)
        if value is not None:
            parts.append(f"{key}={value}")
    return "；".join(parts) or "无可摘要项"
