"""Voice interfaces + proactive assistance.

V1 ships the contracts and deterministic mocks. No always-on microphone, no
silent recording, no ambient capture, no cloud audio without per-session consent.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from app.core.events import AnyEvent, EventBus, EventType


class PermissionState(StrEnum):
    NOT_DETERMINED = "not_determined"
    GRANTED = "granted"
    DENIED = "denied"
    RESTRICTED = "restricted"


class AssistantMode(StrEnum):
    TEXT_ONLY = "text_only"
    PUSH_TO_TALK = "push_to_talk"
    VOICE_RESPONSE = "voice_response"
    QUIET_MODE = "quiet_mode"
    DO_NOT_DISTURB = "do_not_disturb"
    EMERGENCY_STOP = "emergency_stop"


class HealthStatus(BaseModel):
    ok: bool
    detail: str = ""


class STTOptions(BaseModel):
    language: str = "auto"
    local_only: bool = True


class TTSOptions(BaseModel):
    voice: str = "default"
    speed: float = 1.0
    local_only: bool = True


class Transcript(BaseModel):
    text: str
    confidence: float = 1.0
    language: str = "auto"
    provider: str = "mock"
    is_local: bool = True


class VoiceInfo(BaseModel):
    id: str
    name: str
    language: str = "en"


class AudioDevice(BaseModel):
    id: str
    name: str
    is_default: bool = False


class AudioChunk(BaseModel):
    data: bytes = b""
    done: bool = False


class AudioFrame(BaseModel):
    data: bytes
    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))


class SpeechToTextAdapter(Protocol):
    async def health(self) -> HealthStatus: ...

    async def transcribe(self, audio: bytes, options: STTOptions) -> Transcript: ...

    def supported_languages(self) -> list[str]: ...

    def is_local(self) -> bool: ...


class TextToSpeechAdapter(Protocol):
    async def health(self) -> HealthStatus: ...

    async def list_voices(self) -> list[VoiceInfo]: ...

    def synthesize(
        self, text: str, voice: str, options: TTSOptions
    ) -> AsyncIterator[AudioChunk]: ...

    def streaming_capable(self) -> bool: ...


class WakeWordAdapter(Protocol):
    """Interface only. Disabled in V1 and refuses to start if asked."""


class VoiceService:
    """Owns the microphone/speech lifecycle with permission gates that cannot be skipped."""

    def __init__(
        self,
        bus: EventBus,
        *,
        stt: SpeechToTextAdapter | None = None,
        tts: TextToSpeechAdapter | None = None,
        external_consent: bool = False,
    ) -> None:
        self.bus = bus
        self.stt = stt
        self.tts = tts
        self.external_consent = external_consent
        self.permission: PermissionState = PermissionState.NOT_DETERMINED
        self.mode: AssistantMode = AssistantMode.TEXT_ONLY
        self.capturing = False
        self.speaking = False
        self._capture_task: asyncio.Task[None] | None = None
        self._stored_audio: list[bytes] = []

    def set_permission(self, state: PermissionState) -> None:
        self.permission = state

    def set_mode(self, mode: AssistantMode, run_id: str | None = None) -> None:
        self.mode = mode
        asyncio.create_task(
            self.bus.publish(
                AnyEvent(EventType.ASSISTANT_MODE_CHANGED, {"mode": mode.value}, run_id)
            )
        )

    async def start_capture(self, run_id: str | None = None) -> None:
        if self.permission is not PermissionState.GRANTED:
            raise PermissionError(
                f"microphone permission is {self.permission.value}; capture refused"
            )
        if self.mode not in {AssistantMode.PUSH_TO_TALK, AssistantMode.VOICE_RESPONSE}:
            raise PermissionError("voice mode is off; nothing is being recorded")
        self.capturing = True
        self._stored_audio.clear()
        await self.bus.publish(
            AnyEvent(EventType.VOICE_CAPTURE_STARTED, {"user_initiated": True}, run_id)
        )

    async def stop_capture(self, run_id: str | None = None) -> None:
        self.capturing = False
        await self.bus.publish(
            AnyEvent(EventType.VOICE_CAPTURE_STOPPED, {"frames": len(self._stored_audio)}, run_id)
        )

    def cancel(self) -> None:
        """Discard captured audio entirely - nothing is transcribed or stored."""
        self.capturing = False
        self._stored_audio.clear()

    async def transcribe(self, audio: bytes, *, run_id: str | None = None) -> Transcript:
        if self.permission is not PermissionState.GRANTED:
            raise PermissionError("microphone permission not granted")
        if self.stt is None:
            raise RuntimeError("no STT provider configured")
        if not self.stt.is_local() and not self.external_consent:
            raise PermissionError(
                "STT provider is external; audio would leave this device and no consent was given"
            )
        transcript = await self.stt.transcribe(audio, STTOptions())
        await self.bus.publish(
            AnyEvent(EventType.TRANSCRIPT_READY, transcript.model_dump(), run_id)
        )
        return transcript

    async def speak(self, text: str, *, run_id: str | None = None) -> None:
        if self.mode in {
            AssistantMode.QUIET_MODE,
            AssistantMode.DO_NOT_DISTURB,
            AssistantMode.TEXT_ONLY,
        }:
            return
        if self.tts is None:
            return
        self.speaking = True
        await self.bus.publish(AnyEvent(EventType.SPEECH_STARTED, {"chars": len(text)}, run_id))
        async for _ in self.tts.synthesize(text, "default", TTSOptions()):
            if not self.speaking:
                break
        self.speaking = False
        await self.bus.publish(AnyEvent(EventType.SPEECH_STOPPED, {}, run_id))

    def interrupt(self) -> None:
        self.speaking = False

    def status(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "permission": self.permission.value,
            "capturing": self.capturing,
            "speaking": self.speaking,
            "stt_configured": self.stt is not None,
            "tts_configured": self.tts is not None,
            "wake_word": "disabled_in_v1",
            "raw_audio_retained": False,
            "retained_frames": len(self._stored_audio),
        }


class MockSTT:
    """Deterministic STT for tests. Returns a canned transcript."""

    def __init__(self, text: str = "create a python dashboard project", local: bool = True) -> None:
        self.text = text
        self._local = local

    async def health(self) -> HealthStatus:
        return HealthStatus(ok=True, detail="mock stt")

    async def transcribe(self, audio: bytes, options: STTOptions) -> Transcript:
        if options.local_only and not self._local:
            raise PermissionError("local_only requested but provider is external")
        return Transcript(text=self.text, confidence=0.93, provider="mock", is_local=self._local)

    def supported_languages(self) -> list[str]:
        return ["en"]

    def is_local(self) -> bool:
        return self._local


class MockTTS:
    def __init__(self, chunks: int = 3) -> None:
        self.chunks = chunks
        self.spoken: list[str] = []

    async def health(self) -> HealthStatus:
        return HealthStatus(ok=True, detail="mock tts")

    async def list_voices(self) -> list[VoiceInfo]:
        return [VoiceInfo(id="default", name="Nyx")]

    async def synthesize(
        self, text: str, voice: str, options: TTSOptions
    ) -> AsyncIterator[AudioChunk]:
        self.spoken.append(text)
        for i in range(self.chunks):
            yield AudioChunk(data=b"\x00\x00", done=i == self.chunks - 1)

    def streaming_capable(self) -> bool:
        return True


class Suggestion(BaseModel):
    id: str
    text: str
    reason: str
    source: str
    created_at: datetime
    state: Literal["pending", "dismissed", "snoozed", "done"] = "pending"
    run_id: str | None = None


class ProactiveEngine:
    """Only emits cards the app actually observed, each with source + reason."""

    def __init__(self, bus: EventBus, *, quiet_hours: tuple[int, int] | None = None) -> None:
        self.bus = bus
        self.suggestions: dict[str, Suggestion] = {}
        self.quiet_hours = quiet_hours
        self._seen: set[str] = set()

    def _in_quiet_hours(self, when: datetime) -> bool:
        if not self.quiet_hours:
            return False
        start, end = self.quiet_hours
        hour = when.hour
        return hour >= start or hour < end if start > end else start <= hour < end

    def notify_run_event(
        self, run_id: str, kind: Literal["completed", "failed", "needs_approval"], detail: str
    ) -> Suggestion | None:
        key = f"{run_id}:{kind}"
        if key in self._seen:  # no repeated notification loops
            return None
        self._seen.add(key)
        return self._emit(
            text=f"Task {kind}: {detail}",
            reason=f"observed run {run_id} transition to {kind}",
            source=f"run:{run_id}",
            run_id=run_id,
        )

    def notify_reminder(self, reminder_text: str, source: str) -> Suggestion | None:
        return self._emit(
            text=reminder_text,
            reason="you created this reminder",
            source=source,
        )

    def _emit(
        self, *, text: str, reason: str, source: str, run_id: str | None = None
    ) -> Suggestion | None:
        now = datetime.now(UTC)
        if self._in_quiet_hours(now):
            return None
        suggestion = Suggestion(
            id=str(uuid.uuid4()),
            text=text,
            reason=reason,
            source=source,
            created_at=now,
            run_id=run_id,
        )
        self.suggestions[suggestion.id] = suggestion
        asyncio.create_task(
            self.bus.publish(
                AnyEvent(
                    EventType.NOTIFICATION_CREATED,
                    {"id": suggestion.id, "text": text, "reason": reason, "source": source},
                    run_id,
                )
            )
        )
        return suggestion

    def dismiss(self, suggestion_id: str) -> None:
        if suggestion_id in self.suggestions:
            self.suggestions[suggestion_id].state = "dismissed"

    def snooze(self, suggestion_id: str) -> None:
        if suggestion_id in self.suggestions:
            self.suggestions[suggestion_id].state = "snoozed"

    def pending(self) -> list[Suggestion]:
        return [s for s in self.suggestions.values() if s.state == "pending"]
