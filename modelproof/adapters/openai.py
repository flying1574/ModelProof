"""OpenAI 协议适配（含绝大多数国产兼容端点）。"""

from __future__ import annotations

from typing import Any

from ..core.types import Completion
from .base import Adapter, AdapterConfig, open_request


class OpenAIAdapter(Adapter):
    protocol = "openai"

    def __init__(self, config: AdapterConfig) -> None:
        super().__init__(config)
        self.root = self._root()

    def _root(self) -> str:
        """统一成带 /v1 的根，兼容用户填 https://host 或 https://host/v1。"""
        base = self.config.base_url.rstrip("/")
        if not base.endswith("/v1"):
            base = f"{base}/v1"
        return base

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        return headers

    # -- 接口实现 ---------------------------------------------------------

    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int = 64,
        temperature: float | None = None,
        stream: bool = False,
    ) -> Completion:
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "stream": stream,
        }
        if temperature is not None:
            payload["temperature"] = temperature

        url = f"{self.root}/chat/completions"
        try:
            status, body, headers, elapsed = self._request(
                url, payload, headers=self._headers()
            )
        except Exception as error:  # noqa: BLE001 - 网络层异常统一转 Completion
            return Completion(status=0, error=str(error))

        if status >= 400 or "error" in body:
            message = (body.get("error") or {}).get("message") or body.get("message")
            return Completion(
                status=status,
                error=str(message or f"HTTP {status}"),
                headers=headers,
                raw=body,
                elapsed_seconds=elapsed,
            )

        text = ""
        choices = body.get("choices") or []
        if choices:
            choice = choices[0]
            if isinstance(choice.get("message"), dict):
                content = choice["message"].get("content")
                if isinstance(content, list):
                    text = "".join(
                        part.get("text", "")
                        for part in content
                        if isinstance(part, dict)
                    )
                else:
                    text = content or ""
            else:
                text = choice.get("text") or ""

        usage = body.get("usage") or {}
        return Completion(
            text=text,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            system_fingerprint=body.get("system_fingerprint"),
            model=body.get("model"),
            status=status,
            headers=headers,
            raw=body,
            elapsed_seconds=elapsed,
        )

    def stream_collect(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int = 32,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """流式采集一次调用链的时序数据。

        返回 TTFT、chunk 间隔序列、总时长、token 数。这些数据构成一条
        trace —— 它描述的是「这个后端怎么把 token 吐出来」，用于后端一致性
        判断，**不做身份判定**（负载、批处理都会影响它）。
        """
        import json as _json
        import time
        import urllib.request

        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        payload = {
            "model": self.config.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "stream": True,
        }
        headers = self._headers()
        url = f"{self.root}/chat/completions"

        data = _json.dumps(payload).encode("utf-8")
        merged = {"Content-Type": "application/json"}
        merged.update(headers)
        request = urllib.request.Request(url, data=data, headers=merged, method="POST")

        started = time.monotonic()
        first_at: float | None = None
        arrivals: list[float] = []
        text_parts: list[str] = []
        fingerprint: str | None = None
        usage: dict[str, Any] = {}
        error: str | None = None

        try:
            with open_request(
                request, timeout or self.config.timeout
            ) as resp:
                for raw_line in resp:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line or not line.startswith("data:"):
                        continue
                    body = line[5:].strip()
                    if body == "[DONE]":
                        break
                    try:
                        chunk = _json.loads(body)
                    except ValueError:
                        continue
                    now = time.monotonic()
                    if first_at is None:
                        first_at = now
                    arrivals.append(now)
                    if fingerprint is None:
                        fingerprint = chunk.get("system_fingerprint")
                    for choice in chunk.get("choices") or []:
                        delta = choice.get("delta") or {}
                        if isinstance(delta.get("content"), str):
                            text_parts.append(delta["content"])
                    if chunk.get("usage"):
                        usage = chunk["usage"]
        except Exception as exc:  # noqa: BLE001
            error = str(exc)

        total_ms = (time.monotonic() - started) * 1000
        ttft_ms = (first_at - started) * 1000 if first_at is not None else None

        intervals: list[float] = []
        if len(arrivals) >= 2:
            intervals = [
                round((arrivals[i] - arrivals[i - 1]) * 1000, 2)
                for i in range(1, len(arrivals))
            ]

        return {
            "protocol": "openai",
            "stream_supported": error is None and len(arrivals) > 0,
            "ttft_ms": round(ttft_ms, 2) if ttft_ms is not None else None,
            "total_ms": round(total_ms, 2),
            "chunk_count": len(arrivals),
            "inter_chunk_ms": intervals,
            "text": "".join(text_parts),
            "prompt_tokens": usage.get("prompt_tokens"),
            "output_tokens": usage.get("completion_tokens"),
            "system_fingerprint": fingerprint,
            "error": error,
        }

    def count_tokens(self, text: str) -> int | None:
        """借 usage.prompt_tokens 反推 token 数——这正是 C1 通道的原理。"""
        completion = self.complete(text, max_tokens=1)
        if not completion.ok or completion.prompt_tokens is None:
            return None
        return completion.prompt_tokens

    def count_tokens_batch(self, texts: list[str]) -> list[int | None]:
        """逐条问。并发是 orchestrator 的事，适配器保持简单。"""
        return [self.count_tokens(t) for t in texts]

    def list_models(self) -> tuple[list[str], dict[str, Any]]:
        try:
            status, body, headers, _ = self._request(
                f"{self.root}/models", None, method="GET", headers=self._headers()
            )
        except Exception as error:  # noqa: BLE001
            return [], {"status": 0, "error": str(error)}
        ids: list[str] = []
        for entry in body.get("data") or []:
            if isinstance(entry, dict) and entry.get("id"):
                ids.append(entry["id"])
        return ids, {"status": status, "headers": headers, "raw": body}
