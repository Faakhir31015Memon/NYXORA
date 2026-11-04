"""In-process typed async event bus.

Engines communicate through events and interfaces only, never by importing each
other's internals. The bus is deliberately small and swappable: a later Redis
streams implementation can satisfy the same protocol without touching callers.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

logger = logging.getLogger("nyxora.events")

Handler = Callable[["BusEvent"], Awaitable[None]]


class EventType(StrEnum):
    RUN_STARTED = "run_started"
    STAGE_CHANGED = "stage_changed"
    TOKEN_GENERATED = "token_generated"
    SOURCE_RETRIEVED = "source_retrieved"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_DECIDED = "approval_decided"
    TOOL_EXECUTED = "tool_executed"
    RUN_VERIFIED = "run_verified"
    RUN_FAILED = "run_failed"
    RUN_CANCELLED = "run_cancelled"
    MEMORY_PROPOSED = "memory_proposed"
    ROUTING_DECIDED = "routing_decided"
    VOICE_CAPTURE_STARTED = "voice_capture_started"
    VOICE_CAPTURE_STOPPED = "voice_capture_stopped"
    TRANSCRIPT_READY = "transcript_ready"
    SPEECH_STARTED = "speech_started"
    SPEECH_STOPPED = "speech_stopped"
    REMINDER_DUE = "reminder_due"
    NOTIFICATION_CREATED = "notification_created"
    ASSISTANT_MODE_CHANGED = "assistant_mode_changed"
    EMERGENCY_STOP = "emergency_stop"


class BusEvent(Protocol):
    event_type: EventType
    run_id: str | None
    payload: dict[str, Any]
    ts: datetime


def now_utc() -> datetime:
    return datetime.now(UTC)


class EventBus:
    """Fan-out pub/sub. A raising handler cannot break the publisher."""

    def __init__(self) -> None:
        self._handlers: dict[EventType | None, list[Handler]] = defaultdict(list)
        self._queue: asyncio.Queue[BusEvent] = asyncio.Queue()

    def subscribe(self, event_type: EventType | None, handler: Handler) -> Callable[[], None]:
        """Subscribe to one type, or None for all events. Returns an unsubscribe."""
        self._handlers[event_type].append(handler)

        def unsubscribe() -> None:
            with contextlib.suppress(ValueError):
                self._handlers[event_type].remove(handler)

        return unsubscribe

    async def publish(self, event: BusEvent) -> None:
        await self._queue.put(event)
        targets = [*self._handlers.get(event.event_type, []), *self._handlers.get(None, [])]
        for handler in targets:
            try:
                await handler(event)
            except Exception:  # noqa: BLE001 - isolation is the point
                logger.exception(
                    "event_handler_failed",
                    extra={"event_type": event.event_type, "run_id": event.run_id},
                )

    @property
    def pending(self) -> int:
        return self._queue.qsize()


class AnyEvent:
    """Concrete BusEvent. Payloads carry metadata only, never raw audio or secrets."""

    __slots__ = ("event_type", "run_id", "payload", "ts")

    def __init__(
        self,
        event_type: EventType,
        payload: dict[str, Any] | None = None,
        run_id: str | None = None,
    ) -> None:
        self.event_type = event_type
        self.payload = payload or {}
        self.run_id = run_id
        self.ts = now_utc()


_bus_singleton: EventBus | None = None


def get_event_bus() -> EventBus:
    global _bus_singleton
    if _bus_singleton is None:
        _bus_singleton = EventBus()
    return _bus_singleton


def reset_event_bus() -> None:
    """Test seam."""
    global _bus_singleton
    _bus_singleton = None
