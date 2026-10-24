"""基准采集。

CLI 的 `baseline import` 与 API 的 `/v1/baselines/collections` 共用这一段，
保证两条路径采出来的数据结构完全一致。

可采集的通道：
  tokenizer   37 条词表探针（边际 token 成本）
  behavior    12 条行为探针
  trace       3 次流式采样（TTFT / chunk 间隔 / TPS）—— 调用链数据
  backend_fp  后端指纹（搭车，零成本）

默认采集 tokenizer + behavior + backend_fp；显式指定时才会采 trace，
因为它要发流式请求，对部分端点成本更高。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from ..adapters import build_adapter
from .baseline.schema import BaselineEntry, Identity, Provenance
from .baseline.store import BaselineStore
from .channels.behavior import PARSERS, PROBES as BEHAVIOR_PROBES, PROBE_VERSION as BH_VERSION
from .channels.capability import PROBES as CAPABILITY_PROBES, grade as grade_capability
from .channels.distribution import (
    CHALLENGE_COUNT as DIST_CHALLENGES,
    MIN_NUMBERS,
    generate_challenges,
    observation_from_numbers,
    parse_numbers,
)
from .channels.onetoken import (
    LANGUAGES as OT_LANGUAGES,
    SAMPLES_PER_CELL as OT_SAMPLES,
    TASKS as OT_TASKS,
    FIXED_SYSTEM as OT_SYSTEM,
    normalize_answer as normalize_ot,
    observation_from_cells,
)
from .channels.style import TOPIC as STYLE_TOPIC, extract as extract_style
from .channels.tokenizer import PROBES as TOKENIZER_PROBES, PROBE_VERSION as TK_VERSION
from .channels.trace import PROBE_VERSION as TR_VERSION, summarize_samples
from .progress import ProgressTracker

ALL_CHANNELS = (
    "tokenizer", "behavior", "onetoken", "distribution",
    "capability", "style", "trace", "backend_fp",
)
DEFAULT_CHANNELS = ("tokenizer", "behavior", "backend_fp")


def collect_baseline(
    base_url: str,
    api_key: str,
    model: str,
    *,
    vendor: str,
    family: str,
    series: str = "",
    version: str = "",
    tier: str = "community",
    bank: str | None = None,
    channels: tuple[str, ...] | list[str] = DEFAULT_CHANNELS,
    aliases: list[str] | None = None,
    note: str = "",
    on_progress: Callable[[Any], None] | None = None,
) -> tuple[BaselineEntry, list[str]]:
    """采集一个模型的基准数据并写入指定基准库。

    返回 (条目, 提示列表)。提示不是错误，只是「这条数据缺了什么」。
    """
    store = BaselineStore(bank=bank)
    adapter = build_adapter(base_url, api_key, model)

    entry = store.get(model) or BaselineEntry(
        model_id=model, aliases=aliases or []
    )
    # 显式覆盖身份字段（用户这次明确声明的为准）
    entry.model_id = model
    entry.aliases = aliases or entry.aliases
    entry.identity.vendor = vendor
    entry.identity.family = family
    entry.identity.series = series or entry.identity.series
    entry.identity.version = version or entry.identity.version

    notes: list[str] = []
    wanted = tuple(channels) or DEFAULT_CHANNELS

    # ---- tokenizer ----
    if "tokenizer" in wanted:
        tracker = ProgressTracker(
            on_progress, phase="tokenizer", label="采组词表指纹",
            total=len(TOKENIZER_PROBES),
        )
        overhead = adapter.count_tokens("a")
        if overhead is None:
            notes.append("端点不返回 token 计数，跳过 tokenizer 采集")
            tracker.finish()
        else:
            counts: dict[str, int | None] = {}
            for probe in TOKENIZER_PROBES:
                tracker.begin(probe["label"])
                raw = adapter.count_tokens(probe["text"])
                counts[probe["id"]] = None if raw is None else max(0, raw - overhead)
                tracker.advance()
            tracker.finish()
            entry = store.upsert_from_observation(
                entry,
                "tokenizer",
                {
                    "probe_version": TK_VERSION,
                    "overhead": overhead,
                    "counts": counts,
                },
            )

    # ---- behavior ----
    if "behavior" in wanted:
        tracker = ProgressTracker(
            on_progress, phase="behavior", label="采集行为特征",
            total=len(BEHAVIOR_PROBES),
        )
        vector: dict[str, Any] = {}
        for probe in BEHAVIOR_PROBES:
            tracker.begin(probe["label"])
            completion = adapter.complete(probe["prompt"], max_tokens=300)
            if completion.ok:
                parser = PARSERS.get(probe["parser"])
                vector[probe["id"]] = (
                    parser(completion.text) if parser else completion.text[:200]
                )
            else:
                vector[probe["id"]] = None
            tracker.advance()
        tracker.finish()
        if any(v is not None for v in vector.values()):
            if isinstance(vector.get("cutoff"), str):
                entry.identity.knowledge_cutoff = vector["cutoff"]
            if isinstance(vector.get("context"), int):
                entry.identity.context_window = vector["context"]
            entry = store.upsert_from_observation(
                entry, "behavior", {"probe_version": BH_VERSION, "vector": vector}
            )
        else:
            notes.append("行为探针全部无响应，跳过 behavior")

    # ---- distribution（数字分布指纹，ModelTrace 方法） ----
    if "distribution" in wanted:
        import math

        challenges = generate_challenges(DIST_CHALLENGES)
        tracker = ProgressTracker(
            on_progress, phase="distribution", label="采集数字分布指纹",
            total=len(challenges),
        )
        samples: list[list[int]] = []
        for challenge in challenges:
            expected = int(challenge["expected_count"])
            tracker.begin(f"第 {len(samples) + 1} 条 · {expected} 个整数")
            completion = adapter.complete(challenge["prompt"], max_tokens=2048)
            numbers = parse_numbers(completion.text or "") if completion.ok else []
            # 样本太少时分布形状没有统计意义，宁可少一份也不要噪声
            if len(numbers) >= max(MIN_NUMBERS, math.ceil(expected * 0.55)):
                samples.append(numbers)
            tracker.advance()
        tracker.finish()
        if samples:
            entry = store.upsert_from_observation(
                entry, "distribution", observation_from_numbers(samples)
            )
        else:
            notes.append("整数生成任务全部拒答或严重截断，跳过 distribution")

    # ---- onetoken（单 token 分布指纹） ----
    if "onetoken" in wanted:
        cells = [(t, lang) for t in sorted(OT_TASKS) for lang in OT_LANGUAGES]
        tracker = ProgressTracker(
            on_progress, phase="onetoken", label="采集单 token 分布",
            total=len(cells) * OT_SAMPLES,
        )
        samples: dict[str, list[str]] = {}
        for task, lang in cells:
            cell = f"{task}:{lang}"
            samples[cell] = []
            for i in range(OT_SAMPLES):
                tracker.begin(f"{cell} 第 {i + 1}/{OT_SAMPLES} 次")
                completion = adapter.complete(
                    OT_TASKS[task][lang], system=OT_SYSTEM,
                    max_tokens=8, temperature=1.0,
                )
                token = normalize_ot(completion.text, task) if completion.ok else None
                if token is not None:
                    samples[cell].append(token)
                tracker.advance()
        tracker.finish()
        usable = {c: v for c, v in samples.items() if v}
        if usable:
            entry = store.upsert_from_observation(
                entry, "onetoken", observation_from_cells(usable)
            )
        else:
            notes.append("单 token 采样全部无效，跳过 onetoken")

    # ---- capability（能力悬崖） ----
    if "capability" in wanted:
        tracker = ProgressTracker(
            on_progress, phase="capability", label="采集能力向量",
            total=len(CAPABILITY_PROBES),
        )
        answers: dict[str, str] = {}
        for probe in CAPABILITY_PROBES:
            tracker.begin(probe["label"])
            completion = adapter.complete(probe["prompt"], max_tokens=600, temperature=0)
            answers[probe["id"]] = completion.text if completion.ok else ""
            tracker.advance()
        tracker.finish()
        if any(answers.values()):
            entry = store.upsert_from_observation(entry, "capability", grade_capability(answers))
        else:
            notes.append("能力探针全部无响应，跳过 capability")

    # ---- style（排版风格密度） ----
    if "style" in wanted:
        tracker = ProgressTracker(on_progress, phase="style", label="采集排版风格", total=1)
        tracker.begin("生成风格样本")
        completion = adapter.complete(STYLE_TOPIC, max_tokens=900)
        tracker.advance()
        tracker.finish()
        text = completion.text or ""
        if len(text) >= 120:
            entry = store.upsert_from_observation(
                entry, "style", {"probe_version": "st1-1", "vector": extract_style(text)}
            )
        else:
            notes.append("风格样本过短，跳过 style")

    # ---- trace（调用链 / 推理 trace） ----
    if "trace" in wanted:
        if not hasattr(adapter, "stream_collect"):
            notes.append("当前协议不支持流式，跳过 trace 采集")
        else:
            tracker = ProgressTracker(
                on_progress, phase="trace", label="采集调用链 trace", total=3
            )
            samples: list[dict[str, Any]] = []
            for i in range(3):
                tracker.begin(f"第 {i + 1} 次流式采样")
                samples.append(adapter.stream_collect(
                    "请从 1 数到 40，每个数字占一行。", max_tokens=120
                ))
                tracker.advance()
            tracker.finish()
            summary = summarize_samples(samples)
            if summary.get("stream_supported"):
                entry = store.upsert_from_observation(
                    entry, "trace", summary
                )
            else:
                notes.append("端点未返回流式数据，跳过 trace")

    # ---- backend_fp（搭车，零成本） ----
    if "backend_fp" in wanted:
        completion = adapter.complete("hi", max_tokens=1)
        if completion.ok and completion.system_fingerprint:
            entry = store.upsert_from_observation(
                entry,
                "backend_fp",
                {
                    "known_values": [completion.system_fingerprint],
                    "last_seen": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                },
            )

    entry.provenance = Provenance(
        tier=tier,
        method="modelproof baseline collect",
        collected_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        endpoint=base_url,
        note=note or f"采集通道：{', '.join(wanted)}",
    )

    path = store.save(entry)
    return entry, notes


def describe_plan(channels: tuple[str, ...] | list[str]) -> dict[str, Any]:
    """采集前的成本预估（让用户知道要发多少请求）。"""
    costs = {
        "tokenizer": len(TOKENIZER_PROBES),
        "behavior": len(BEHAVIOR_PROBES),
        "distribution": DIST_CHALLENGES,
        "onetoken": len(OT_TASKS) * len(OT_LANGUAGES) * OT_SAMPLES,
        "capability": len(CAPABILITY_PROBES),
        "style": 1,
        "trace": 3,
        "backend_fp": 1,
    }
    wanted = tuple(channels) or DEFAULT_CHANNELS
    return {
        "channels": list(wanted),
        "requests": sum(costs.get(c, 0) for c in wanted),
        "per_channel": {c: costs.get(c, 0) for c in wanted},
    }
