"""C9 —— 调用链 / 推理 trace 采集。

为什么要有这条通道：
  调研（arXiv 2605.19537《The Silent Hyperparameter》）指出**推理后端是一个
  沉默的超参数**——同一个模型换后端，分数可以差到 16 个百分点，且连
  t=0 的贪心解码都不保证可复现（浮点加法不满足结合律，kernel 归约顺序
  随 batch size 变化）。该文给服务商的建议之一，正是「在响应元数据里暴露
  system fingerprint，让用户能追踪后端变化」。

本通道采集的就是这条「后端如何吐 token」的时序指纹：
  - TTFT（首 chunk 延迟）
  - chunk 间隔序列（p50 / p95）
  - 稳定期 TPS
  - 是否支持流式

**硬边界**：trace 受负载、批处理、网络影响极大，**它不做身份判定**，
只用于一致性判断（「现在的后端和上次是同一个吗」）与性能画像。
"""

from __future__ import annotations

import statistics
from datetime import datetime, timezone
from typing import Any

from ..types import CostRange, Evidence
from .base import BaseChannel, RunContext

PROBE_VERSION = "tr1-3"

PROMPT = "请从 1 数到 40，每个数字占一行。"

# 一致性判定容差：trace 抖动大，容差必须宽
CONSISTENCY_TOLERANCE = 0.35


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((pct / 100) * (len(ordered) - 1)))))
    return round(ordered[index], 2)


def summarize_samples(samples: list[dict[str, Any]]) -> dict[str, Any]:
    """把多次采样的 trace 聚合成一条稳定特征（中位数抗抖动）。"""
    ok = [s for s in samples if s.get("stream_supported")]
    if not ok:
        return {
            "probe_version": PROBE_VERSION,
            "stream_supported": False,
            "samples": len(samples),
            "note": "端点不支持流式或未返回流，无法采集 trace",
        }

    def med(key: str) -> float | None:
        values = [s[key] for s in ok if s.get(key) is not None]
        return round(statistics.median(values), 2) if values else None

    ttft = med("ttft_ms")
    total = med("total_ms")
    tps_values = []
    for s in ok:
        tokens = s.get("output_tokens")
        if tokens and s.get("total_ms") and s.get("ttft_ms") is not None:
            gen_ms = max(1.0, s["total_ms"] - s["ttft_ms"])
            tps_values.append(tokens / gen_ms * 1000)
    tps = round(statistics.median(tps_values), 2) if tps_values else None

    all_intervals: list[float] = []
    for s in ok:
        all_intervals.extend(s.get("inter_chunk_ms") or [])

    return {
        "probe_version": PROBE_VERSION,
        "stream_supported": True,
        "samples": len(ok),
        "ttft_ms": ttft,
        "total_ms": total,
        "tps": tps,
        "inter_chunk_p50_ms": _percentile(all_intervals, 50),
        "inter_chunk_p95_ms": _percentile(all_intervals, 95),
        "chunk_count": med("chunk_count"),
        "collected_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        # 保留原始样本，便于事后查看抖动范围
        "raw_samples": [
            {
                "ttft_ms": s.get("ttft_ms"),
                "total_ms": s.get("total_ms"),
                "chunk_count": s.get("chunk_count"),
                "output_tokens": s.get("output_tokens"),
                "system_fingerprint": s.get("system_fingerprint"),
            }
            for s in ok
        ],
    }


class TraceChannel(BaseChannel):
    id = "trace"
    yields = set()          # 不做身份判定
    reliability = 0.15      # 只用于一致性，权重最低

    def collect(self, ctx: RunContext) -> list[Evidence]:
        item = ctx.item("trace.stream_timing")
        if item is None:
            return []

        adapter = ctx.adapter
        if not hasattr(adapter, "stream_collect"):
            return [
                self._evidence("trace.stream_timing", None,
                               "当前适配器不支持流式采集", match=None, weight_scale=0.0)
            ]

        samples_n = 3
        tracker = ctx.tracker("trace", "调用链采样", total=samples_n,
                              detail="测量 TTFT 与 chunk 间隔")
        samples: list[dict[str, Any]] = []
        for i in range(samples_n):
            tracker.begin(f"第 {i + 1} 次采样")
            samples.append(adapter.stream_collect(PROMPT, max_tokens=120))
            tracker.advance()
        tracker.finish()

        summary = summarize_samples(samples)
        if not summary.get("stream_supported"):
            return [
                self._evidence(
                    "trace.stream_timing", None,
                    "端点未返回流式数据，trace 不可用",
                    match=None,
                    note="可能不支持 stream，或中间层把流式拆成了整段返回。",
                    weight_scale=0.0,
                )
            ]

        described = (
            f"TTFT {summary['ttft_ms']}ms · "
            f"稳定期 {summary['tps']} tok/s · "
            f"chunk 间隔 p50 {summary['inter_chunk_p50_ms']}ms / "
            f"p95 {summary['inter_chunk_p95_ms']}ms"
        )

        # 与基准比对：只报一致性，不参与身份打分
        consistency = None
        note = "trace 只用于一致性判断，不参与身份判定。"
        for entry in ctx.baselines:
            reference = (entry.channels or {}).get("trace")
            if not reference or reference.get("probe_version") != PROBE_VERSION:
                continue
            ratio = compare_trace(summary, reference)
            if ratio is None:
                continue
            consistency = ratio
            if ratio >= 0.65:
                note = f"与 {entry.model_id} 的 trace 特征一致（{ratio:.0%}），像是同一后端。"
            else:
                note = (
                    f"与 {entry.model_id} 的 trace 特征差异较大（{ratio:.0%}）："
                    f"可能是换后端了，也可能只是负载不同 —— trace 抖动大，不足以定性。"
                )
            break

        return [
            self._evidence(
                "trace.stream_timing", None, described,
                metric={"summary": summary, "consistency": consistency},
                match=None,
                note=note,
                weight_scale=0.0,   # 不参与身份打分
            )
        ]


def compare_trace(observed: dict[str, Any], reference: dict[str, Any]) -> float | None:
    """trace 一致性：逐项算相对差，取平均相似度。抖动大，容差宽。"""
    keys = ("ttft_ms", "tps", "inter_chunk_p50_ms", "chunk_count")
    scores: list[float] = []
    for key in keys:
        a = observed.get(key)
        b = reference.get(key)
        if a is None or b is None or b == 0:
            continue
        diff = abs(float(a) - float(b)) / abs(float(b))
        scores.append(max(0.0, 1.0 - diff / CONSISTENCY_TOLERANCE))
    if not scores:
        return None
    return round(sum(scores) / len(scores), 4)
