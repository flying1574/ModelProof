"""测试项与测试集模型。

每个测试项是原子单元，自带成本声明 —— 这是「消耗预估」和「自由组合」
能同时落地的前提：套件只是测试项的有序集合，换套件等于换一组成本元数据。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..types import CostRange, TargetLayer


@dataclass
class TestItem:
    id: str
    channel: str
    name: str
    yields: list[TargetLayer] = field(default_factory=list)
    requires: list[str] = field(default_factory=list)
    requests: int = 0
    input_tokens_range: tuple[int, int] = (0, 0)
    output_tokens_range: tuple[int, int] = (0, 0)
    manual_friendly: bool = False
    prompt: str = ""
    parser: str = ""
    description: str = ""
    enabled_by_default: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "channel": self.channel,
            "name": self.name,
            "yields": list(self.yields),
            "requires": list(self.requires),
            "cost": {
                "requests": self.requests,
                "input_tokens_range": list(self.input_tokens_range),
                "output_tokens_range": list(self.output_tokens_range),
            },
            "manual_friendly": self.manual_friendly,
            "description": self.description,
            "enabled_by_default": self.enabled_by_default,
        }

    @property
    def is_free(self) -> bool:
        """搭车采集的通道不额外花钱（backend_fp / timing）。"""
        return self.requests == 0


@dataclass
class Suite:
    id: str
    name: str
    items: list[str] = field(default_factory=list)
    description: str = ""
    builtin: bool = True

    def resolved_items(self, registry: "Any") -> list["TestItem"]:
        """把套件里的 ID 列表换成真正的测试项对象；未知 ID 直接跳过。"""
        resolved = []
        for item_id in self.items:
            item = registry.item(item_id)
            if item is not None:
                resolved.append(item)
        return resolved

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "items": list(self.items),
            "builtin": self.builtin,
        }


def item_cost(item: TestItem) -> CostRange:
    """测试项的成本区间（不含价格换算，那是 estimate 的事）。"""
    return CostRange(
        requests=item.requests,
        input_tokens=item.input_tokens_range,
        output_tokens=item.output_tokens_range,
    )
