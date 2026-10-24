"""适配器工厂。新增厂商只需在这里注册一行。"""

from __future__ import annotations

from .anthropic import AnthropicAdapter
from .base import Adapter, AdapterConfig, detect_protocol, normalize_base
from .openai import OpenAIAdapter

ADAPTERS: dict[str, type[Adapter]] = {
    "openai": OpenAIAdapter,
    "anthropic": AnthropicAdapter,
}


def build_adapter(
    base_url: str,
    api_key: str = "",
    model: str = "",
    protocol: str | None = None,
    **kwargs,
) -> Adapter:
    """按协议构造适配器；未指定协议时自动探测。"""
    if protocol is None or protocol == "auto":
        protocol = detect_protocol(base_url, api_key)
    if protocol not in ADAPTERS:
        # 探测失败时退回 OpenAI —— 兼容端点占绝大多数，这是最有用的默认值。
        protocol = "openai"
    config = AdapterConfig(base_url=base_url, api_key=api_key, model=model, **kwargs)
    return ADAPTERS[protocol](config)


__all__ = [
    "Adapter",
    "AdapterConfig",
    "AnthropicAdapter",
    "OpenAIAdapter",
    "build_adapter",
    "detect_protocol",
    "normalize_base",
]
