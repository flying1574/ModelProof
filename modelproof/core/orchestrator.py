"""验证编排。

职责：搭适配器 → 预估 → 预算守卫 → 跑通道 → 融合 → 出报告。
三个入口（CLI / Web / API）都只调这一个函数，能力天然一致。
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ..adapters import build_adapter, detect_protocol
from ..core.baseline.store import BaselineStore
from ..core.channels import CHANNELS, NON_IDENTITY_CHANNELS, RunContext
from ..core.estimate import (
    PriceTable,
    combine_ranges,
    estimate_from_spec,
    load_price_table,
    over_budget,
)
from ..core.fusion import decide
from ..core.report import VerificationReport, attach_actual_cost, attach_estimate
from ..core.suite.registry import REGISTRY
from ..core.suite.model import TestItem
from ..core.types import Cost, CostRange, TargetLayer, Truthfulness, TruthfulnessCheck
from ..core.progress import Progress, legacy_adapter


from ..core.channel_meta import CHANNEL_LABEL  # noqa: F401  进度条与 CLI 都用它


def _baseline_summary(store: Any, version_prefix: str | None = None) -> dict[str, Any]:
    """基准库概况。除了条目总数，还要说清**有多少条真的带指纹**。

    这个数字是刚需：库里 103 条，但只有 18 条带实测指纹时，
    闭集判定的实际候选池是 18 而不是 103。把它写出来，
    「为什么测不出来」才有答案，而不是靠用户自己猜。
    """
    entries = store.all()
    with_fingerprints = [
        e for e in entries if any((e.channels or {}).get(c) for c in _IDENTITY_CHANNELS)
    ]
    families = sorted({
        e.identity.family for e in with_fingerprints if e.identity.family
    })
    prefix = version_prefix or f"bank:{store.bank}"
    return {
        "version": f"{prefix}-{store.count()}",
        "bank": store.bank,
        "candidates_considered": store.count(),
        "with_fingerprints": len(with_fingerprints),
        "fingerprint_families": families,
        "note": (
            f"库内 {store.count()} 条，其中 {len(with_fingerprints)} 条带实测指纹"
            + (f"（覆盖 {('、').join(families)} 家族）" if families else "")
            + "。没有指纹的条目不参与闭集比对 —— 这决定了实际候选池的大小。"
        ),
    }


# 能产出身份证据的通道（gateway / validity / consistency 不算）
_IDENTITY_CHANNELS = (
    "tokenizer", "backend_fp", "distribution", "onetoken",
    "behavior", "capability", "style", "trace",
)


def estimate_run(
    items: list[TestItem],
    price_table: str | PriceTable = "openai",
    model: str | None = None,
    reasoning: bool = False,
) -> CostRange:
    """开跑前的消耗预估。纯计算，不发任何请求。"""
    if isinstance(price_table, str):
        table = load_price_table(price_table).for_model(model)
    else:
        table = price_table

    ranges = [
        estimate_from_spec(
            item.requests,
            item.input_tokens_range,
            item.output_tokens_range,
            table,
            reasoning=reasoning,
        )
        for item in items
    ]
    return combine_ranges(ranges)


def run_verification(
    base_url: str,
    api_key: str = "",
    model: str = "",
    *,
    suite: str | None = None,
    items: str | None = None,
    claim: str | None = None,
    target_layer: TargetLayer = "family",
    max_cost: float | None = None,
    max_seconds: float | None = None,
    price_table: str = "openai",
    protocol: str | None = None,
    store: BaselineStore | None = None,
    bank: str | None = None,
    include: list[str] | None = None,
    reasoning: bool = False,
    on_progress: Callable[[Progress], None] | None = None,
    dry_run: bool = False,
) -> VerificationReport:
    """跑一次完整验证，返回 VerificationReport。

    `bank`   指定用哪个基准库做对照（自定义库也走这里）；
    `include` 再缩小到库里的某几个模型（不选 = 整个库）；
    `on_progress` 收到的是结构化 Progress，含当前项名称，不只是计数。
    """
    started = time.monotonic()
    if store is None:
        store = BaselineStore(bank=bank)
    store.load()

    picked = REGISTRY.resolve(items or suite or "standard")
    declared_model = model or ""
    claim_id = claim or declared_model

    def emit(phase: str, label: str, item: str = "", current: int = 0, total: int = 0) -> None:
        if on_progress:
            on_progress(Progress(phase=phase, label=label, item=item,
                                 current=current, total=total))

    report = VerificationReport(
        input={
            "base_url": base_url,
            "declared_model": declared_model,
            "claim": claim,
            "suite": suite or (items and "custom" or "standard"),
            "target_layer": target_layer,
            "bank": store.bank,
            "items": [i.id for i in picked],
        },
        baseline=_baseline_summary(store),
    )

    estimate = estimate_run(picked, price_table, declared_model, reasoning=reasoning)
    attach_estimate(report, estimate)

    # 预算守卫：预估下限就超了，直接不跑
    reason = over_budget(estimate, max_cost, max_seconds)
    if reason:
        report.warnings.append(reason)
        report.cost["budget"] = {
            "max_cost": max_cost,
            "max_seconds": max_seconds,
            "interrupted": True,
        }
        return report.finish()

    if dry_run:
        report.warnings.append("dry-run：只做了预估，未发起任何请求")
        return report.finish()

    # -- 协议探测与适配器 --------------------------------------------------
    emit("prepare", "准备", "探测端点协议")
    detected = protocol if protocol and protocol != "auto" else detect_protocol(base_url, api_key)
    report.protocol = {
        "detected": detected,
        "attempted": ["openai", "anthropic"],
        "requested": protocol or "auto",
    }
    if detected == "unknown":
        report.warnings.append("未能确定端点协议，已按 OpenAI 兼容格式尝试")

    adapter = build_adapter(base_url, api_key, declared_model, protocol=detected)

    # -- 基准候选 ----------------------------------------------------------
    # 有 claim 时把 claim 对应的条目也纳入候选，便于直接比对
    baselines = store.all()
    if include:
        wanted = {str(name).strip() for name in include if str(name).strip()}
        selected = [
            entry for entry in baselines
            if entry.model_id in wanted or wanted & set(entry.aliases or [])
        ]
        if not selected:
            report.warnings.append(
                f"指定的对照模型 {sorted(wanted)} 在库 {store.bank} 里都不存在，"
                "已退回整库比对"
            )
        else:
            baselines = selected
            report.input["include"] = sorted(wanted)
    if claim_id:
        claimed_entry = store.get(claim_id)
        if claimed_entry is None:
            report.warnings.append(
                f"基准库里没有 {claim_id}，真伪判定只能基于通用特征"
            )

    ctx = RunContext(
        adapter=adapter,
        declared_model=declared_model,
        claim=claim,
        items=picked,
        baselines=baselines,
        on_progress=on_progress,
    )

    # -- 跑通道 ------------------------------------------------------------
    # 整体进度以「请求数」为刻度：通道内部的探针数基本等于请求数，
    # 所以把每段计数加上已完成的前置量，就能得到整次运行的百分比。
    actual = Cost(currency=estimate.currency)
    total_requests = sum(item.requests for item in picked) or 1
    ctx.progress_total = total_requests
    done_requests = 0

    channel_ids = {item.channel for item in picked}
    ordered = [c for c in sorted(channel_ids)]
    for index, channel_id in enumerate(ordered, start=1):
        channel = CHANNELS.get(channel_id)
        if channel is None:
            report.warnings.append(f"未注册的通道：{channel_id}（已跳过）")
            continue
        ctx.progress_base = done_requests
        emit(channel_id, CHANNEL_LABEL.get(channel_id, channel_id),
             f"{index}/{len(ordered)} 个通道", done_requests, total_requests)
        try:
            evidence = channel.collect(ctx)
        except Exception as error:  # noqa: BLE001 - 单通道失败不该毁掉整次运行
            report.warnings.append(f"通道 {channel_id} 运行失败：{error}")
            continue
        done_requests += sum(i.requests for i in picked if i.channel == channel_id)
        for item in evidence:
            if item.channel == "gateway":
                report.gateway = item.metric
            report.evidence.append(item)

    emit("finalize", "汇总判定", "融合各通道证据", done_requests, total_requests)

    # 所有证据都无权重意味着今天这次跑不出结论 —— 明确告诉用户为什么，
    # 而不是让他对着一堆「弃权」猜。
    if report.evidence and all(e.weight == 0 for e in report.evidence):
        report.warnings.append(
            "本次没有任何通道给出可用于判定的证据。最常见的原因是基准库里"
            "还没有这个模型的对照数据（tokenizer / behavior 指纹），"
            "请先用 modelproof baseline import 采集。"
        )

    # -- 融合判定 ----------------------------------------------------------
    verdicts, conflicts = decide(report.evidence, baselines, target_layer=target_layer)
    report.layers = verdicts
    report.conflicts = conflicts

    # -- 结果可信度：C10 的观测 + 本地指标自检（纯计算，零请求） ------------
    from .channels.validity import metric_sanity

    sanity = metric_sanity(report)
    observed_validity = next(
        (e.metric for e in report.evidence if e.channel == "validity"), None
    )
    if observed_validity:
        sanity["observed"] = observed_validity
        if observed_validity.get("verdict") != "stable":
            sanity["factors"].append(
                "验真探针未全绿：" + observed_validity.get("verdict", "")
            )
            if sanity["level"] == "ok":
                sanity["level"] = "caution"
    report.validity = sanity

    # -- 真伪模式 ----------------------------------------------------------
    if claim and claim_id:
        report.truthfulness = _judge_truthfulness(report, store, claim_id)

    # -- 成本结算 ----------------------------------------------------------
    elapsed = time.monotonic() - started
    table = load_price_table(price_table).for_model(declared_model)
    actual.requests = sum(
        1 for e in report.evidence if e.channel != "gateway"
    )
    actual.input_tokens = estimate.input_tokens[0]
    actual.output_tokens = estimate.output_tokens[0]
    actual.seconds = elapsed
    actual.amount = (
        actual.input_tokens / 1e6 * table.input_per_mtok
        + actual.output_tokens / 1e6 * table.output_per_mtok
    )
    actual.currency = table.currency
    attach_actual_cost(report, actual)
    report.cost["budget"] = {
        "max_cost": max_cost,
        "max_seconds": max_seconds,
        "interrupted": max_seconds is not None and elapsed > max_seconds,
    }

    return report.finish()


def _cutoff_match(claimed: Any, observed: Any) -> bool:
    """知识截止比对到「年月」。

    模型几乎总是只答到月（"2026-04"），而基准里可能记的是完整日期
    （"2026-04-30"）。按日严格比对会把「答对了但更粗」误判成不符，
    所以统一取到年月再比。跨月则照实判为不一致。
    """
    def month(value: Any) -> str:
        text = str(value).strip()
        match = re.match(r"(\d{4})[-/.年](\d{1,2})", text)
        if match:
            return f"{match.group(1)}-{int(match.group(2)):02d}"
        match = re.match(r"(\d{4})", text)
        return match.group(1) if match else text

    return month(claimed) == month(observed)


def _judge_truthfulness(
    report: VerificationReport, store: BaselineStore, claim_id: str
) -> Truthfulness:
    """真伪判定：拿观测到的自述去对基准里声明的字段。"""
    entry = store.get(claim_id)
    identity = entry.identity if entry else None

    # 从 behavior 证据里取观测值
    observed: dict[str, Any] = {}
    for item in report.evidence:
        if item.channel == "behavior" and item.metric.get("vector"):
            observed = item.metric["vector"]
            break

    checks: list[TruthfulnessCheck] = []

    def add(field: str, claimed: Any, seen: Any) -> bool | None:
        if claimed in (None, "", 0) or seen is None:
            return None
        if field == "knowledge_cutoff":
            ok = _cutoff_match(claimed, seen)
        elif isinstance(claimed, int) and isinstance(seen, int):
            ok = abs(claimed - seen) <= max(1, 0.2 * claimed)
        else:
            ok = str(claimed).strip().lower() == str(seen).strip().lower()
        checks.append(TruthfulnessCheck(field, claimed, seen, ok))
        return ok

    if identity:
        add("family", identity.family, observed.get("identity"))
        if identity.knowledge_cutoff:
            add("knowledge_cutoff", identity.knowledge_cutoff, observed.get("cutoff"))
        if identity.context_window:
            add("context_window", identity.context_window, observed.get("context"))

    # 家族层判定也可以参与
    family_verdict = report.layers.get("family")
    if family_verdict and not family_verdict.abstained and identity:
        checks.append(
            TruthfulnessCheck(
                "family_fingerprint",
                identity.family,
                family_verdict.value,
                identity.family == family_verdict.value,
            )
        )

    judged = [c for c in checks if c.match is not None]
    if not judged:
        return Truthfulness(
            claim=claim_id,
            verdict="unknown",
            confidence=0.0,
            checks=checks,
            note="没有可用于比对的特征（基准库缺条目或行为通道未跑）",
        )

    matched = sum(1 for c in judged if c.match)
    ratio = matched / len(judged)

    if ratio == 1.0:
        verdict, note = "consistent", "全部可比对字段与声明一致"
    elif ratio >= 0.5:
        verdict, note = (
            "suspicious",
            "部分字段对不上，可能是更旧的版本，或上游做了替换",
        )
    else:
        verdict, note = "inconsistent", "多数字段对不上，与声明严重不符"

    return Truthfulness(
        claim=claim_id,
        verdict=verdict,
        confidence=round(ratio, 4),
        checks=checks,
        note=note,
    )


def emit_prompts(suite: str = "standard") -> list[dict[str, str]]:
    """手动模式：导出提示词，让用户自己去问，再把回答喂回来。"""
    picked = REGISTRY.resolve(suite)
    prompts: list[dict[str, str]] = []
    for item in picked:
        if item.channel == "behavior":
            from .channels.behavior import PROBES

            for probe in PROBES:
                prompts.append(
                    {
                        "item": item.id,
                        "probe": probe["id"],
                        "label": probe["label"],
                        "prompt": probe["prompt"],
                    }
                )
        elif item.channel == "tokenizer":
            prompts.append(
                {
                    "item": item.id,
                    "probe": "usage_probe",
                    "label": "需要 usage.prompt_tokens",
                    "prompt": "请在目标端点发送本条文本并记录返回的 prompt_tokens",
                }
            )
    return prompts


def verify_from_answers(
    answers: list[dict[str, Any]],
    suite: str = "standard",
    claim: str | None = None,
    target_layer: TargetLayer = "family",
    store: BaselineStore | None = None,
) -> VerificationReport:
    """手动模式：用外部收集的回答做判定（本地不调任何 API）。"""
    store = store or BaselineStore()
    store.load()
    picked = REGISTRY.resolve(suite)

    report = VerificationReport(
        input={
            "mode": "manual",
            "suite": suite,
            "claim": claim,
            "target_layer": target_layer,
            "items": [i.id for i in picked],
        },
        baseline=_baseline_summary(store, version_prefix="baselines-local"),
    )

    from .channels.behavior import PARSERS, PROBES_BY_ID

    vector: dict[str, Any] = {}
    for answer in answers:
        probe_id = answer.get("probe")
        text = answer.get("answer") or answer.get("text") or ""
        probe = PROBES_BY_ID.get(probe_id)
        if not probe:
            continue
        parser = PARSERS.get(probe["parser"])
        vector[probe_id] = parser(text) if parser else text[:200]

    if not vector:
        report.warnings.append("没有解析到任何回答，无法判定")
        return report.finish()

    channel = CHANNELS["behavior"]
    fake_ctx = RunContext(
        adapter=None, items=picked, baselines=store.all(), declared_model=claim or ""
    )
    from ..core.types import Evidence

    ranked = channel.rank(vector, store.all())  # type: ignore[attr-defined]
    report.evidence.append(
        Evidence(
            channel="behavior",
            item="behavior.interrogate",
            target_layer="version",
            observed=f"手动录入 {len(vector)} 条回答",
            metric={"vector": vector},
            candidates=ranked[:8],
            # 没有对照数据时是「无法判定」，不是「判为不符」
            match=(ranked[0][1] >= 0.7) if ranked else None,
            weight=channel.reliability,
            note="手动模式：回答由用户在外部收集，判定基于与基准的一致性。",
        )
    )

    verdicts, conflicts = decide(
        report.evidence, store.all(), target_layer=target_layer
    )
    report.layers = verdicts
    report.conflicts = conflicts
    if claim:
        report.truthfulness = _judge_truthfulness(report, store, claim)
    report.cost = {
        "requests": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "elapsed_seconds": 0.0,
        "amount": 0.0,
        "currency": "USD",
        "note": "手动模式未经过本工具调用 API",
    }
    return report.finish()
