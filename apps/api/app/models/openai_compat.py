"""OpenAI-compatible adapter. Disabled unless the user supplies a key.

Keys come from configuration, are never logged, and this adapter refuses to run
when privacy_level is local_only unless the caller passes explicit consent.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.models.base import (
    AdapterHealth,
    CancelToken,
    Capability,
    GenerationRequest,
    GenerationResult,
    ModelCapabilities,
    ModelInfo,
    ProviderKind,
    StreamChunk,
)

logger = logging.getLogger("nyxora.openai_compat")


class ExternalConsentError(PermissionError):
    """Raised when an external provider is called without user consent."""


class OpenAICompatibleAdapter:
    provider_kind = ProviderKind.EXTERNAL

    def __init__(
        self,
        *,
        provider: str = "openai",
        base_url: str,
        api_key: str,
        timeout_s: float = 120.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.provider = provider
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self.timeout_s = timeout_s
        self._transport = transport

    @property
    def enabled(self) -> bool:
        return bool(self._api_key)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}

    def _client(self, timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.base_url, timeout=timeout, transport=self._transport)

    def _guard(self, req: GenerationRequest) -> None:
        if not self.enabled:
            msg = f"{self.provider} adapter disabled: no API key configured"
            raise RuntimeError(msg)
        if not req.metadata.get("external_consent") and not req.metadata.get("local", False):
            msg = (
                f"{self.provider} is an external provider: data would leave this device. "
                "Set PRIVACY_LEVEL with consent and pass external_consent=True."
            )
            raise ExternalConsentError(msg)

    async def health(self) -> AdapterHealth:
        if not self.enabled:
            return AdapterHealth(ok=False, detail="no API key - adapter disabled")
        started = time.perf_counter()
        try:
            async with self._client(10.0) as client:
                resp = await client.get("/models", headers=self._headers())
                resp.raise_for_status()
        except (httpx.HTTPError, OSError) as exc:
            return AdapterHealth(
                ok=False, detail=f"{self.provider} unreachable: {type(exc).__name__}"
            )
        return AdapterHealth(
            ok=True, detail="online", latency_ms=(time.perf_counter() - started) * 1000
        )

    async def list_models(self) -> list[ModelInfo]:
        if not self.enabled:
            return []
        async with self._client(10.0) as client:
            resp = await client.get("/models", headers=self._headers())
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()
        ids = [str(m.get("id")) for m in data.get("data", []) if m.get("id")]
        return [
            ModelInfo(
                provider=self.provider,
                name=mid,
                provider_kind=ProviderKind.EXTERNAL,
                capabilities=ModelCapabilities(
                    tags={Capability.CHAT, Capability.REASONING, Capability.TOOL_USE},
                    context_window=128000,
                ),
            )
            for mid in ids
        ]

    async def capabilities(self, model: str) -> ModelCapabilities:
        return ModelCapabilities(
            tags={Capability.CHAT, Capability.REASONING, Capability.TOOL_USE},
            context_window=128000,
        )

    def _payload(self, req: GenerationRequest, *, stream: bool) -> dict[str, Any]:
        messages = [m.model_dump() for m in req.messages]
        if req.system:
            messages = [{"role": "system", "content": req.system}, *messages]
        payload: dict[str, Any] = {
            "model": req.model,
            "messages": messages,
            "stream": stream,
            "temperature": req.temperature,
        }
        if req.max_tokens is not None:
            payload["max_tokens"] = req.max_tokens
        if req.response_format == "json":
            payload["response_format"] = {"type": "json_object"}
        return payload

    async def generate(self, req: GenerationRequest, cancel: CancelToken) -> GenerationResult:
        self._guard(req)
        started = time.perf_counter()
        async with self._client(req.timeout_s) as client:
            resp = await client.post(
                "/chat/completions", json=self._payload(req, stream=False), headers=self._headers()
            )
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()
        usage = data.get("usage", {})
        return GenerationResult(
            text=str(data["choices"][0]["message"]["content"]),
            model=req.model,
            provider=self.provider,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            latency_ms=(time.perf_counter() - started) * 1000,
        )

    async def stream(
        self, req: GenerationRequest, cancel: CancelToken
    ) -> AsyncIterator[StreamChunk]:
        try:
            self._guard(req)
            async with (
                self._client(req.timeout_s) as client,
                client.stream(
                    "POST",
                    "/chat/completions",
                    json=self._payload(req, stream=True),
                    headers=self._headers(),
                ) as resp,
            ):
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if cancel.cancelled:
                        yield StreamChunk(error="cancelled", done=True)
                        return
                    if not line.startswith("data:"):
                        continue
                    body = line[5:].strip()
                    if body == "[DONE]":
                        yield StreamChunk(done=True)
                        return
                    try:
                        data: dict[str, Any] = json.loads(body)
                    except json.JSONDecodeError:
                        continue
                    delta = data.get("choices", [{}])[0].get("delta", {})
                    yield StreamChunk(text=str(delta.get("content", "")))
        except ExternalConsentError as exc:
            yield StreamChunk(error=str(exc), done=True)
        except (httpx.HTTPError, OSError) as exc:
            yield StreamChunk(
                error=f"{self.provider} stream failed: {type(exc).__name__}", done=True
            )
