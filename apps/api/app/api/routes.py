"""HTTP surface. Consistent error shape, token auth, SSE streaming."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, UploadFile
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session

from app.core.events import EventType
from app.core.memory import MemoryEngine, MemoryProposalError, explicit_memory_request
from app.core.orchestrator import RunRequest, RunState
from app.core.rag import KnowledgeEngine
from app.core.tools import TOOL_PERMISSIONS, ToolContext
from app.db.models import (
    Approval,
    Conversation,
    Memory,
    Message,
    PlanRow,
    PlanStepRow,
    Project,
    RunStage,
    Setting,
    TaskRun,
)
from app.deps import Container
from app.models.router import RoutingInput
from app.security.permissions import PermissionClass, PermissionDenied

logger = logging.getLogger("nyxora.api")
router = APIRouter()


class ApiError(BaseModel):
    code: str
    message: str
    details: Any = None


def error_payload(code: str, message: str, details: Any = None) -> dict[str, Any]:
    return {"error": ApiError(code=code, message=message, details=details).model_dump()}


def get_container(request: Request) -> Container:
    """Auth itself lives in the app middleware - routes never trust a bare request."""
    container: Container = request.app.state.container
    return container


# --- request models ---------------------------------------------------------
class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=60000)
    conversation_id: str | None = None
    project_id: str | None = None
    attachments: list[str] = Field(default_factory=list)
    provider: str | None = None
    model: str | None = None
    use_documents: bool = True
    use_memory: bool = True


class RoutePreviewRequest(BaseModel):
    prompt: str
    task_type: str = "chat"
    complexity: str = "simple"
    needs_tools: bool = False


class ApprovalDecision(BaseModel):
    approved: bool
    grant_for_run: bool = False


class MemoryCreate(BaseModel):
    content: str
    scope: str = "global"
    project_id: str | None = None


class ToolRequest(BaseModel):
    params: dict[str, Any] = Field(default_factory=dict)
    run_id: str | None = None


class ConversationCreate(BaseModel):
    title: str = "New chat"
    project_id: str | None = None


class ProjectCreate(BaseModel):
    name: str
    workspace_root: str | None = None


# --- health & models -------------------------------------------------------
@router.get("/health")
async def health(request: Request) -> dict[str, Any]:
    c = get_container(request)
    ollama = c.adapters.get("ollama")
    ollama_health = await ollama.health() if ollama else None
    db_ok = True
    try:
        with c.engine.connect() as conn:
            conn.execute(sa_text("SELECT 1"))
    except Exception:  # noqa: BLE001
        db_ok = False
    models = [f"{m.provider}:{m.name}" for m in c.registry.all()]
    return {
        "status": "ok" if db_ok else "degraded",
        "version": "0.1.0",
        "db": db_ok,
        "ollama": {
            "configured": ollama is not None,
            "reachable": bool(ollama_health.ok) if ollama_health else False,
            "detail": ollama_health.detail if ollama_health else "not configured",
            "latency_ms": round(ollama_health.latency_ms or 0, 1) if ollama_health else None,
        },
        "models_available": models,
        "privacy_level": c.settings.privacy_level,
        "external_models_in_use": bool(c.settings.external_models_allowed),
        "emergency_stopped": c.permissions.stopped,
        "setup_hint": None if models else "ollama pull qwen2.5:3b",
    }


@router.get("/models")
async def list_models(request: Request) -> dict[str, Any]:
    c = get_container(request)
    return {
        "models": [m.model_dump() for m in c.registry.all()],
        "providers": sorted(c.adapters),
        "ollama_default": c.settings.ollama_default_model,
    }


@router.post("/models/refresh")
async def refresh_models(request: Request) -> dict[str, Any]:
    c = get_container(request)
    found = await c.registry.refresh()
    override_file = Path("config/models.yaml")
    applied = c.registry.load_yaml_overrides(override_file) if override_file.exists() else 0
    return {
        "discovered": [f"{m.provider}:{m.name}" for m in found],
        "yaml_overrides_applied": applied,
        "missing_hint": None if found else "ollama pull qwen2.5:3b",
    }


@router.post("/route/preview")
async def route_preview(payload: RoutePreviewRequest, request: Request) -> dict[str, Any]:
    c = get_container(request)
    decision = c.router.route(
        RoutingInput(
            task_type=payload.task_type,
            complexity=payload.complexity,
            needs_tools=payload.needs_tools,
            prompt_chars=len(payload.prompt),
        )
    )
    return decision.model_dump()


# --- chat with SSE ---------------------------------------------------------
@router.post("/chat")
async def chat(payload: ChatRequest, request: Request) -> StreamingResponse:
    c = get_container(request)
    run_id = str(uuid.uuid4())
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    def sse(event: str, data: dict[str, Any]) -> dict[str, Any]:
        return {"event": event, "data": json.dumps(data, default=str)}

    async def forward(evt: Any) -> None:
        mapping = {
            EventType.STAGE_CHANGED: "status",
            EventType.TOKEN_GENERATED: "token",
            EventType.ROUTING_DECIDED: "routing",
            EventType.APPROVAL_REQUESTED: "approval_required",
            EventType.TOOL_EXECUTED: "tool_result",
            EventType.SOURCE_RETRIEVED: "source",
            EventType.RUN_FAILED: "error",
            EventType.MEMORY_PROPOSED: "memory_suggestion",
        }
        name = mapping.get(evt.event_type)
        if name is None:
            return
        if name == "token" and evt.payload.get("final"):
            return
        queue.put_nowait(sse(name, {"run_id": evt.run_id, **evt.payload}))

    unsubscribe = c.bus.subscribe(None, forward)

    # Persist the user turn before generating, so a crash mid-stream is not silent.
    conversation_id = payload.conversation_id or str(uuid.uuid4())
    with DbSession(c) as session:
        if session.get(Conversation, conversation_id) is None:
            session.add(
                Conversation(
                    id=conversation_id,
                    project_id=payload.project_id,
                    title=payload.message[:60] or "New chat",
                )
            )
        session.add(Message(conversation_id=conversation_id, role="user", content=payload.message))
        session.add(
            TaskRun(id=run_id, conversation_id=conversation_id, state=RunState.CREATED.value)
        )
        session.flush()
        history = [
            f"{m.role}: {m.content}"
            for m in session.scalars(
                select(Message)
                .where(Message.conversation_id == conversation_id)
                .order_by(Message.id)
            ).all()[:-1]
        ][-8:]
        memory_items = (
            [m.content for m in MemoryEngine(session).active(limit=6)] if payload.use_memory else []
        )
        document_chunks: list[str] = []
        sources: list[dict[str, Any]] = []
        if payload.use_documents:
            knowledge = KnowledgeEngine(session, Path(c.settings.db_path).parent / "documents")
            for chunk in knowledge.search(payload.message, limit=4):
                document_chunks.append(chunk.as_context())
                sources.append(
                    {
                        "doc_id": chunk.doc_id,
                        "filename": chunk.filename,
                        "page": chunk.page,
                        "chunk_id": chunk.chunk_id,
                        "score": round(chunk.score, 3),
                    }
                )

    if sources:
        queue.put_nowait(sse("source", {"sources": sources, "run_id": run_id}))

    async def pump() -> None:
        try:
            outcome = await c.orchestrator.run(
                RunRequest(
                    message=payload.message,
                    conversation_id=conversation_id,
                    project_id=payload.project_id,
                    run_id=run_id,
                    attachments=payload.attachments,
                    history=history,
                    memory_items=memory_items,
                    document_chunks=document_chunks,
                    user_preferred_model=payload.model,
                )
            )
            with DbSession(c) as session:
                row = session.get(TaskRun, run_id)
                if row is not None:
                    row.state = outcome.state.value
                    row.summary = outcome.honest_summary
                    row.error = outcome.error
                    session.add(
                        Message(
                            conversation_id=conversation_id,
                            role="assistant",
                            content=outcome.answer,
                            model=(outcome.routing.model if outcome.routing else None),
                            provider=(outcome.routing.provider if outcome.routing else None),
                            routing_decision_json=(
                                outcome.routing.model_dump() if outcome.routing else None
                            ),
                            sources_json=sources or None,
                        )
                    )
                for stage in outcome.stages:
                    session.add(
                        RunStage(
                            run_id=run_id,
                            state=stage.state.value,
                            started_at=stage.started_at,
                            finished_at=stage.finished_at,
                            duration_ms=stage.duration_ms,
                            detail_json=stage.detail,
                        )
                    )
                if outcome.plan is not None:
                    plan_id = str(uuid.uuid4())
                    session.add(
                        PlanRow(
                            id=plan_id,
                            run_id=run_id,
                            goal=outcome.plan.goal,
                            generated_by=outcome.plan.generated_by,
                        )
                    )
                    for step in outcome.plan.steps:
                        session.add(
                            PlanStepRow(
                                plan_id=plan_id,
                                step_key=step.id,
                                description=step.description,
                                tool=step.tool,
                                params_json=step.params,
                                required_permission=step.required_permission,
                            )
                        )
                if outcome.routing is not None and outcome.plan is not None:
                    queue.put_nowait(sse("plan", outcome.plan.model_dump()))
        except Exception as exc:  # noqa: BLE001
            logger.exception("chat_pump_failed")
            queue.put_nowait(sse("error", {"code": "run_failed", "message": str(exc)}))
        finally:
            # The client may have sent no conversation_id and let us create one, so
            # the id it needs for the reload has to come back on the wire.
            queue.put_nowait(sse("done", {"run_id": run_id, "conversation_id": conversation_id}))

    task = asyncio.create_task(pump())

    async def stream() -> AsyncIterator[str]:
        try:
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=240.0)
                except TimeoutError:
                    yield 'event: error\ndata: {"code":"timeout","message":"no events"}\n\n'
                    break
                yield f"event: {item['event']}\ndata: {item['data']}\n\n"
                if item["event"] == "done":
                    break
        finally:
            unsubscribe()
            if not task.done():
                task.cancel()

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "X-Run-Id": run_id},
    )


class DbSession:
    """Transactional scope: commit on success, rollback on error, always close."""

    def __init__(self, c: Container) -> None:
        self.engine = c.engine
        self.session: Session | None = None

    def __enter__(self) -> Session:
        self.session = Session(self.engine, expire_on_commit=False)
        return self.session

    def __exit__(self, *exc: object) -> None:
        assert self.session is not None
        if exc[0] is None:
            self.session.commit()
        else:
            self.session.rollback()
        self.session.close()


# --- conversations & projects ---------------------------------------------
@router.get("/conversations")
async def list_conversations(request: Request, limit: int = 50) -> dict[str, Any]:
    c = get_container(request)
    with DbSession(c) as s:
        rows = s.scalars(
            select(Conversation).order_by(Conversation.updated_at.desc()).limit(limit)
        ).all()
        return {"conversations": [_conv_dict(r) for r in rows]}


@router.post("/conversations", status_code=201)
async def create_conversation(payload: ConversationCreate, request: Request) -> dict[str, Any]:
    c = get_container(request)
    cid = str(uuid.uuid4())
    with DbSession(c) as s:
        s.add(Conversation(id=cid, title=payload.title, project_id=payload.project_id))
    return {"id": cid, "title": payload.title}


@router.get("/conversations/{conversation_id}")
async def get_conversation(conversation_id: str, request: Request) -> dict[str, Any]:
    c = get_container(request)
    with DbSession(c) as s:
        conv = s.get(Conversation, conversation_id)
        if conv is None:
            raise HTTPException(404, detail=error_payload("not_found", "conversation not found"))
        messages = s.scalars(
            select(Message).where(Message.conversation_id == conversation_id).order_by(Message.id)
        ).all()
        return {"conversation": _conv_dict(conv), "messages": [_msg_dict(m) for m in messages]}


@router.delete("/conversations/{conversation_id}", status_code=204, response_class=Response)
async def delete_conversation(conversation_id: str, request: Request) -> Response:
    c = get_container(request)
    with DbSession(c) as s:
        conv = s.get(Conversation, conversation_id)
        if conv is None:
            raise HTTPException(404, detail=error_payload("not_found", "conversation not found"))
        s.delete(conv)
    return Response(status_code=204)


@router.get("/projects")
async def list_projects(request: Request) -> dict[str, Any]:
    c = get_container(request)
    with DbSession(c) as s:
        rows = s.scalars(select(Project)).all()
        return {
            "projects": [
                {"id": p.id, "name": p.name, "workspace_root": p.workspace_root} for p in rows
            ]
        }


@router.post("/projects", status_code=201)
async def create_project(payload: ProjectCreate, request: Request) -> dict[str, Any]:
    c = get_container(request)
    pid = str(uuid.uuid4())
    with DbSession(c) as s:
        s.add(Project(id=pid, name=payload.name, workspace_root=payload.workspace_root))
    return {"id": pid, "name": payload.name}


# --- tools, approvals, undo, stop -----------------------------------------
@router.get("/tools")
async def list_tools(request: Request) -> dict[str, Any]:
    from app.core.tools import TOOL_PERMISSIONS

    return {
        "tools": [{"name": n, "permission_class": p.value} for n, p in TOOL_PERMISSIONS.items()]
    }


@router.post("/tools/{name}/request")
async def request_tool(name: str, payload: ToolRequest, request: Request) -> dict[str, Any]:
    c = get_container(request)
    permission = TOOL_PERMISSIONS.get(name)
    if permission is None:
        raise HTTPException(404, detail=error_payload("unknown_tool", f"no tool named {name}"))
    if permission is PermissionClass.READ:
        result = await _run_tool(c, name, payload.params, payload.run_id, None)
        return {"auto_allowed": True, "result": result.model_dump()}
    preview = c.tools.preview_write(name, payload.params)
    approval = c.permissions.request(
        name,
        payload.params,
        permission,
        preview=preview.summary,
        run_id=payload.run_id,
        risk_note=preview.risk_note,
        diff=preview.diff,
    )
    c.audit.write(
        actor="user",
        outcome="requested",
        tool=name,
        params=payload.params,
        permission_class=permission.value,
        approval_id=approval.id,
        run_id=payload.run_id,
    )
    return {
        "approval": approval.model_dump(mode="json"),
        "preview": preview.model_dump(),
        "expires_in_s": max(0, int((approval.expires_at - approval.created_at).total_seconds())),
    }


@router.get("/approvals")
async def list_approvals(request: Request) -> dict[str, Any]:
    c = get_container(request)
    return {"approvals": [a.model_dump(mode="json") for a in c.permissions.pending()]}


@router.post("/approvals/{approval_id}/decide")
async def decide_approval(
    approval_id: str, payload: ApprovalDecision, request: Request
) -> dict[str, Any]:
    c = get_container(request)
    approval = c.permissions.get(approval_id)
    if approval is None:
        raise HTTPException(404, detail=error_payload("not_found", "approval not found"))
    params = dict(approval.params)
    try:
        decided = c.permissions.decide(
            approval_id, payload.approved, grant_for_run=payload.grant_for_run
        )
    except PermissionDenied as exc:
        raise HTTPException(403, detail=error_payload("permission_denied", str(exc))) from exc
    result = None
    if payload.approved and decided.run_id is None:
        # Direct tool approval from the UI executes immediately under this approval.
        result = await _run_tool(c, approval.tool, params, approval.run_id, approval_id)
    with DbSession(c) as s:
        s.add(
            Approval(
                id=approval_id,
                run_id=approval.run_id,
                tool=approval.tool,
                permission_class=approval.permission_class.value,
                params_json=params,
                hash=approval.hash,
                preview=approval.preview,
                approved=payload.approved,
                created_at=approval.created_at,
                expires_at=approval.expires_at,
            )
        )
    return {
        "approval_id": approval_id,
        "approved": payload.approved,
        "result": result.model_dump() if result else None,
    }


@router.post("/actions/{action_id}/undo")
async def undo_action(action_id: str, request: Request) -> dict[str, Any]:
    c = get_container(request)
    try:
        detail = c.undo.undo(action_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, detail=error_payload("undo_failed", str(exc))) from exc
    c.audit.write(actor="user", outcome="undone", params={"action_id": action_id})
    return {"ok": True, "detail": detail}


@router.post("/stop")
async def emergency_stop(request: Request) -> dict[str, Any]:
    c = get_container(request)
    cancelled = c.permissions.emergency_stop()
    active_runs = 0
    for run_id in list(getattr(c.orchestrator, "_cancel", {})):
        c.orchestrator.cancel(run_id)
        active_runs += 1
    c.audit.write(actor="user", outcome="emergency_stop", params={"cancelled_approvals": cancelled})
    return {"ok": True, "cancelled_approvals": cancelled, "runs_cancelled": active_runs}


@router.post("/resume")
async def resume(request: Request) -> dict[str, Any]:
    c = get_container(request)
    c.permissions.resume()
    c.audit.write(actor="user", outcome="resumed")
    return {"ok": True}


async def _run_tool(
    c: Container, name: str, params: dict[str, Any], run_id: str | None, approval_id: str | None
) -> Any:
    from app.core.tools import ToolResult

    # No consume() here. The tool consumes as its last gate before touching the
    # filesystem - consuming twice made every approved write fail.
    ctx = ToolContext(run_id=run_id, actor="user", approval_id=approval_id)
    fn = getattr(c.tools, name.replace(".", "_"), None)
    if fn is None:
        return ToolResult(ok=False, error=f"tool {name} not implemented")
    return await fn(params, ctx)


# --- knowledge -------------------------------------------------------------
@router.post("/documents/upload", status_code=201)
async def upload_document(request: Request, file: UploadFile) -> dict[str, Any]:
    c = get_container(request)
    data = await file.read()
    limit = c.settings.max_upload_size_mb * 1_048_576
    if len(data) > limit:
        raise HTTPException(
            413,
            detail=error_payload("too_large", f"max {c.settings.max_upload_size_mb} MB"),
        )
    store_dir = Path(c.settings.db_path).parent / "documents"
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in _ALLOWED_SUFFIXES:
        raise HTTPException(
            415, detail=error_payload("unsupported_type", f"allowed: {sorted(_ALLOWED_SUFFIXES)}")
        )
    with DbSession(c) as s:
        engine = KnowledgeEngine(s, store_dir)
        if suffix == ".pdf":
            content_type = "application/pdf"
        elif suffix == ".md":
            content_type = "text/markdown"
        else:
            content_type = "text/plain"
        try:
            result = engine.ingest(
                filename=file.filename or "document",
                data=data,
                content_type=content_type,
            )
        except ValueError as exc:
            raise HTTPException(415, detail=error_payload("ingest_failed", str(exc))) from exc
        return result.model_dump()


_ALLOWED_SUFFIXES = {".pdf", ".txt", ".md"}


@router.get("/documents")
async def list_documents(request: Request) -> dict[str, Any]:
    c = get_container(request)
    with DbSession(c) as s:
        return {
            "documents": KnowledgeEngine(
                s, Path(c.settings.db_path).parent / "documents"
            ).list_documents()
        }


@router.delete("/documents/{doc_id}")
async def delete_document(doc_id: str, request: Request) -> dict[str, Any]:
    c = get_container(request)
    with DbSession(c) as s:
        ok = KnowledgeEngine(s, Path(c.settings.db_path).parent / "documents").delete_document(
            doc_id
        )
    if not ok:
        raise HTTPException(404, detail=error_payload("not_found", "document not found"))
    return {"ok": True}


@router.post("/documents/search")
async def search_documents(payload: dict[str, Any], request: Request) -> dict[str, Any]:
    c = get_container(request)
    query = str(payload.get("query", ""))[:2000]
    limit = int(payload.get("limit", 5))
    with DbSession(c) as s:
        engine = KnowledgeEngine(s, Path(c.settings.db_path).parent / "documents")
        chunks = engine.search(query, limit=min(limit, 20))
        guard_note = engine.answer_guard(query, chunks)
    return {
        "query": query,
        "results": [
            {
                "chunk_id": ch.chunk_id,
                "doc_id": ch.doc_id,
                "filename": ch.filename,
                "page": ch.page,
                "score": round(ch.score, 3),
                "excerpt": ch.content[:400],
            }
            for ch in chunks
        ],
        "confidence_note": guard_note,
    }


# --- memory ---------------------------------------------------------------
@router.get("/memory")
async def get_memory(request: Request, include_disabled: bool = True) -> dict[str, Any]:
    c = get_container(request)
    with DbSession(c) as s:
        items = MemoryEngine(s).list_all(include_disabled=include_disabled)
    return {"memories": [i.model_dump(mode="json") for i in items]}


@router.post("/memory", status_code=201)
async def propose_memory(payload: MemoryCreate, request: Request) -> dict[str, Any]:
    c = get_container(request)
    with DbSession(c) as s:
        try:
            item = MemoryEngine(s).propose(
                payload.content, scope=payload.scope, project_id=payload.project_id
            )
        except MemoryProposalError as exc:
            c.audit.write(
                actor="user",
                outcome="denied",
                tool="memory.propose",
                params={"reason": str(exc.reason)},
            )
            raise HTTPException(422, detail=error_payload("memory_blocked", str(exc))) from exc
    return {"memory": item.model_dump(mode="json"), "requires_confirmation": True}


@router.post("/memory/{memory_id}/confirm")
async def confirm_memory(memory_id: str, request: Request) -> dict[str, Any]:
    c = get_container(request)
    with DbSession(c) as s:
        try:
            item = MemoryEngine(s).confirm(memory_id)
        except MemoryProposalError as exc:
            raise HTTPException(404, detail=error_payload("not_found", str(exc))) from exc
    return {"memory": item.model_dump(mode="json")}


@router.patch("/memory/{memory_id}")
async def patch_memory(memory_id: str, payload: dict[str, Any], request: Request) -> dict[str, Any]:
    c = get_container(request)
    with DbSession(c) as s:
        engine = MemoryEngine(s)
        row = s.get(Memory, memory_id)
        if row is None:
            raise HTTPException(404, detail=error_payload("not_found", "memory not found"))
        if "content" in payload:
            try:
                engine.edit(memory_id, str(payload["content"]))
            except MemoryProposalError as exc:
                raise HTTPException(422, detail=error_payload("memory_blocked", str(exc))) from exc
        if "state" in payload:
            state = str(payload["state"])
            if state not in {"active", "disabled", "pending"}:
                raise HTTPException(422, detail=error_payload("bad_state", state))
            engine._set(memory_id, state)
        item = next((m for m in engine.list_all() if m.id == memory_id), None)
    return {"memory": item.model_dump(mode="json") if item else None}


@router.delete("/memory/{memory_id}")
async def delete_memory(memory_id: str, request: Request) -> dict[str, Any]:
    c = get_container(request)
    with DbSession(c) as s:
        MemoryEngine(s).delete(memory_id)
    return {"ok": True}


@router.get("/memory/export")
async def export_memory(request: Request) -> dict[str, Any]:
    c = get_container(request)
    with DbSession(c) as s:
        return {"memories": MemoryEngine(s).export_json()}


# --- runs, audit, settings, agents, voice ---------------------------------
@router.get("/runs")
async def list_runs(request: Request, limit: int = 30) -> dict[str, Any]:
    c = get_container(request)
    with DbSession(c) as s:
        rows = s.scalars(select(TaskRun).order_by(TaskRun.created_at.desc()).limit(limit)).all()
        return {
            "runs": [
                {
                    "id": r.id,
                    "state": r.state,
                    "summary": r.summary,
                    "error": r.error,
                    "created_at": r.created_at.isoformat(),
                }
                for r in rows
            ]
        }


@router.get("/runs/{run_id}")
async def get_run(run_id: str, request: Request) -> dict[str, Any]:
    c = get_container(request)
    with DbSession(c) as s:
        run = s.get(TaskRun, run_id)
        if run is None:
            raise HTTPException(404, detail=error_payload("not_found", "run not found"))
        stages = s.scalars(select(RunStage).where(RunStage.run_id == run_id)).all()
        outcome = c.orchestrator.outcome(run_id)
        return {
            "run": {"id": run.id, "state": run.state, "summary": run.summary, "error": run.error},
            "stages": [
                {
                    "state": st.state,
                    "started_at": st.started_at.isoformat(),
                    "detail": st.detail_json,
                }
                for st in stages
            ],
            "live": outcome.model_dump(mode="json") if outcome else None,
        }


@router.get("/audit-log")
async def audit_log(request: Request, limit: int = 200, tool: str | None = None) -> dict[str, Any]:
    c = get_container(request)
    entries = c.audit.read(limit=limit, tool=tool)
    return {
        "entries": [e.model_dump(mode="json") for e in entries],
        "append_only": True,
    }


@router.get("/audit-log/export")
async def audit_export(request: Request) -> dict[str, Any]:
    c = get_container(request)
    return {"json": c.audit.export_json()}


@router.get("/settings")
async def get_settings_route(request: Request) -> dict[str, Any]:
    c = get_container(request)
    with DbSession(c) as s:
        rows = s.scalars(select(Setting)).all()
        stored = {r.key: r.value_json for r in rows}
    return {
        "stored": stored,
        "workspace_root": str(c.guard.root),
        "privacy_level": c.settings.privacy_level,
        "max_steps_per_run": c.settings.max_steps_per_run,
        "approval_expiry_s": c.settings.approval_expiry_s,
        "data_stays_local": not c.settings.external_models_allowed,
    }


@router.get("/agents")
async def list_agents(request: Request) -> dict[str, Any]:
    c = get_container(request)
    return {"agents": c.agents.roster()}


@router.get("/assistant/presence")
async def presence(request: Request) -> dict[str, Any]:
    c = get_container(request)
    ollama = c.adapters.get("ollama")
    return {
        "state": "paused" if c.permissions.stopped else "running",
        "mode": c.voice.mode.value,
        "microphone": c.voice.status(),
        "pending_approvals": len(c.permissions.pending()),
        "privacy_badge": "on-device"
        if not c.settings.external_models_allowed
        else "external-model",
        "models_ready": bool(c.registry.all()),
        "ollama_reachable": (await ollama.health()).ok if ollama else False,
        "collected_metrics_only": True,
    }


@router.get("/voice/status")
async def voice_status(request: Request) -> dict[str, Any]:
    c = get_container(request)
    return c.voice.status()


@router.get("/system/health/details")
async def system_details(request: Request) -> dict[str, Any]:
    c = get_container(request)
    return {
        "api": "ok",
        "db": str(c.settings.db_path),
        "workspace_root": str(c.guard.root),
        "adapters": sorted(c.adapters),
        "undo_actions_available": len(c.undo.list_actions()),
        "stopped": c.permissions.stopped,
    }


def _conv_dict(row: Conversation) -> dict[str, Any]:
    return {
        "id": row.id,
        "title": row.title,
        "project_id": row.project_id,
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    }


def _msg_dict(row: Message) -> dict[str, Any]:
    return {
        "id": row.id,
        "role": row.role,
        "content": row.content,
        "model": row.model,
        "provider": row.provider,
        "routing": row.routing_decision_json,
        "sources": row.sources_json,
        "created_at": row.created_at.isoformat(),
    }
