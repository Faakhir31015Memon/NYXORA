"""Ollama adapter: local, free, private. Full streaming, health, retry, cancellation."""

from __future__ import annotations

import asyncio
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

logger = logging.getLogger("nyxora.ollama")

# Capability inference from tag name only. Never a guarantee - the user's
# config/models.yaml is authoritative and overrides these guesses.
_TAG_HINTS: dict[str, set[Capability]] = {
    "coder": {Capability.CHAT, Capability.CODING},
    "embed": {Capability.EMBEDDING},
    "vl": {Capability.CHAT, Capability.VISION},
    "llava": {Capability.CHAT, Capability.VISION},
    "minil": set(),
}


def _infer_capabilities(name: str) -> ModelCapabilities:
    tags: set[Capability] = {Capability.CHAT, Capability.REASONING}
    lowered = name.lower()
    for needle, extra in _TAG_HINTS.items():
        if needle in lowered:
            tags |= extra
    if "embed" in lowered:
        tags = {Capability.EMBEDDING}
    if any(s in lowered for s in ("32b", "70b", "14b", "3.2", "long")):
        tags.add(Capability.LONG_CONTEXT)
    return ModelCapabilities(tags=tags, context_window=8192, supports_json=True)


class OllamaAdapter:
    provider = "ollama"
    provider_kind = ProviderKind.LOCAL

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:11434",
        *,
        timeout_s: float = 120.0,
        max_retries: int = 3,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self._transport = transport

    def _client(self, timeout: float | None = None) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout if timeout is not None else self.timeout_s,
            transport=self._transport,
        )

    async def health(self) -> AdapterHealth:
        started = time.perf_counter()
        try:
            async with self._client(timeout=5.0) as client:
                resp = await client.get("/api/tags")
                resp.raise_for_status()
        except (httpx.HTTPError, OSError) as exc:
            return AdapterHealth(
                ok=False,
                detail=f"Ollama unreachable at {self.base_url}: {type(exc).__name__}",
                latency_ms=(time.perf_counter() - started) * 1000,
            )
        return AdapterHealth(
            ok=True, detail="ollama online", latency_ms=(time.perf_counter() - started) * 1000
        )

    async def list_models(self) -> list[ModelInfo]:
        async with self._client(timeout=10.0) as client:
            resp = await client.get("/api/tags")
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()
        out: list[ModelInfo] = []
        for entry in data.get("models", []):
            name = entry.get("name") or entry.get("model")
            if not name:
                continue
            out.append(
                ModelInfo(
                    provider=self.provider,
                    name=str(name),
                    provider_kind=ProviderKind.LOCAL,
                    capabilities=_infer_capabilities(str(name)),
                    size_bytes=entry.get("size"),
                )
            )
        return out

    async def capabilities(self, model: str) -> ModelCapabilities:
        return _infer_capabilities(model)

    def _payload(self, req: GenerationRequest, *, stream: bool) -> dict[str, Any]:
        options: dict[str, Any] = {"temperature": req.temperature}
        if req.top_p is not None:
            options["top_p"] = req.top_p
        if req.max_tokens is not None:
            options["num_predict"] = req.max_tokens
        if req.stop:
            options["stop"] = req.stop
        if req.num_ctx is not None:
            options["num_ctx"] = req.num_ctx
        payload: dict[str, Any] = {
            "model": req.model,
            "messages": [m.model_dump() for m in req.messages],
            "stream": stream,
            "options": options,
        }
        if req.system:
            payload["messages"] = [{"role": "system", "content": req.system}, *payload["messages"]]
        if req.response_format == "json":
            payload["format"] = "json"
        return payload

    async def generate(self, req: GenerationRequest, cancel: CancelToken) -> GenerationResult:
        started = time.perf_counter()
        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            if cancel.cancelled:
                raise asyncio.CancelledError
            try:
                async with self._client(timeout=req.timeout_s) as client:
                    resp = await client.post("/api/chat", json=self._payload(req, stream=False))
                    resp.raise_for_status()
                    data: dict[str, Any] = resp.json()
                return GenerationResult(
                    text=str(data.get("message", {}).get("content", "")),
                    model=req.model,
                    provider=self.provider,
                    prompt_tokens=data.get("prompt_eval_count"),
                    completion_tokens=data.get("eval_count"),
                    latency_ms=(time.perf_counter() - started) * 1000,
                    finish_reason=str(data.get("done_reason", "stop")),
                )
            except (httpx.ConnectError, httpx.TimeoutException) as exc:
                # Retry connection-level failures only, with backoff.
                last_error = exc
                await asyncio.sleep(min(2**attempt, 8))
            except httpx.HTTPError as exc:
                last_error = exc
                break
        msg = f"Ollama generation failed for {req.model}"
        raise RuntimeError(msg) from last_error

    async def stream(
        self, req: GenerationRequest, cancel: CancelToken
    ) -> AsyncIterator[StreamChunk]:
        try:
            async with (
                self._client(timeout=req.timeout_s) as client,
                client.stream("POST", "/api/chat", json=self._payload(req, stream=True)) as resp,
            ):
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if cancel.cancelled:
                        yield StreamChunk(error="cancelled", done=True)
                        return
                    if not line.strip():
                        continue
                    try:
                        data: dict[str, Any] = json.loads(line)
                    except json.JSONDecodeError:
                        logger.warning("skipping non-json ollama line")
                        continue
                    if data.get("error"):
                        yield StreamChunk(error=str(data["error"]), done=True)
                        return
                    yield StreamChunk(
                        text=str(data.get("message", {}).get("content", "")),
                        done=bool(data.get("done", False)),
                    )
        except (httpx.HTTPError, OSError) as exc:
            yield StreamChunk(error=f"Ollama stream failed: {type(exc).__name__}", done=True)
