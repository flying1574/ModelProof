"""通道 ID → 中文名，以及通道的稳定排序。

为什么要单独放一个模块：orchestrator 与 report 各自抄了一份 CHANNEL_LABEL
（互相 import 会成环），registry 现在也要用 —— 再抄第三份迟早对不上。
这里没有任何 import，谁都可以拿。
"""

from __future__ import annotations

CHANNEL_LABEL: dict[str, str] = {
    "tokenizer": "词表指纹",
    "backend_fp": "后端指纹",
    "gateway": "接入链路",
    "behavior": "行为特征",
    "distribution": "数字分布指纹",
    "onetoken": "单 token 分布指纹",
    "capability": "能力悬崖",
    "style": "排版风格",
    "trace": "调用链 trace",
    "validity": "结果验真",
    "consistency": "请求一致性",
    "prepare": "准备",
    "finalize": "汇总判定",
}


def channel_name(channel_id: str) -> str:
    return CHANNEL_LABEL.get(channel_id, channel_id)


# 展示用的稳定顺序（不是权重，也不是执行顺序）。
# 硬信号在前、零成本在前，让用户看到的「大类列表」每次都一样。
CHANNEL_ORDER: list[str] = [
    "tokenizer",
    "backend_fp",
    "distribution",
    "behavior",
    "gateway",
    "onetoken",
    "consistency",
    "capability",
    "validity",
    "trace",
    "style",
]
