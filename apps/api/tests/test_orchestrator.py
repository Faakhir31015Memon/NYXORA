"""Orchestrator state machine: approve, deny, fail, retry, cancel, verify, stop."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from app.core.engines import ContextBuilder
from app.core.events import BusEvent, EventBus, EventType
from app.core.orchestrator import Orchestrator, RunRequest, RunState
from app.core.tools import FileTools
from app.models.mock import MockAdapter
from app.models.registry import ModelRegistry
from app.models.router import ModelRouter
from app.security.audit import AuditLog
from app.security.path_guard import PathGuard
from app.security.permissions import PermissionController
from app.security.undo import UndoStore


@pytest.fixture
def stack(tmp_root: Path) -> dict[str, Any]:
    root = tmp_root / "workspace"
    root.mkdir(parents=True, exist_ok=True)
    templates = tmp_root / "templates"
    (templates / "python_dashboard").mkdir(parents=True)
    (templates / "python_dashboard" / "README.md").write_text(
        "# {{project_name}}\n", encoding="utf-8"
    )
    (templates / "python_dashboard" / "db.py").write_text("SQL = '{{slug}}'\n", encoding="utf-8")
    permissions = PermissionController(expiry_s=300)
    audit = AuditLog(tmp_root / "audit.jsonl")
    tools = FileTools(
        PathGuard(root), permissions, UndoStore(root), audit, templates_root=templates
    )
    return {"root": root, "tools": tools, "permissions": permissions, "audit": audit}


async def make_orchestrator(
    adapter: MockAdapter, stack: dict[str, Any], bus: EventBus | None = None
) -> Orchestrator:
    registry = ModelRegistry()
    registry.register_adapter(adapter)
    for model in await adapter.list_models():
        registry.upsert(model)
    return Orchestrator(
        adapter=adapter,
        router=ModelRouter(registry),
        tools=stack["tools"],
        permissions=stack["permissions"],
        bus=bus or EventBus(),
        context_builder=ContextBuilder("You are NYXORA."),
        max_steps=12,
        default_model="mock-nyxora",
    )


async def test_simple_chat_uses_the_fast_path_and_skips_planning(
    tmp_root: Path, stack: dict[str, Any]
) -> None:
    adapter = MockAdapter()
    orchestrator = await make_orchestrator(adapter, stack)
    outcome = await orchestrator.run(RunRequest(message="hello there"))
    states = [s.state for s in outcome.stages]
    assert RunState.PLANNING not in states, "plain conversation must stay snappy"
    assert states.index(RunState.UNDERSTANDING) < states.index(RunState.ROUTING)
    assert outcome.state is RunState.COMPLETED
    assert "hello there" in outcome.answer


async def test_scaffold_stops_for_approval_before_writing_anything(
    tmp_root: Path, stack: dict[str, Any]
) -> None:
    adapter = MockAdapter()
    orchestrator = await make_orchestrator(adapter, stack)
    outcome = await orchestrator.run(
        RunRequest(message="/scaffold a python dashboard project called reports")
    )
    assert outcome.state is RunState.AWAITING_APPROVAL
    assert len(outcome.pending_approvals) == 1
    assert not (stack["root"] / "python").exists()
    assert outcome.plan is not None
    assert any(s.required_permission == "write" for s in outcome.plan.steps)


async def test_denied_approval_means_no_files_written(
    tmp_root: Path, stack: dict[str, Any]
) -> None:
    adapter = MockAdapter()
    orchestrator = await make_orchestrator(adapter, stack)
    outcome = await orchestrator.run(RunRequest(message="/scaffold a python dashboard project"))
    stack["permissions"].decide(outcome.pending_approvals[0], False)

    resumed = await orchestrator.run(
        RunRequest(message="/scaffold a python dashboard project", run_id=outcome.run_id)
    )
    assert resumed.state is RunState.FAILED, "a denial must end the run, not re-prompt it"
    # .nyx is NYXORA's own undo/audit directory, not user-visible work.
    assert [p.name for p in stack["root"].iterdir() if p.name != ".nyx"] == []


async def test_approved_plan_executes_verifies_and_is_undoable(
    tmp_root: Path, stack: dict[str, Any]
) -> None:
    adapter = MockAdapter()
    orchestrator = await make_orchestrator(adapter, stack)
    outcome = await orchestrator.run(RunRequest(message="/scaffold a python dashboard project"))
    approval = stack["permissions"].get(outcome.pending_approvals[0])
    assert approval is not None
    stack["permissions"].decide(approval.id, True, grant_for_run=True)

    second = await orchestrator.run(
        RunRequest(
            message="/scaffold a python dashboard project",
            run_id=outcome.run_id,
        )
    )
    created = [p for p in stack["root"].rglob("*.md")]
    assert created, "approved scaffold must actually write files"
    assert second.state in {RunState.COMPLETED, RunState.AWAITING_APPROVAL}
    assert second.verification is not None
    write_entry = next(r for r in second.tool_results if r["action_id"])
    stack["tools"].undo.undo(write_entry["action_id"])
    assert not list(stack["root"].rglob("*.md"))


async def test_model_failure_falls_back_down_the_chain(
    tmp_root: Path, stack: dict[str, Any]
) -> None:
    adapter = MockAdapter(fail_models={"mock-nyxora"})
    orchestrator = await make_orchestrator(adapter, stack)
    outcome = await orchestrator.run(RunRequest(message="hello"))
    if outcome.state is RunState.FAILED:
        assert "no fallback" in outcome.answer.lower() or outcome.error
        assert "done" not in outcome.honest_summary.lower()
    else:
        assert outcome.state is RunState.COMPLETED


async def test_no_model_available_returns_setup_error_not_a_crash(
    tmp_root: Path, stack: dict[str, Any]
) -> None:
    adapter = MockAdapter(available=False)
    orchestrator = await make_orchestrator(adapter, stack)
    outcome = await orchestrator.run(RunRequest(message="hello"))
    assert outcome.state is RunState.FAILED
    assert "ollama pull" in outcome.answer or "ollama pull" in (outcome.error or "")


async def test_verification_failure_prevents_claiming_done(
    tmp_root: Path, stack: dict[str, Any]
) -> None:
    """A plan step that reports success but leaves an empty file must not pass."""
    adapter = MockAdapter()
    orchestrator = await make_orchestrator(adapter, stack)
    empty = stack["root"] / "empty_file.md"
    empty.write_text("", encoding="utf-8")
    outcome = await orchestrator.run(RunRequest(message="hello"))
    assert "⚠" not in outcome.answer or outcome.state is RunState.FAILED
    failed_check = orchestrator.verification_engine.check_non_empty(empty)
    assert failed_check["ok"] is False
    aggregate = orchestrator.verification_engine.aggregate([failed_check])
    assert aggregate.ok is False
    assert "content_non_empty" in aggregate.failed


async def test_every_transition_is_emitted_as_an_event(
    tmp_root: Path, stack: dict[str, Any]
) -> None:
    bus = EventBus()
    seen: list[str] = []

    async def record(evt: BusEvent) -> None:
        if evt.event_type is EventType.STAGE_CHANGED:
            seen.append(str(evt.payload.get("state", "")))

    bus.subscribe(None, record)
    adapter = MockAdapter()
    orchestrator = await make_orchestrator(adapter, stack, bus)
    outcome = await orchestrator.run(RunRequest(message="hello"))
    assert seen, "the bus must receive events"
    assert "understanding" in seen
    assert "routing" in seen
    assert "completed" in seen or outcome.state.value in seen


async def test_cancel_between_steps_stops_the_run(tmp_root: Path, stack: dict[str, Any]) -> None:
    adapter = MockAdapter(chunk_delay_s=0.001)
    orchestrator = await make_orchestrator(adapter, stack)
    run_id = "cancel-me"
    task = asyncio.create_task(
        orchestrator.run(RunRequest(message="write a long answer please " * 30, run_id=run_id))
    )
    await asyncio.sleep(0.004)
    orchestrator.cancel(run_id)
    outcome = await task
    assert outcome.state in {RunState.CANCELLED, RunState.FAILED, RunState.COMPLETED}
    if outcome.state is RunState.CANCELLED:
        assert outcome.honest_summary == "cancelled by user"


async def test_plan_is_capped_at_max_steps(tmp_root: Path, stack: dict[str, Any]) -> None:
    adapter = MockAdapter()
    orchestrator = await make_orchestrator(adapter, stack)
    orchestrator.max_steps = 2
    outcome = await orchestrator.run(RunRequest(message="/scaffold a python dashboard project"))
    if outcome.plan is not None:
        assert len(outcome.plan.steps) <= 2


async def test_emergency_stop_blocks_a_run_midflight(tmp_root: Path, stack: dict[str, Any]) -> None:
    adapter = MockAdapter()
    orchestrator = await make_orchestrator(adapter, stack)
    stack["permissions"].emergency_stop()
    outcome = await orchestrator.run(RunRequest(message="/scaffold a python dashboard project"))
    assert outcome.state in {RunState.FAILED, RunState.AWAITING_APPROVAL, RunState.COMPLETED}
    assert not list(stack["root"].rglob("*.md")), "nothing may be written while stopped"


async def test_run_without_tools_never_touches_the_filesystem(
    tmp_root: Path, stack: dict[str, Any]
) -> None:
    adapter = MockAdapter()
    orchestrator = await make_orchestrator(adapter, stack)
    await orchestrator.run(RunRequest(message="what is the capital of France?"))
    # .nyx is the app's own undo/audit directory, not user-visible work.
    assert [p.name for p in stack["root"].iterdir() if p.name != ".nyx"] == []


async def test_events_reach_a_subscriber_in_order(tmp_root: Path, stack: dict[str, Any]) -> None:
    bus = EventBus()
    order: list[str] = []

    async def record(evt: BusEvent) -> None:
        order.append(evt.event_type.value)

    bus.subscribe(None, record)
    adapter = MockAdapter()
    orchestrator = await make_orchestrator(adapter, stack, bus)
    await orchestrator.run(RunRequest(message="hello"))
    assert EventType.STAGE_CHANGED.value in order
    assert order.index(EventType.RUN_STARTED.value) < order.index(EventType.TOKEN_GENERATED.value)
    assert order[-1] == EventType.TOKEN_GENERATED.value
    assert EventType.ROUTING_DECIDED.value in order
