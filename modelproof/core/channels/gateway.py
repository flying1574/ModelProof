"""C5 —— 接入链路探测。

这条通道**不问模型身份，只问「这个入口是谁搭的」**。它与身份正交：
一个端点背后的模型可以是真的 Claude，但入口可能是别人搭的壳。

两轴必须分开报（借鉴 APIScamHunter 的做法）：
  - 链路轴（link）：中间有几层。有中间层不等于有问题 —— 公司网关、
    Cloudflare、云厂商网关都很常见。
  - 行为轴（behaviour）：它在不在改你的流量。只有这一轴才可能有恶意含义，
    且**必须 ≥2 个独立信号**才下结论，绝不能靠单条信号指控。

另外一条硬纪律：只在拿到 2xx 业务响应时才判断。连不上就是连不上，
不能把「打不通」当成「可疑」。
"""

from __future__ import annotations

from typing import Any

from ..types import Evidence
from .base import BaseChannel, RunContext

# 官方直连域名（含官方云网关之外的自营域名）
OFFICIAL_HOSTS = {
    "api.openai.com": "openai",
    "api.anthropic.com": "anthropic",
    "generativelanguage.googleapis.com": "google",
    "api.deepseek.com": "deepseek",
    "dashscope.aliyuncs.com": "qwen",
    "dashscope-intl.aliyuncs.com": "qwen",
    "api.moonshot.cn": "moonshot",
    "api.moonshot.ai": "moonshot",
    "open.bigmodel.cn": "zhipu",
    "api.z.ai": "zhipu",
    "api.kimi.com": "kimi",
    "api.x.ai": "grok",
    "api.mistral.ai": "mistral",
    "api.minimaxi.com": "minimax",
    "api.minimax.chat": "minimax",
}

# 官方云网关：模型是真的，入口是云厂商的
CLOUD_GATEWAY_PATTERNS = [
    ("openai.azure.com", "azure-openai"),
    ("azure.com", "azure"),
    ("bedrock-runtime", "aws-bedrock"),
    ("amazonaws.com", "aws"),
    ("aiplatform.googleapis.com", "gcp-vertex"),
    ("googleapis.com", "gcp"),
    ("volces.com", "volcano"),
    ("volcengineapi.com", "volcano"),
    ("aliyuncs.com", "aliyun"),
    ("tencentcloudapi.com", "tencent"),
    ("myqcloud.com", "tencent"),
    ("huaweicloud.com", "huawei"),
    ("baidubce.com", "baidu"),
    ("硅基流动", "siliconflow"),
    ("siliconflow.cn", "siliconflow"),
]

# 中转系统（OneAPI / New-API / 各类二开）的典型错误文案
RESELLER_ERROR_PATTERNS = [
    "无可用渠道",
    "当前分组",
    "负载已饱和",
    "无效的渠道",
    "one_api_error",
    "oneapi",
    "new-api",
    "channel not found",
    "current group load is saturated",
    "no available channel",
    "quota_exceeded",
    "余额不足",
    "剩余额度",
]

# 订阅反代的限流文案（5 小时滚动窗口）
SUBSCRIPTION_LIMIT_PATTERNS = [
    "5-hour",
    "5 hour",
    "five-hour",
    "usage limit",
    "rate limit reached",
    "hit your usage limit",
    "limit reached",
]

# 订阅反代的典型特征
SUBSCRIPTION_HINTS = ["codex-", "chatgpt-account-id", "chatgpt-", "oai-", "session_token"]

VERDICT_LABELS = {
    "official_direct": "官方直连",
    "official_cloud": "官方云网关",
    "subscription_relay": "订阅反代",
    "third_party_relay": "第三方中转",
    "unknown": "无法判定",
}


