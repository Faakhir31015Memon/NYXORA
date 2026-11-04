"""SQLAlchemy models for the Section 13 data model (SQLite + FTS5)."""

from __future__ import annotations

import contextlib
import re
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    create_engine,
    event,
    text,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    workspace_root: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Conversation(Base):
    __tablename__ = "conversations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"))
    title: Mapped[str] = mapped_column(String(200), default="New chat")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(String(120))
    provider: Mapped[str | None] = mapped_column(String(60))
    routing_decision_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    sources_json: Mapped[list[Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TaskRun(Base):
    __tablename__ = "task_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[str | None] = mapped_column(String(36))
    state: Mapped[str] = mapped_column(String(32), default="created")
    summary: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RunStage(Base):
    __tablename__ = "run_stages"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("task_runs.id", ondelete="CASCADE"), index=True)
    state: Mapped[str] = mapped_column(String(32))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[float | None] = mapped_column(Float)
    detail_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class Document(Base):
    __tablename__ = "documents"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    filename: Mapped[str] = mapped_column(String(300))
    content_type: Mapped[str | None] = mapped_column(String(120))
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DocumentChunk(Base):
    __tablename__ = "document_chunks"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    doc_id: Mapped[str] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    filename: Mapped[str] = mapped_column(String(300), default="")
    page: Mapped[int | None] = mapped_column(Integer)
    chunk_idx: Mapped[int] = mapped_column(Integer, default=0)
    content: Mapped[str] = mapped_column(Text)
    tokens: Mapped[int] = mapped_column(Integer, default=0)


class Memory(Base):
    __tablename__ = "memories"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    content: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    scope: Mapped[str] = mapped_column(String(16), default="global")
    project_id: Mapped[str | None] = mapped_column(String(36))
    source: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class ProjectNote(Base):
    __tablename__ = "project_notes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Approval(Base):
    __tablename__ = "approvals"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str | None] = mapped_column(String(36), index=True)
    tool: Mapped[str] = mapped_column(String(60))
    permission_class: Mapped[str] = mapped_column(String(16))
    params_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    hash: Mapped[str] = mapped_column(String(64))
    preview: Mapped[str | None] = mapped_column(Text)
    approved: Mapped[bool | None] = mapped_column(Boolean)
    consumed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditRow(Base):
    __tablename__ = "audit_log"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(60))
    run_id: Mapped[str | None] = mapped_column(String(36))
    tool: Mapped[str | None] = mapped_column(String(60))
    params_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    permission_class: Mapped[str | None] = mapped_column(String(16))
    approval_id: Mapped[str | None] = mapped_column(String(36))
    outcome: Mapped[str] = mapped_column(String(48))
    error: Mapped[str | None] = mapped_column(Text)


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value_json: Mapped[Any] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Reminder(Base):
    __tablename__ = "reminders"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    text: Mapped[str] = mapped_column(Text)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    state: Mapped[str] = mapped_column(String(16), default="scheduled")
    reason: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str | None] = mapped_column(Text)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ModelRegistryRow(Base):
    __tablename__ = "model_registry"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(60))
    name: Mapped[str] = mapped_column(String(120))
    capabilities_json: Mapped[list[Any]] = mapped_column(JSON, default=list)
    context_window: Mapped[int] = mapped_column(Integer, default=4096)
    ram_hint_gb: Mapped[float | None] = mapped_column(Float)
    license: Mapped[str | None] = mapped_column(String(120))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (Index("uq_provider_name", "provider", "name", unique=True),)


class PlanRow(Base):
    __tablename__ = "plans"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str | None] = mapped_column(String(36), index=True)
    goal: Mapped[str] = mapped_column(Text)
    generated_by: Mapped[str] = mapped_column(String(16), default="rules")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PlanStepRow(Base):
    __tablename__ = "plan_steps"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plan_id: Mapped[str] = mapped_column(ForeignKey("plans.id", ondelete="CASCADE"), index=True)
    step_key: Mapped[str] = mapped_column(String(36))
    description: Mapped[str] = mapped_column(Text)
    tool: Mapped[str | None] = mapped_column(String(60))
    params_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    required_permission: Mapped[str] = mapped_column(String(16), default="read")
    status: Mapped[str] = mapped_column(String(24), default="pending")


FTS_DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS document_chunks_fts USING fts5(
    content,
    content='document_chunks',
    content_rowid='id',
    tokenize='porter unicode61'
);
"""

FTS_INSERT = """
CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON document_chunks BEGIN
    INSERT INTO document_chunks_fts(rowid, content) VALUES (new.id, new.content);
END;
"""

FTS_DELETE = """
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON document_chunks BEGIN
    INSERT INTO document_chunks_fts(document_chunks_fts, rowid, content)
    VALUES ('delete', old.id, old.content);
END;
"""


def make_engine(db_path: Path | str, *, create_fts: bool = True) -> Engine:
    """SQLite with WAL, foreign keys, and FTS5. `:memory:` is supported for tests."""
    target = ":memory:" if str(db_path) == ":memory:" else str(Path(db_path))
    if target != ":memory:":
        Path(target).parent.mkdir(parents=True, exist_ok=True)
    url = "sqlite+pysqlite:///" + target
    engine = create_engine(url, future=True, connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _configure(dbapi_conn: Any, _record: Any) -> None:
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA journal_mode=WAL")
        cur.close()

    if create_fts:

        @event.listens_for(engine, "connect")
        def _fts(dbapi_conn: Any, _record: Any) -> None:
            cur = dbapi_conn.cursor()
            for ddl in (FTS_DDL, FTS_INSERT, FTS_DELETE):
                with contextlib.suppress(Exception):  # FTS5 may be unavailable in this build
                    cur.execute(ddl)
            dbapi_conn.commit()
            cur.close()

    return engine


def init_db(engine: Engine) -> None:
    Base.metadata.create_all(engine)


def session_scope(engine: Engine) -> Generator[Session, None, None]:
    factory = sessionmaker(engine, expire_on_commit=False)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _fts_match(query: str) -> str:
    """Build a safe FTS5 MATCH expression.

    Wrapping the whole query in quotes would make it a phrase search, so
    "TLS certificate renew" only matches that exact contiguous text and real
    documents never hit. Terms are joined with OR and bm25() does the ranking;
    stripping non-word characters first also removes any FTS operator syntax a
    caller might inject.
    """
    terms = [t for t in re.split(r"[^0-9A-Za-z_]+", query.lower()) if len(t) > 1]
    if not terms:
        return ""
    return " OR ".join(f'"{t}"' for t in dict.fromkeys(terms))


def search_chunks(engine: Engine, query: str, limit: int = 5) -> list[dict[str, Any]]:
    """BM25-ranked FTS5 retrieval. Returns chunks with document provenance."""
    match = _fts_match(query)
    if not match:
        return []
    sql = text(
        """
        SELECT c.id, c.doc_id, c.filename, c.page, c.chunk_idx, c.content,
               bm25(document_chunks_fts) AS score
        FROM document_chunks_fts f
        JOIN document_chunks c ON c.id = f.rowid
        WHERE document_chunks_fts MATCH :q
        ORDER BY score ASC
        LIMIT :limit
        """
    )
    try:
        with engine.connect() as conn:
            rows = conn.execute(sql, {"q": match, "limit": limit}).all()
    except Exception:  # noqa: BLE001
        return []
    return [
        {
            "chunk_id": r[0],
            "doc_id": r[1],
            "filename": r[2],
            "page": r[3],
            "chunk_idx": r[4],
            "content": r[5],
            "score": -float(r[6]),
        }
        for r in rows
    ]
