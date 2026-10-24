"""协议适配基类与共享 HTTP 工具。

只用标准库：适配器要能在离线 CI 和最小容器里工作，不值得为一个 HTTP
客户端拖进 requests。
"""

from __future__ import annotations

import ipaddress
import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from ..core.types import Completion

USER_AGENT = "ModelProof/0.1 (+https://github.com/modelproof/modelproof)"

RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}

# 环回地址直连用的 opener。环境里若设了 http_proxy/HTTP_PROXY，urllib 会把
# 本机端点的请求也送进代理 —— 代理要么返回 502，要么直接挂住，探测就会
# 莫名其妙地"连不上"。本机端点不该经过任何中间人。
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def is_loopback(url: str) -> bool:
    """URL 的主机是否是环回地址（127.0.0.1 / ::1 / localhost）。"""
    host = (urllib.parse.urlparse(url).hostname or "").strip().lower()
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def open_request(request: urllib.request.Request, timeout: float):
    """发一个请求：环回地址直连，其余照环境代理。"""
    if is_loopback(request.full_url):
        return _NO_PROXY_OPENER.open(request, timeout=timeout)
    return urllib.request.urlopen(request, timeout=timeout)


@dataclass
class AdapterConfig:
    base_url: str
    api_key: str = ""
    model: str = ""
    timeout: float = 45.0
    max_retries: int = 2
    extra_headers: dict[str, str] = field(default_factory=dict)
    # 是否把 api_key 打进日志（默认绝不）
    verbose: bool = False


class AdapterError(RuntimeError):
    pass


class Adapter:
    """统一补全接口。子类只需实现协议差异部分。"""

    protocol: str = "unknown"

    def __init__(self, config: AdapterConfig) -> None:
        self.config = config

    # -- 子类实现 ---------------------------------------------------------

    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int = 64,
        temperature: float | None = None,
        stream: bool = False,
    ) -> Completion:
        raise NotImplementedError

    def count_tokens(self, text: str) -> int | None:
        raise NotImplementedError

    def list_models(self) -> tuple[list[str], dict[str, Any]]:
        raise NotImplementedError

    # -- 共享工具 ---------------------------------------------------------

    def _request(
        self,
        url: str,
        payload: dict[str, Any] | None = None,
        *,
        method: str = "POST",
        headers: dict[str, str] | None = None,
        retries: int | None = None,
    ) -> tuple[int, dict[str, Any], dict[str, str], float]:
        """返回 (status, body, headers, elapsed)。网络层失败抛 AdapterError。"""
        retries = self.config.max_retries if retries is None else retries
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        merged = {"User-Agent": USER_AGENT, "Content-Type": "application/json"}
        merged.update(self.config.extra_headers)
        if headers:
            merged.update(headers)

        last_error: Exception | None = None
        for attempt in range(1, retries + 2):
            request = urllib.request.Request(url, data=data, headers=merged, method=method)
            started = time.monotonic()
            try:
                with open_request(request, self.config.timeout) as resp:
                    raw = resp.read()
                    elapsed = time.monotonic() - started
                    body = _parse_json(raw)
                    return resp.status, body, dict(resp.headers.items()), elapsed
            except urllib.error.HTTPError as error:
                raw = error.read()
                elapsed = time.monotonic() - started
                body = _parse_json(raw)
                if error.code in RETRYABLE_STATUS and attempt <= retries:
                    _sleep_backoff(attempt)
                    last_error = error
                    continue
                return error.code, body, dict(error.headers.items()), elapsed
            except OSError as error:
                # URLError 是 OSError 子类，但连接被重置时 socket 直接抛
                # ConnectionResetError，urllib 不包装它 —— 不接住就会漏成崩溃。
                last_error = error
                if attempt <= retries:
                    _sleep_backoff(attempt)
                    continue
                raise AdapterError(f"无法连接 {url}：{error}") from error
        raise AdapterError(f"请求 {url} 失败：{last_error}")

    def _join(self, *parts: str) -> str:
        return "/".join(p.strip("/") for p in parts if p.strip("/"))


def _parse_json(raw: bytes) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw.decode("utf-8", errors="replace"))
    except (ValueError, UnicodeDecodeError):
        return {"_raw": raw.decode("utf-8", errors="replace")[:2000]}
    return parsed if isinstance(parsed, dict) else {"_value": parsed}


def _sleep_backoff(attempt: int) -> None:
    time.sleep(min(6.0, 0.6 * (2 ** (attempt - 1))) + random.uniform(0, 0.4))


def normalize_base(base_url: str) -> tuple[str, int, str]:
    """拆出 host / port / 根路径，供链路探测与端点去重使用。"""
    parsed = urllib.parse.urlparse(base_url if "://" in base_url else f"https://{base_url}")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return parsed.hostname or "", port, parsed.path.rstrip("/")


def detect_protocol(base_url: str, api_key: str = "", timeout: float = 10.0) -> str:
    """探测端点协议。顺序尝试 openai → anthropic，谁先给出业务响应就算谁。

    注意：只看是否返回 2xx–4xx 的业务响应；连不上时不猜。
    """
    host, port, root = normalize_base(base_url)
    if not host:
        return "unknown"
    scheme = "https" if port == 443 else ("http" if port == 80 else None)
    if scheme is None:
        # 非标准端口两种都试
        candidates = ["https", "http"]
    else:
        candidates = [scheme]

    for use_scheme in candidates:
        base = f"{use_scheme}://{host}:{port}{root}"
        # OpenAI：/models 或 /chat/completions
        status, _, _, _ = _probe_get(f"{base}/models", api_key, timeout, openai=True)
        if status and status < 500:
            return "openai"
        status, _, _, _ = _probe_get(f"{base}/v1/models", api_key, timeout, openai=True)
        if status and status < 500:
            return "openai"
        # Anthropic：/v1/messages 用 POST
        status, _, _, _ = _probe_get(f"{base}/v1/models", api_key, timeout, openai=False)
        if status and status < 500:
            return "anthropic"
    return "unknown"


def _probe_get(
    url: str, api_key: str, timeout: float, openai: bool
) -> tuple[int | None, dict[str, Any], dict[str, str], float]:
    headers = {"User-Agent": USER_AGENT}
    if api_key:
        if openai:
            headers["Authorization"] = f"Bearer {api_key}"
        else:
            headers["x-api-key"] = api_key
            headers["anthropic-version"] = "2023-06-01"
    request = urllib.request.Request(url, headers=headers, method="GET")
    started = time.monotonic()
    try:
        with open_request(request, timeout) as resp:
            resp.read()
            return resp.status, {}, dict(resp.headers.items()), time.monotonic() - started
    except urllib.error.HTTPError as error:
        error.read()
        return error.code, {}, dict(error.headers.items()), time.monotonic() - started
    except OSError:
        return None, {}, {}, time.monotonic() - started


def empty_completion(status: int = 0, error: str | None = None) -> Completion:
    return Completion(status=status, error=error)
