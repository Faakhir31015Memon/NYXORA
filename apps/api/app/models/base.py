"""Typed contracts for the model intelligence layer."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from enum import StrEnum
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field


class Capability(StrEnum):
    CHAT = "chat"
    REASONING = "reasoning"
    CODING = "coding"
    SUMMARIZATION = "summarization"
    EMBEDDING = "embedding"
    VISION = "vision"
    TOOL_USE = "tool_use"
    LONG_CONTEXT = "long_context"
    STRUCTURED_OUTPUT = "structured_output"


class TaskType(StrEnum):
    CHAT = "chat"
    RESEARCH = "research"
    CODING = "coding"
    DOCUMENT = "document"
    FILE_TASK = "file_task"
    PROJECT_SCAFFOLD = "project_scaffold"
    MEMORY_REQUEST = "memory_request"
    UNKNOWN = "unknown"


class Complexity(StrEnum):
    SIMPLE = "simple"
    MODERATE = "moderate"
    COMPLEX = "complex"


class RiskLevel(StrEnum):
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ProviderKind(StrEnum):
    LOCAL = "local"
    EXTERNAL = "external"
    NYXORA = "nyxora"
    MOCK = "mock"


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str


class ModelCapabilities(BaseModel):
    tags: set[Capability] = Field(default_factory=set)
    context_window: int = 4096
    supports_streaming: bool = True
    supports_json: bool = False


class ModelInfo(BaseModel):
    provider: str
    name: str
    capabilities: ModelCapabilities
    provider_kind: ProviderKind = ProviderKind.LOCAL
    ram_hint_gb: float | None = None
    license: str | None = None
    enabled: bool = True
    size_bytes: int | None = None


class AdapterHealth(BaseModel):
    ok: bool
    detail: str = ""
    latency_ms: float | None = None


class GenerationRequest(BaseModel):
    model: str
    messages: list[ChatMessage]
    system: str | None = None
    temperature: float = 0.7
    top_p: float | None = None
    max_tokens: int | None = None
    stop: list[str] | None = None
    tools: list[dict[str, Any]] | None = None
    response_format: Literal["text", "json"] = "text"
    timeout_s: float = 120.0
    num_ctx: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class StreamChunk(BaseModel):
    text: str = ""
    done: bool = False
    error: str | None = None


class GenerationResult(BaseModel):
    text: str
    model: str
    provider: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    latency_ms: float | None = None
    finish_reason: str = "stop"


class CancelToken:
    """Cooperative cancellation shared between the orchestrator and adapters."""

    def __init__(self) -> None:
        self._event = asyncio.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    async def wait(self) -> None:
        await self._event.wait()


@runtime_checkable
class ModelAdapter(Protocol):
    provider: str
    provider_kind: ProviderKind

    async def health(self) -> AdapterHealth: ...

    async def list_models(self) -> list[ModelInfo]: ...

    async def capabilities(self, model: str) -> ModelCapabilities: ...

    async def generate(self, req: GenerationRequest, cancel: CancelToken) -> GenerationResult: ...

    def stream(self, req: GenerationRequest, cancel: CancelToken) -> AsyncIterator[StreamChunk]: ...
