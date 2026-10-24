"""Anthropic 协议适配。

Anthropic 与 OpenAI 有几处关键差异，适配层必须抹平：
  - 鉴权用 x-api-key + anthropic-version，不是 Bearer
  - system 是顶层字段，不是 messages 里的一条
  - usage 字段名是 input_tokens / output_tokens
  - **没有 system_fingerprint**（它改用带日期的模型名，如 claude-sonnet-4-5-20250929）
"""

from __future__ import annotations

from typing import Any

from ..core.types import Completion
from .base import Adapter, AdapterConfig, open_request

ANTHROPIC_VERSION = "2023-06-01"


class AnthropicAdapter(Adapter):
    protocol = "anthropic"

    def __init__(self, config: AdapterConfig) -> None:
        super().__init__(config)
        self.root = self.config.base_url.rstrip("/")
        if not self.root.endswith("/v1"):
            self.root = f"{self.root}/v1"

    def _headers(self) -> dict[str, str]:
        headers = {"anthropic-version": ANTHROPIC_VERSION}
        if self.config.api_key:
            headers["x-api-key"] = self.config.api_key
        return headers

    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int = 64,
        temperature: float | None = None,
        stream: bool = False,
    ) -> Completion:
        payload: dict[str, Any] = {
            "model": self.config.model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
            "stream": stream,
        }
        if system:
            payload["system"] = system
        if temperature is not None:
            payload["temperature"] = temperature

        url = f"{self.root}/messages"
        try:
            status, body, headers, elapsed = self._request(
                url, payload, headers=self._headers()
            )
        except Exception as error:  # noqa: BLE001
            return Completion(status=0, error=str(error))

        if status >= 400 or body.get("type") == "error":
            message = (body.get("error") or {}).get("message")
            return Completion(
                status=status,
                error=str(message or f"HTTP {status}"),
                headers=headers,
                raw=body,
                elapsed_seconds=elapsed,
            )

        text = ""
        for block in body.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "text":
                text += block.get("text", "")

        usage = body.get("usage") or {}
        return Completion(
            text=text,
            prompt_tokens=usage.get("input_tokens"),
            completion_tokens=usage.get("output_tokens"),
            # Anthropic 不返回 system_fingerprint；用带日期的模型名兜底。
            system_fingerprint=None,
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
        """Anthropic 的 SSE 与 OpenAI 不同：事件有 event 行，delta 在
        content_block_delta 里，token 计数在 message_delta 事件里。"""
        import json as _json
        import time
        import urllib.request

        payload: dict[str, Any] = {
            "model": self.config.model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
            "stream": True,
        }
        if system:
            payload["system"] = system

        headers = {"Content-Type": "application/json", "anthropic-version": ANTHROPIC_VERSION}
        if self.config.api_key:
            headers["x-api-key"] = self.config.api_key

        data = _json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{self.root}/messages", data=data, headers=headers, method="POST"
        )

        started = time.monotonic()
        first_at: float | None = None
        arrivals: list[float] = []
        text_parts: list[str] = []
        output_tokens: int | None = None
        error: str | None = None

        try:
            with open_request(
                request, timeout or self.config.timeout
            ) as resp:
                for raw_line in resp:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line or not line.startswith("data:"):
                        continue
                    try:
                        event = _json.loads(line[5:].strip())
                    except ValueError:
                        continue
                    kind = event.get("type")
                    now = time.monotonic()
                    if kind == "content_block_delta":
                        if first_at is None:
                            first_at = now
                        arrivals.append(now)
                        delta = event.get("delta") or {}
                        if isinstance(delta.get("text"), str):
                            text_parts.append(delta["text"])
                    elif kind == "message_delta":
                        usage = event.get("usage") or {}
                        if usage.get("output_tokens") is not None:
                            output_tokens = usage["output_tokens"]
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
            "protocol": "anthropic",
            "stream_supported": error is None and len(arrivals) > 0,
            "ttft_ms": round(ttft_ms, 2) if ttft_ms is not None else None,
            "total_ms": round(total_ms, 2),
            "chunk_count": len(arrivals),
            "inter_chunk_ms": intervals,
            "text": "".join(text_parts),
            "prompt_tokens": None,
            "output_tokens": output_tokens,
            # Anthropic 不返回 system_fingerprint
            "system_fingerprint": None,
            "error": error,
        }

    def count_tokens(self, text: str) -> int | None:
        completion = self.complete(text, max_tokens=1)
        if not completion.ok or completion.prompt_tokens is None:
            return None
        return completion.prompt_tokens

    def count_tokens_batch(self, texts: list[str]) -> list[int | None]:
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
