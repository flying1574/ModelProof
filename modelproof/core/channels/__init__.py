"""通道注册。新增通道在这里挂一行，融合层自动接管。"""

from __future__ import annotations

from .backend_fp import BackendFingerprintChannel
from .base import BaseChannel, RunContext
from .behavior import BehaviorChannel
from .capability import CapabilityChannel
from .consistency import ConsistencyChannel
from .distribution import DistributionChannel
from .gateway import GatewayChannel
from .onetoken import OneTokenChannel
from .style import StyleChannel
from .tokenizer import TokenizerChannel
from .trace import TraceChannel
from .validity import ValidityChannel

CHANNELS: dict[str, BaseChannel] = {
    channel.id: channel
    for channel in (
        TokenizerChannel(),
        BackendFingerprintChannel(),
        GatewayChannel(),
        BehaviorChannel(),
        OneTokenChannel(),
        DistributionChannel(),
        CapabilityChannel(),
        StyleChannel(),
        TraceChannel(),
        ValidityChannel(),
        ConsistencyChannel(),
    )
}

# 不参与身份打分的通道 —— 它们回答的是别的问题：
#   gateway      入口是谁搭的
#   trace        后端时序一致性（受负载影响，不能定性）
#   validity     这次结果能不能信
#   consistency  这个入口对你发出的每个请求是否始终如一
NON_IDENTITY_CHANNELS = {"gateway", "trace", "validity", "consistency"}


def channels_for(items: list) -> list[BaseChannel]:
    wanted = {item.channel for item in items}
    return [ch for cid, ch in CHANNELS.items() if cid in wanted]


__all__ = [
    "BaseChannel",
    "BehaviorChannel",
    "BackendFingerprintChannel",
    "CapabilityChannel",
    "CHANNELS",
    "ConsistencyChannel",
    "DistributionChannel",
    "GatewayChannel",
    "NON_IDENTITY_CHANNELS",
    "OneTokenChannel",
    "StyleChannel",
    "RunContext",
    "TokenizerChannel",
    "TraceChannel",
    "ValidityChannel",
    "channels_for",
]
