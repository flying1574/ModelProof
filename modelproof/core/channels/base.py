"""通道抽象。

一条通道 = 一组观测 + 一个比对方。通道自己声明「能证明到哪一层」，
融合层据此决定它在哪几层投票 —— 通道不许越层发言。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from ..types import CostRange, Evidence, TargetLayer


@dataclass
class RunContext:
    """一次运行共享的上下文，通道只读它。"""

    adapter: Any  # adapters.base.Adapter
    declared_model: str = ""
    claim: str | None = None
    items: list[Any] = field(default_factory=list)  # 本次要跑的 TestItem
    baselines: list[Any] = field(default_factory=list)  # 候选基准条目
    budget_remaining: float | None = None
    on_progress: Any = None  # Callable[[Progress], None]
    # 全局进度映射：通道内部只知道自己那一段的计数，编排层知道整次运行的总量。
    # 把通道的 current 加上 base、total 换成全局总数，前端的「已完成/总数」
    # 才是整次运行的进度，而不是某个通道内部的进度。
    progress_base: int = 0
    progress_total: int = 0

    def wants(self, channel: str) -> bool:
        return any(item.channel == channel for item in self.items)

    def item(self, item_id: str) -> Any | None:
        for item in self.items:
            if item.id == item_id:
                return item
        return None

    def report_progress(self, message: str, done: int = 0, total: int = 0) -> None:
        """兼容旧签名：把 (message, done, total) 转成 Progress 上报。"""
        if not self.on_progress:
            return
        from ..progress import Progress

        self.on_progress(
            Progress(phase="", label=message, current=done, total=total, item=message)
        )

    def tracker(self, phase: str, label: str, total: int = 0, detail: str = ""):
        """拿到一个 ProgressTracker，通道用它逐步上报「当前在测哪一项」。"""
        from ..progress import ProgressTracker

        reporter = self.on_progress
        base = self.progress_base
        grand = self.progress_total
        if reporter is not None and grand > 0:
            def reporter(progress):  # noqa: F811 - 有意覆盖，把段内计数换算成全局计数
                progress.current = min(grand, base + progress.current)
                progress.total = grand
                self.on_progress(progress)

        return ProgressTracker(
            reporter, phase=phase, label=label, total=total, detail=detail
        )


class Channel(Protocol):
    id: str
    yields: set[TargetLayer]
    reliability: float  # 由留一法标定产出，不允许手改

    def collect(self, ctx: RunContext) -> list[Evidence]:
        ...

    def estimate(self, model: str | None = None) -> CostRange:
        ...


class BaseChannel:
    """通道基类，提供公共的候选打分与证据构造。"""

    id: str = "base"
    yields: set[TargetLayer] = set()
    reliability: float = 0.0

    def collect(self, ctx: RunContext) -> list[Evidence]:
        raise NotImplementedError

    def estimate(self, model: str | None = None) -> CostRange:
        from ..estimate import PriceTable

        return CostRange()

    # -- 工具 -------------------------------------------------------------

    def _evidence(
        self,
        item_id: str,
        layer: TargetLayer | None,
        observed: str,
        *,
        metric: dict[str, Any] | None = None,
        candidates: list[tuple[str, float]] | None = None,
        match: bool | None = None,
        note: str = "",
        weight_scale: float = 1.0,
        axis: str | None = None,
        level: str | None = None,
    ) -> Evidence:
        from ..types import Candidate

        return Evidence(
            channel=self.id,
            item=item_id,
            target_layer=layer,
            observed=observed,
            metric=metric or {},
            candidates=[Candidate(mid, score) for mid, score in (candidates or [])],
            match=match,
            weight=self.reliability * weight_scale,
            note=note,
            axis=axis,
            level=level,
        )