class GatewayChannel(BaseChannel):
    id = "gateway"
    yields = set()  # 不参与身份打分
    reliability = 0.0

    def collect(self, ctx: RunContext) -> list[Evidence]:
        adapter = ctx.adapter
        base_url = getattr(getattr(adapter, "config", None), "base_url", "") or ""
        host = _host_of(base_url)

        link_signals: list[dict[str, Any]] = []
        behaviour_signals: list[dict[str, Any]] = []

        # 1) 域名归属
        official = OFFICIAL_HOSTS.get(host)
        cloud = _cloud_gateway(host)
        if official:
            link_signals.append(_sig("host", f"{host} 属 {official} 官方域名", "info"))
        elif cloud:
            link_signals.append(
                _sig("host", f"{host} 匹配云网关 {cloud}", "info")
            )
        else:
            link_signals.append(_sig("host", f"{host} 非已知官方域名", "notice"))

        # 2) 响应头
        completion = adapter.complete("hi", max_tokens=1)
        headers = {k.lower(): v for k, v in (completion.headers or {}).items()}
        for name in ("server", "via", "x-powered-by", "cf-ray", "x-request-id"):
            if name in headers:
                link_signals.append(
                    _sig("header", f"{name}: {headers[name][:60]}", "info")
                )
        for name in SUBSCRIPTION_HINTS:
            if name in headers:
                behaviour_signals.append(
                    _sig("header", f"出现订阅侧特征头 {name}", "notice")
                )

        # 3) usage 是否可用 —— 订阅反代的决定性特征
        if completion.ok:
            if completion.prompt_tokens is None:
                behaviour_signals.append(
                    _sig(
                        "usage",
                        "响应不带 token 计数",
                        "notice",
                        "ChatGPT/Codex 网页与 Claude 订阅后端不返回 token 计数，"
                        "这是订阅反代的决定性特征",
                    )
                )
            elif completion.prompt_tokens == 0:
                behaviour_signals.append(
                    _sig("usage", "token 计数全为 0", "notice")
                )
        elif completion.error:
            text = completion.error.lower()
            for pattern in RESELLER_ERROR_PATTERNS:
                if pattern.lower() in text:
                    behaviour_signals.append(
                        _sig(
                            "error_body",
                            f"错误文案命中中转系统特征「{pattern}」",
                            "notice",
                        )
                    )
                    break
            for pattern in SUBSCRIPTION_LIMIT_PATTERNS:
                if pattern.lower() in text:
                    behaviour_signals.append(
                        _sig("error_body", f"限流文案命中订阅侧特征「{pattern}」", "notice")
                    )
                    break

        # 4) 模型列表与端点面
        models, meta = adapter.list_models()
        raw = meta.get("raw") or {}
        if models:
            link_signals.append(
                _sig("models", f"/models 返回 {len(models)} 个模型", "info")
            )
            # Anthropic 端点却返回 OpenAI 的 {"object":"list"} 形状 → 大概率是壳
            if raw.get("object") == "list" and adapter.protocol == "anthropic":
                behaviour_signals.append(
                    _sig(
                        "shape",
                        "Anthropic 端点返回 OpenAI 的 list 形状",
                        "notice",
                        "协议形状不符，通常是外层套了兼容层",
                    )
                )
        elif not completion.ok and not models:
            return [
                self._evidence(
                    "gateway.probe",
                    None,
                    "未拿到任何业务响应，无法判定链路性质",
                    match=None,
                    note="端点不可达或鉴权失败。打不通不等于可疑，请先确认地址与密钥。",
                    axis="link",
                    level="info",
                    weight_scale=0.0,
                )
            ]

        # 5) 判定
        verdict, confidence, reason = _decide(
            official, cloud, host, behaviour_signals, completion
        )

        behaviour_count = len(behaviour_signals)
        note = reason
        if behaviour_count >= 2:
            note += f"；行为轴共 {behaviour_count} 个独立信号"
        elif behaviour_count == 1:
            note += "；行为轴仅 1 个信号，不足以定性"

        return [
            self._evidence(
                "gateway.probe",
                None,
                VERDICT_LABELS.get(verdict, verdict),
                metric={
                    "verdict": verdict,
                    "label": VERDICT_LABELS.get(verdict, verdict),
                    "confidence": confidence,
                    "host": host,
                    "reason": reason,
                    "link_signals": link_signals,
                    "behaviour_signals": behaviour_signals,
                    "link_signal_count": len(link_signals),
                    "behaviour_signal_count": behaviour_count,
                },
                match=None,
                note=note,
                axis="behaviour" if behaviour_count >= 2 else "link",
                level=(
                    "warning" if behaviour_count >= 2
                    else ("notice" if behaviour_count == 1 else "info")
                ),
                weight_scale=0.0,  # 不参与身份打分
            )
        ]


def _sig(kind: str, text: str, level: str, note: str = "") -> dict[str, Any]:
    return {"kind": kind, "text": text, "level": level, "note": note}


def _host_of(base_url: str) -> str:
    from ...adapters.base import normalize_base

    host, _port, _root = normalize_base(base_url)
    return host


def _cloud_gateway(host: str) -> str | None:
    for pattern, label in CLOUD_GATEWAY_PATTERNS:
        if pattern in host:
            return label
    return None


def _decide(
    official: str | None,
    cloud: str | None,
    host: str,
    behaviour: list[dict[str, Any]],
    completion: Any,
) -> tuple[str, float, str]:
    """五档判定。返回 (verdict, confidence, reason)。"""
    kinds = {s["kind"] for s in behaviour}

    # 连不上：直接弃权
    if not completion.ok and not completion.headers and not completion.error:
        return "unknown", 0.0, "未拿到业务响应（2xx–4xx），不予判定"

    # 订阅反代：usage 缺失是决定性特征
    if "usage" in kinds:
        extra = 1 if len(behaviour) >= 2 else 0
        return (
            "subscription_relay",
            round(0.65 + 0.15 * extra, 2),
            "响应不带 token 计数，符合 ChatGPT/Codex 与 Claude 订阅后端特征"
            + ("，且有第二个独立信号佐证" if extra else ""),
        )

    # 第三方中转：错误文案或形状不符
    if "error_body" in kinds or "shape" in kinds:
        return (
            "third_party_relay",
            0.8 if len(behaviour) >= 2 else 0.55,
            "命中中转系统特征"
            + ("（多信号互证）" if len(behaviour) >= 2 else "（单信号，供参考）"),
        )

    if official:
        return "official_direct", 0.9, f"{host} 为官方域名，未发现中转特征"
    if cloud:
        return "official_cloud", 0.8, f"{host} 为官方云网关（{cloud}）"

    if behaviour:
        return (
            "third_party_relay",
            0.5,
            f"{host} 非官方域名，且有 {len(behaviour)} 个行为信号",
        )

    return "official_direct", 0.5, f"{host} 非已知官方域名，但未发现中转特征"
