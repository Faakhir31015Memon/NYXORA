"""UniversalOrchestrator: the state machine that composes every engine.

CREATED -> UNDERSTANDING -> PLANNING -> ROUTING -> AWAITING_APPROVAL
        -> EXECUTING -> OBSERVING -> VERIFYING -> REPORTING -> COMPLETED
                                                    | FAILED | CANCELLED

Invariants (each tested):
  * every transition emits an event and is recorded on the run
  * a WRITE/DELETE/EXTERNAL step cannot execute without a consumed approval
  * cancellation is checked between steps and propagates into model calls
  * total steps are capped so a confused model cannot loop forever
  * the final report never claims success when verification failed
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from app.core.engines import (
    ContextBuilder,
    Plan,
    PlanningEngine,
    PlanStep,
    TaskUnderstanding,
    UnderstandingEngine,
    VerificationEngine,
    VerificationResult,
)
from app.core.events import AnyEvent, EventBus, EventType
from app.core.safety import (
    SafetyReport,
    scan_for_harmful_request,
    scan_output_for_secrets,
    wrap_untrusted,
)
from app.core.tools import FileTools, ToolContext, ToolResult
from app.models.base import (
    CancelToken,
    ChatMessage,
    Complexity,
    GenerationRequest,
    ModelAdapter,
)
from app.models.router import ModelRouter, RoutingDecision, RoutingInput
from app.security.permissions import (
    ApprovalRequest,
    DecisionOutcome,
    PermissionClass,
    PermissionController,
    PermissionDenied,
)

logger = logging.getLogger("nyxora.orchestrator")


class RunState(StrEnum):
    CREATED = "created"
    UNDERSTANDING = "understanding"
    PLANNING = "planning"
    ROUTING = "routing"
    AWAITING_APPROVAL = "awaiting_approval"
    EXECUTING = "executing"
    OBSERVING = "observing"
    VERIFYING = "verifying"
    REPORTING = "reporting"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL = {RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED}


class RunRequest(BaseModel):
    message: str
    conversation_id: str | None = None
    project_id: str | None = None
    run_id: str | None = None
    attachments: list[str] = Field(default_factory=list)
    has_images: bool = False
    memory_items: list[str] = Field(default_factory=list)
    document_chunks: list[str] = Field(default_factory=list)
    history: list[str] = Field(default_factory=list)
    user_preferred_model: str | None = None
    fast_path: bool = True


class StageRecord(BaseModel):
    state: RunState
    started_at: datetime
    finished_at: datetime | None = None
    duration_ms: float | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class RunOutcome(BaseModel):
    run_id: str
    state: RunState
    answer: str = ""
    understanding: TaskUnderstanding | None = None
    plan: Plan | None = None
    routing: RoutingDecision | None = None
    stages: list[StageRecord] = Field(default_factory=list)
    tool_results: list[dict[str, Any]] = Field(default_factory=list)
    verification: VerificationResult | None = None
    safety: list[SafetyReport] = Field(default_factory=list)
    pending_approvals: list[str] = Field(default_factory=list)
    error: str | None = None
    honest_summary: str = ""


class ApprovalRequired(Exception):  # noqa: N818 - control-flow signal, not a failure
    def __init__(self, approval: ApprovalRequest) -> None:
        self.approval = approval
        super().__init__(approval.id)


class Orchestrator:
    def __init__(
        self,
        *,
        adapter: ModelAdapter,
        router: ModelRouter,
        tools: FileTools,
        permissions: PermissionController,
        bus: EventBus,
        context_builder: ContextBuilder,
        max_steps: int = 12,
        context_window: int = 8192,
        default_model: str = "mock-nyxora",
    ) -> None:
        self.adapter = adapter
        self.router = router
        self.tools = tools
        self.permissions = permissions
        self.bus = bus
        self.context_builder = context_builder
        self.max_steps = max_steps
        self.context_window = context_window
        self.default_model = default_model
        self.understanding_engine = UnderstandingEngine(adapter, default_model)
        self.planning_engine = PlanningEngine(adapter, default_model)
        self.verification_engine = VerificationEngine()
        self._cancel: dict[str, CancelToken] = {}
        self._outcomes: dict[str, RunOutcome] = {}

    # --- public control ----------------------------------------------------
    def cancel(self, run_id: str) -> None:
        token = self._cancel.get(run_id)
        if token:
            token.cancel()

    def outcome(self, run_id: str) -> RunOutcome | None:
        return self._outcomes.get(run_id)

    # --- the machine -------------------------------------------------------
    async def run(self, request: RunRequest) -> RunOutcome:
        run_id = request.run_id or str(uuid.uuid4())
        cancel = CancelToken()
        self._cancel[run_id] = cancel
        outcome = RunOutcome(run_id=run_id, state=RunState.CREATED)
        self._outcomes[run_id] = outcome

        async def stage(state: RunState, detail: dict[str, Any] | None = None) -> StageRecord:
            record = StageRecord(state=state, started_at=datetime.now(UTC), detail=detail or {})
            outcome.stages.append(record)
            await self.bus.publish(
                AnyEvent(EventType.STAGE_CHANGED, {"state": state.value, **record.detail}, run_id)
            )
            return record

        await stage(RunState.CREATED, {"message_chars": len(request.message)})
        await self.bus.publish(AnyEvent(EventType.RUN_STARTED, {"run_id": run_id}, run_id))

        try:
            # 1. SAFETY on the inbound message
            harmful = scan_for_harmful_request(request.message)
            outcome.safety.append(harmful)
            if harmful.verdict.value == "refuse":
                outcome.state = RunState.COMPLETED
                outcome.answer = (
                    "I won't help with that. This request falls outside NYXORA's safety policy."
                )
                outcome.honest_summary = "refused by safety policy"
                await self._finish(outcome, run_id)
                return outcome

            # 2. UNDERSTANDING
            await stage(RunState.UNDERSTANDING)
            understanding = self.understanding_engine.understand(
                request.message,
                has_attachments=bool(request.attachments),
                has_images=request.has_images,
            )
            outcome.understanding = understanding
            if understanding.should_clarify:
                outcome.state = RunState.COMPLETED
                outcome.answer = (
                    f"I want to get this right rather than guess. Please clarify: "
                    f"{'; '.join(understanding.ambiguities)}."
                )
                outcome.honest_summary = "asked a clarifying question"
                await self._finish(outcome, run_id)
                return outcome

            # 3. fast path: plain conversation skips planning and tools entirely
            use_fast_path = (
                request.fast_path
                and understanding.complexity is Complexity.SIMPLE
                and not understanding.needs_tools
            )
            plan: Plan | None = None
            if not use_fast_path:
                await stage(RunState.PLANNING)
                plan = await self.planning_engine.plan(understanding, request.message, cancel)
                outcome.plan = plan
                if len(plan.steps) > self.max_steps:
                    plan.steps = plan.steps[: self.max_steps]
                    outcome.safety.append(
                        SafetyReport(
                            verdict="clean",
                            action_taken="truncated_plan",
                            detail=f"plan capped at {self.max_steps} steps",
                        )
                    )

            # 4. ROUTING
            await stage(RunState.ROUTING)
            decision = self.router.route(
                RoutingInput(
                    task_type=understanding.task_type,
                    complexity=understanding.complexity.value,
                    needs_tools=bool(
                        understanding.needs_tools or (plan and any(s.tool for s in plan.steps))
                    ),
                    needs_vision=understanding.needs_vision,
                    needs_long_context=len(request.message) > 12000,
                    prompt_chars=len(request.message),
                    user_preferred_model=request.user_preferred_model,
                )
            )
            outcome.routing = decision
            await self.bus.publish(
                AnyEvent(EventType.ROUTING_DECIDED, decision.model_dump(), run_id)
            )
            if decision.model is None:
                outcome.state = RunState.FAILED
                outcome.error = decision.reason
                outcome.answer = (
                    f"No usable model is available. {decision.reason}. "
                    f"Setup: {decision.setup_hint}"
                )
                outcome.honest_summary = "failed: no model available"
                await self._finish(outcome, run_id)
                return outcome

            model = decision.model

            # 5. EXECUTING the plan's tool steps, each permission-gated
            tool_outputs: list[str] = []
            if plan and any(s.tool for s in plan.steps):
                await stage(RunState.EXECUTING, {"steps": len(plan.steps)})
                for step in plan.steps:
                    if cancel.cancelled:
                        raise asyncio.CancelledError
                    if not step.tool:
                        continue
                    try:
                        result = await self._execute_step(step, run_id, outcome)
                    except ApprovalRequired as exc:
                        outcome.state = RunState.AWAITING_APPROVAL
                        outcome.pending_approvals.append(exc.approval.id)
                        outcome.honest_summary = (
                            f"paused: approval {exc.approval.id} required before "
                            f"{step.tool} ({step.required_permission})"
                        )
                        await self.bus.publish(
                            AnyEvent(
                                EventType.APPROVAL_REQUESTED, exc.approval.model_dump(), run_id
                            )
                        )
                        await self._finish(outcome, run_id)
                        return outcome
                    if result is not None:
                        tool_outputs.append(
                            wrap_untrusted(_stringify(result.data), f"tool:{step.tool}")
                        )
                        await self.bus.publish(
                            AnyEvent(
                                EventType.TOOL_EXECUTED,
                                {"tool": step.tool, "ok": result.ok, "action_id": result.action_id},
                                run_id,
                            )
                        )
                await stage(RunState.OBSERVING, {"results": len(tool_outputs)})

            # 6. build context and generate
            built = self.context_builder.build(
                window=self.context_window,
                memory_items=[wrap_untrusted(m, "memory") for m in request.memory_items],
                document_chunks=[wrap_untrusted(c, "document") for c in request.document_chunks],
                history=request.history,
                tool_results=tool_outputs,
                user_message=request.message,
            )
            messages = [ChatMessage(role="user", content=built.prompt)]
            streamed: list[str] = []
            generation_failed = False
            try:
                generator = self.adapter.stream(
                    GenerationRequest(model=model, messages=messages, timeout_s=120.0), cancel
                )
                try:
                    async for chunk in generator:
                        if chunk.error:
                            generation_failed = True
                            outcome.error = chunk.error
                            break
                        if chunk.done:
                            break
                        streamed.append(chunk.text)
                        await self.bus.publish(
                            AnyEvent(EventType.TOKEN_GENERATED, {"text": chunk.text}, run_id)
                        )
                finally:
                    aclose = getattr(generator, "aclose", None)
                    if aclose is not None:
                        await aclose()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - runtime fallback walks the chain
                logger.warning(
                    "generation_failed_falling_back", extra={"error": type(exc).__name__}
                )
                generation_failed = True
                outcome.error = str(exc)

            if generation_failed:
                fallback_text = await self._try_fallbacks(decision, messages, cancel, run_id)
                if fallback_text is None:
                    outcome.state = RunState.FAILED
                    outcome.answer = (
                        f"Generation failed on {decision.provider}:{model} and no fallback model "
                        f"succeeded. Nothing was claimed as done. Error: {outcome.error}"
                    )
                    outcome.honest_summary = "failed: generation and fallbacks"
                    await self._finish(outcome, run_id)
                    return outcome
                answer = fallback_text
            else:
                answer = "".join(streamed)

            # 7. VERIFY
            await stage(RunState.VERIFYING)
            checks: list[dict[str, Any]] = []
            for entry in outcome.tool_results:
                if entry.get("tool") in {"fs.create", "fs.edit", "project.scaffold"}:
                    for path in entry.get("paths", []):
                        checks.append(self.verification_engine.check_non_empty(path))
            if request.document_chunks and request.attachments:
                checks.append(
                    self.verification_engine.check_cites_sources(answer, request.document_chunks)
                )
            verification = self.verification_engine.aggregate(checks)
            outcome.verification = verification

            secret_scan = scan_output_for_secrets(answer)
            outcome.safety.append(secret_scan)
            if secret_scan.verdict.value == "secret_leak":
                answer = "[redacted]"

            # 8. REPORT
            await stage(RunState.REPORTING)
            outcome.answer = answer
            if verification.ok:
                outcome.state = RunState.COMPLETED
                outcome.honest_summary = "completed and verified" if checks else "completed"
            else:
                outcome.state = RunState.FAILED
                outcome.answer = (
                    f"{answer}\n\n⚠ Verification failed: {', '.join(verification.failed)}. "
                    "I cannot call this done."
                )
                outcome.honest_summary = (
                    f"completed action but verification failed: {verification.failed}"
                )
            await self.bus.publish(
                AnyEvent(
                    EventType.RUN_VERIFIED if verification.ok else EventType.RUN_FAILED,
                    {"failed": verification.failed},
                    run_id,
                )
            )
            await self._finish(outcome, run_id)
            return outcome

        except asyncio.CancelledError:
            outcome.state = RunState.CANCELLED
            outcome.honest_summary = "cancelled by user"
            await self.bus.publish(AnyEvent(EventType.RUN_CANCELLED, {}, run_id))
            await self._finish(outcome, run_id)
            return outcome
        except PermissionDenied as exc:
            outcome.state = RunState.FAILED
            outcome.error = str(exc)
            outcome.answer = f"Blocked by the permission controller: {exc}"
            outcome.honest_summary = f"permission denied ({exc.outcome})"
            await self.bus.publish(AnyEvent(EventType.RUN_FAILED, {"reason": str(exc)}, run_id))
            await self._finish(outcome, run_id)
            return outcome

    async def _execute_step(
        self, step: PlanStep, run_id: str, outcome: RunOutcome
    ) -> ToolResult | None:
        permission = PermissionClass(step.required_permission)
        if permission is PermissionClass.FORBIDDEN:
            raise PermissionDenied(DecisionOutcome.FORBIDDEN, f"{step.tool} is forbidden in V1")

        if self.permissions.stopped:
            raise PermissionDenied(DecisionOutcome.BLOCKED_BY_STOP, "emergency stop active")

        approval_id: str | None = None
        if permission is PermissionClass.READ:
            approval_id = self.permissions.authorize_read(
                step.tool or "unknown", step.params, run_id
            )
        else:
            preview = self.tools.preview_write(step.tool or "unknown", step.params)
            tool = step.tool or "unknown"
            # find_approved, not pending(): a just-granted approval is no longer
            # pending, so querying pending() here would re-ask on every resume.
            existing = self.permissions.find_approved(tool, step.params, run_id)
            if existing is None:
                denied = self.permissions.find_denied(tool, step.params, run_id)
                if denied is not None:
                    raise PermissionDenied(
                        DecisionOutcome.DENIED,
                        f"you denied {tool} for this run (approval {denied.id})",
                    )
                request = self.permissions.request(
                    tool,
                    step.params,
                    permission,
                    preview=preview.summary,
                    run_id=run_id,
                    risk_note=preview.risk_note,
                    diff=preview.diff,
                )
                raise ApprovalRequired(request)
            approval_id = existing.id

        ctx = ToolContext(run_id=run_id, actor="agent:manager", approval_id=approval_id)
        fn = getattr(self.tools, (step.tool or "").replace(".", "_"), None)
        if fn is None:
            raise PermissionDenied(DecisionOutcome.FORBIDDEN, f"unknown tool {step.tool!r}")
        result: ToolResult = await fn(step.params, ctx)
        outcome.tool_results.append(
            {
                "tool": step.tool,
                "ok": result.ok,
                "action_id": result.action_id,
                "paths": _touched_paths(result),
            }
        )
        return result

    async def _try_fallbacks(
        self,
        decision: RoutingDecision,
        messages: list[ChatMessage],
        cancel: CancelToken,
        run_id: str,
    ) -> str | None:
        for entry in decision.fallback_chain[1:]:
            provider, _, name = entry.partition(":")
            if provider != self.adapter.provider:
                continue
            try:
                result = await self.adapter.generate(
                    GenerationRequest(model=name, messages=messages), cancel
                )
                await self.bus.publish(
                    AnyEvent(EventType.STAGE_CHANGED, {"fallback_used": name}, run_id)
                )
                return result.text
            except Exception:  # noqa: BLE001
                continue
        return None

    async def _finish(self, outcome: RunOutcome, run_id: str) -> None:
        """The terminal state is a transition too, so it is emitted and recorded."""
        self._cancel.pop(run_id, None)
        self._outcomes[run_id] = outcome
        outcome.stages.append(StageRecord(state=outcome.state, started_at=datetime.now(UTC)))
        await self.bus.publish(
            AnyEvent(
                EventType.STAGE_CHANGED,
                {"state": outcome.state.value, "summary": outcome.honest_summary},
                run_id,
            )
        )
        await self.bus.publish(AnyEvent(EventType.TOKEN_GENERATED, {"final": True}, run_id))


def _stringify(data: Any) -> str:
    if isinstance(data, str):
        return data
    try:
        return json.dumps(data, default=str)
    except (TypeError, ValueError):
        return str(data)


def _touched_paths(result: ToolResult) -> list[str]:
    from pathlib import Path

    paths: list[str] = []
    data = result.data or {}
    if isinstance(data, dict):
        for key in ("path", "files", "deleted"):
            value = data.get(key)
            if isinstance(value, str):
                paths.append(value)
            elif isinstance(value, list):
                paths.extend(str(v) for v in value)
    return [p for p in paths if Path(p).exists() or Path(p).is_dir()]
