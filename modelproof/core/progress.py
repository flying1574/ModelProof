"""结构化进度。

早期进度只有一个「3/37」的计数，用户看不出当前到底在测什么。
这里把进度定义成结构化对象：阶段 + 计数 + **当前项的名称或编号**，
前端据此渲染进度条与「正在测：韩文组合字母（12/37）」这样的说明。

通道只需要调用 tracker.step("探针名")，百分比与计数由这里算。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class Progress:
    phase: str = ""            # 阶段标识：tokenizer / behavior / trace …
    label: str = ""            # 阶段名：词表探针
    current: int = 0
    total: int = 0
    item: str = ""             # 当前项：探针名 / 模型名 / 指标项
    detail: str = ""           # 补充说明
    done: bool = False

    @property
    def pct(self) -> float:
        if self.total <= 0:
            return 100.0 if self.done else 0.0
        return round(min(100.0, max(0.0, self.current / self.total * 100)), 1)

    @property
    def counter(self) -> str:
        return f"{self.current}/{self.total}" if self.total else ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "label": self.label,
            "current": self.current,
            "total": self.total,
            "item": self.item,
            "detail": self.detail,
            "pct": self.pct,
            "counter": self.counter,
            "done": self.done,
        }

    def describe(self) -> str:
        """给 CLI 用的一行文本。"""
        head = f"{self.label or self.phase}"
        if self.counter:
            head += f" {self.counter}"
        if self.item:
            head += f" · {self.item}"
        return head


class ProgressTracker:
    """通道里的进度上报助手。

    用法：
        tracker = ProgressTracker(ctx, phase="tokenizer", label="词表探针", total=37)
        for probe in probes:
            tracker.step(probe["label"])
            ...
        tracker.finish()
    """

    def __init__(
        self,
        reporter: Callable[[Progress], None] | None,
        *,
        phase: str = "",
        label: str = "",
        total: int = 0,
        detail: str = "",
    ) -> None:
        self._reporter = reporter
        self.phase = phase
        self.label = label or phase
        self.total = total
        self.detail = detail
        self.current = 0

    def _emit(self, item: str = "", done: bool = False) -> None:
        if not self._reporter:
            return
        self._reporter(
            Progress(
                phase=self.phase,
                label=self.label,
                current=self.current,
                total=self.total,
                item=item,
                detail=self.detail,
                done=done,
            )
        )

    def start(self, item: str = "") -> None:
        self.current = 0
        self._emit(item)

    def step(self, item: str = "", advance: int = 1) -> None:
        """完成一项并上报。item 是刚刚完成的那一项目名。"""
        self.current += advance
        self._emit(item)

    def begin(self, item: str = "") -> None:
        """上报「正在处理 item」，此时它还没计入完成数。

        前端据此显示「正在测：韩文组合字母（11/37）」—— 名称是当前项，
        计数是已完成数，两者语义不同，所以要分开。
        """
        self._emit(item)

    def advance(self, n: int = 1) -> None:
        """完成当前项，计数 +1（不改变当前项名称）。"""
        self.current += n

    def note(self, text: str) -> None:
        """不定进度的说明性事件（比如「探测协议」）。"""
        if not self._reporter:
            return
        self._reporter(
            Progress(phase=self.phase, label=self.label, current=self.current,
                     total=self.total, item=text, detail=self.detail)
        )

    def finish(self) -> None:
        self.current = self.total or self.current
        self._emit(done=True)


def legacy_adapter(callback: Callable[[str, int, int], None] | None) -> Callable[[Progress], None] | None:
    """把旧的 (message, done, total) 回调包装成新的 Progress 回调。

    CLI 那边还在用旧签名，这样不用改调用方也能拿到 item 信息。
    """
    if callback is None:
        return None

    def wrapped(progress: Progress) -> None:
        # 计数由回调自己拼成 (done/total)，这里只给「阶段 + 当前项」
        text = progress.label or progress.phase or ""
        if progress.item:
            text = f"{text} · {progress.item}" if text else progress.item
        callback(text or progress.describe(), progress.current, progress.total)

    return wrapped
