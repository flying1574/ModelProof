"""溯源与可信度分级。

分三级，权重不同，且**derived 永远不能单独支撑结论**：
  official  1.0  官方文档 / 官方端点自采
  community 0.8  社区成员自采，来源可查
  derived   0.5  从其他数据推导（比如厂商文档里写的上下文窗口）

没有溯源的基准一律不采信 —— 宁可少一条数据，也不要一条来路不明的
数据把结论带偏。
"""

from __future__ import annotations

TIER_WEIGHTS = {
    "official": 1.0,
    "community": 0.8,
    "derived": 0.5,
}

TIER_LABELS = {
    "official": "官方",
    "community": "社区",
    "derived": "推导",
}

TIER_NOTES = {
    "official": "来自官方文档或官方端点自采，可信度最高",
    "community": "社区成员自采，来源可查；多条互相印证时可接近官方",
    "derived": "由其他数据推导而来，只能作辅助，不能单独支撑结论",
}


def tier_weight(tier: str) -> float:
    return TIER_WEIGHTS.get(tier, 0.5)


def describe(tier: str) -> str:
    return f"{TIER_LABELS.get(tier, tier)}（权重 {tier_weight(tier):.1f}）：{TIER_NOTES.get(tier, '')}"
