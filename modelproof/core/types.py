"""ModelProof 的公共类型。

这一层刻意不依赖任何第三方包：核心判定逻辑必须能在只有标准库的
环境里跑起来（CLI、CI、离线采集都靠它）。Web / rich 渲染是可选外壳。
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Literal

# --------------------------------------------------------------------------
# 判定层
# --------------------------------------------------------------------------

TargetLayer = Literal["vendor", "family", "series", "version"]

LAYER_ORDER: tuple[TargetLayer, ...] = ("vendor", "family", "series", "version")

# v0.1 目标阈值，由留一法标定脚本产出后覆盖（见 core/fusion/calibrate.py）。
DEFAULT_LAYER_THRESHOLDS: dict[TargetLayer, float] = {
    "vendor": 0.98,
    "family": 0.95,
    "series": 0.85,
    "version": 0.70,
}

# v0.1 目标层温度，同样由标定产出。
DEFAULT_LAYER_BETAS: dict[TargetLayer, float] = {
    "vendor": 14.0,
    "family": 9.0,
    "series": 6.0,
    "version": 4.0,
}


# --------------------------------------------------------------------------
# 成本
# --------------------------------------------------------------------------


@dataclass
class Cost:
    """一次测试项（或一整个套件）的消耗声明。"""

    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0.0
    amount: float = 0.0
    currency: str = "USD"

    def add(self, other: "Cost") -> "Cost":
        return Cost(
            requests=self.requests + other.requests,
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            seconds=self.seconds + other.seconds,
            amount=self.amount + other.amount,
            currency=other.currency or self.currency,
        )

    def scaled(self, factor: float) -> "Cost":
        return Cost(
            requests=int(round(self.requests * factor)),
            input_tokens=int(round(self.input_tokens * factor)),
            output_tokens=int(round(self.output_tokens * factor)),
            seconds=self.seconds * factor,
            amount=self.amount * factor,
            currency=self.currency,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CostRange:
    """预估是区间，不是单点——输出 token 长度本身就不可控。"""

    requests: int = 0
    input_tokens: tuple[int, int] = (0, 0)
    output_tokens: tuple[int, int] = (0, 0)
    seconds: tuple[float, float] = (0.0, 0.0)
    amount: tuple[float, float] = (0.0, 0.0)
    currency: str = "USD"

    @property
    def expected(self) -> Cost:
        def mid(pair: tuple[float, float]) -> float:
            return (pair[0] + pair[1]) / 2

        return Cost(
            requests=self.requests,
            input_tokens=int(round(mid(self.input_tokens))),
            output_tokens=int(round(mid(self.output_tokens))),
            seconds=mid(self.seconds),
            amount=mid(self.amount),
            currency=self.currency,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "requests": self.requests,
            "input_tokens": list(self.input_tokens),
            "output_tokens": list(self.output_tokens),
            "seconds": list(self.seconds),
            "amount": list(self.amount),
            "currency": self.currency,
        }


# --------------------------------------------------------------------------
# 协议适配输出
# --------------------------------------------------------------------------


@dataclass
class Completion:
    """一次补全调用的统一视图。

    `raw` 保留厂商原始响应体，通道需要挖非标准字段时用得上（比如各家
    system_fingerprint 的位置和名字都不一样）。
    """

    text: str = ""
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    system_fingerprint: str | None = None
    model: str | None = None
    status: int = 0
    headers: dict[str, str] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)
    elapsed_seconds: float = 0.0
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and 200 <= self.status < 300

    @property
    def has_usage(self) -> bool:
        return self.prompt_tokens is not None

    def cost(self) -> Cost:
        return Cost(
            requests=1,
            input_tokens=self.prompt_tokens or 0,
            output_tokens=self.completion_tokens or 0,
            seconds=self.elapsed_seconds,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "system_fingerprint": self.system_fingerprint,
            "model": self.model,
            "status": self.status,
            "headers": self.headers,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "error": self.error,
        }


# --------------------------------------------------------------------------
# 证据
# --------------------------------------------------------------------------


@dataclass
class Candidate:
    """一条证据在某个候选上的得分。"""

    id: str
    score: float

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "score": round(self.score, 4)}


@dataclass
class Evidence:
    """一条通道观测换算成的证据。

    `target_layer` 说明这条证据能证明到哪一层；`weight` = 通道可靠性 ×
    测试项权重，由标定产出，不允许手改。
    """

    channel: str
    item: str
    target_layer: TargetLayer | None = None
    observed: str = ""
    metric: dict[str, Any] = field(default_factory=dict)
    candidates: list[Candidate] = field(default_factory=list)
    match: bool | None = None
    weight: float = 0.0
    note: str = ""
    # 链路通道专用：不参与身份打分
    axis: str | None = None
    level: str | None = None
    # 溯源：这条证据比对时用的基准
    baseline_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = {
            "channel": self.channel,
            "item": self.item,
            "layer": self.target_layer,
            "observed": self.observed,
            "match": self.match,
            "weight": round(self.weight, 4),
        }
        if self.metric:
            data["metric"] = self.metric
        if self.candidates:
            data["candidates"] = [c.to_dict() for c in self.candidates]
        if self.note:
            data["note"] = self.note
        if self.axis:
            data["axis"] = self.axis
        if self.level:
            data["level"] = self.level
        if self.baseline_id:
            data["baseline_id"] = self.baseline_id
        return data


# --------------------------------------------------------------------------
# 分层结论
# --------------------------------------------------------------------------


@dataclass
class LayerVerdict:
    value: str | None = None
    confidence: float = 0.0
    abstained: bool = False
    abstain_reason: str | None = None
    detail: str = ""
    top_candidates: list[Candidate] = field(default_factory=list)
    threshold: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "value": self.value,
            "confidence": round(self.confidence, 4),
            "abstained": self.abstained,
            "top_candidates": [c.to_dict() for c in self.top_candidates],
            "threshold": self.threshold,
        }
        if self.abstain_reason:
            data["abstain_reason"] = self.abstain_reason
        if self.detail:
            data["detail"] = self.detail
        return data


@dataclass
class TruthfulnessCheck:
    field: str
    claimed: Any = None
    observed: Any = None
    match: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "claimed": self.claimed,
            "observed": self.observed,
            "match": self.match,
        }


@dataclass
class Truthfulness:
    """真伪模式（P）结论：它是不是自己声称的那个。"""

    claim: str | None = None
    verdict: str = "unknown"  # consistent | suspicious | inconsistent | unknown
    confidence: float = 0.0
    checks: list[TruthfulnessCheck] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim": self.claim,
            "verdict": self.verdict,
            "confidence": round(self.confidence, 4),
            "checks": [c.to_dict() for c in self.checks],
            "note": self.note,
        }


@dataclass
class Conflict:
    """通道之间打架的记录。冲突往往是最有价值的发现，不是要消除的噪声。"""

    layer: TargetLayer
    channels: list[str]
    values: list[str]
    severity: str = "soft"  # soft | hard
    penalty: float = 1.0
    hint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "layer": self.layer,
            "channels": self.channels,
            "values": self.values,
            "severity": self.severity,
            "penalty": self.penalty,
            "hint": self.hint,
        }
