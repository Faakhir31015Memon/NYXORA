"""Deterministic adapter used by every test and by CI. No hardware needed."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

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

DEFAULT_MOCK_MODEL = "mock-nyxora"
MOCK_MODELS: dict[str, set[Capability]] = {
    "mock-nyxora": {Capability.CHAT, Capability.REASONING, Capability.STRUCTURED_OUTPUT},
    "mock-coder": {Capability.CHAT, Capability.CODING, Capability.TOOL_USE},
    "mock-embed": {Capability.EMBEDDING},
}


class MockAdapter:
    """Echoes a stable string derived from the last user message."""

    provider = "mock"
    provider_kind = ProviderKind.MOCK

    def __init__(
        self,
        *,
        available: bool = True,
        fail_models: set[str] | None = None,
        chunk_delay_s: float = 0.0,
    ) -> None:
        self.available = available
        self.fail_models = fail_models or set()
        self.chunk_delay_s = chunk_delay_s
        self.calls: list[GenerationRequest] = []

    async def health(self) -> AdapterHealth:
        return AdapterHealth(
            ok=self.available, detail="mock adapter online" if self.available else "mock offline"
        )

    async def list_models(self) -> list[ModelInfo]:
        if not self.available:
            return []
        return [
            ModelInfo(
                provider=self.provider,
                name=name,
                provider_kind=ProviderKind.MOCK,
                capabilities=ModelCapabilities(tags=tags, context_window=8192),
            )
            for name, tags in MOCK_MODELS.items()
        ]

    async def capabilities(self, model: str) -> ModelCapabilities:
        return ModelCapabilities(
            tags=MOCK_MODELS.get(model, {Capability.CHAT}), context_window=8192, supports_json=True
        )

    def _text(self, req: GenerationRequest) -> str:
        last = next((m.content for m in reversed(req.messages) if m.role == "user"), "")
        return f"[mock:{req.model}] {last.strip()[:200]}"

    async def generate(self, req: GenerationRequest, cancel: CancelToken) -> GenerationResult:
        self.calls.append(req)
        if not self.available or req.model in self.fail_models:
            msg = f"mock generation failed for {req.model}"
            raise RuntimeError(msg)
        if cancel.cancelled:
            msg = "cancelled"
            raise asyncio.CancelledError(msg)
        text = self._text(req)
        return GenerationResult(
            text=text,
            model=req.model,
            provider=self.provider,
            completion_tokens=len(text.split()),
        )

    async def stream(
        self, req: GenerationRequest, cancel: CancelToken
    ) -> AsyncIterator[StreamChunk]:
        self.calls.append(req)
        if not self.available or req.model in self.fail_models:
            yield StreamChunk(error=f"mock generation failed for {req.model}", done=True)
            return
        for word in self._text(req).split(" "):
            if cancel.cancelled:
                yield StreamChunk(error="cancelled", done=True)
                return
            yield StreamChunk(text=f"{word} ")
            if self.chunk_delay_s:
                await asyncio.sleep(self.chunk_delay_s)
        yield StreamChunk(done=True)
