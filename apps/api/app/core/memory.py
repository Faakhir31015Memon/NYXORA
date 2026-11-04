"""Memory Engine: four layers, user-authorized writes only.

States: pending -> active -> disabled -> deleted. A proposal from the assistant
is always `pending`; it becomes `active` only after an explicit user confirm.
Sensitive content is blocked at proposal time, not at display time.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Memory

SENSITIVE_PATTERNS: tuple[str, ...] = (
    r"(?i)\b(password|passwd|secret|api[_-]?key|access[_-]?token|private[_-]?key)\b\s*[:=]",
    r"(?i)\bsk-[A-Za-z0-9_\-]{12,}",
    r"(?i)\bbearer\s+[A-Za-z0-9._\-]{12,}",
    r"\b\d{4}[- ]?\d{4}[- ]?\d{4}[- ]?\d{4}\b",  # card-like
    r"\b\d{13}\b",  # CNIC-like / SSN-like
    r"(?i)\b(ssn|social security number|cnic)\b\s*[:=]?\s*\d",
    r"(?i)\b(routing number|iban|swift)\b\s*[:=]",
    r"(?i)\b(i have (cancer|diabetes|hiv)|my diagnosis|my bank balance)\b",
)
_SENSITIVE_RE = [re.compile(p) for p in SENSITIVE_PATTERNS]

MemoryState = str


class MemoryProposalError(ValueError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class MemoryItem(BaseModel):
    id: str
    content: str
    state: MemoryState
    scope: str
    project_id: str | None = None
    source: str | None = None
    created_at: datetime


def assert_not_sensitive(content: str) -> None:
    for pattern in _SENSITIVE_RE:
        if pattern.search(content):
            raise MemoryProposalError(
                "This looks like a credential, ID number, or private financial/health fact. "
                "NYXORA will not store it. Keep it in a password manager instead."
            )


class MemoryEngine:
    def __init__(self, session: Session) -> None:
        self.session = session

    def propose(
        self,
        content: str,
        *,
        scope: str = "global",
        project_id: str | None = None,
        source: str | None = None,
    ) -> MemoryItem:
        cleaned = " ".join((content or "").split())
        if len(cleaned) < 3:
            raise MemoryProposalError("memory is too short to be useful")
        assert_not_sensitive(cleaned)
        row = Memory(
            id=str(uuid.uuid4()),
            content=cleaned[:500],
            state="pending",
            scope=scope if scope in {"global", "project"} else "global",
            project_id=project_id,
            source=source,
        )
        self.session.add(row)
        self.session.flush()
        return _to_item(row)

    def confirm(self, memory_id: str) -> MemoryItem:
        row = self.session.get(Memory, memory_id)
        if row is None:
            raise MemoryProposalError("unknown memory")
        if row.state not in {"pending", "disabled"}:
            return _to_item(row)
        row.state = "active"
        self.session.flush()
        return _to_item(row)

    def active(self, *, project_id: str | None = None, limit: int = 20) -> list[MemoryItem]:
        stmt = select(Memory).where(Memory.state == "active")
        rows = list(self.session.scalars(stmt).all())
        scoped = [
            r for r in rows if r.scope == "global" or (project_id and r.project_id == project_id)
        ]
        return [_to_item(r) for r in _rank(scoped, project_id)[:limit]]

    def list_all(self, *, include_disabled: bool = True) -> list[MemoryItem]:
        stmt = select(Memory)
        rows = list(self.session.scalars(stmt).all())
        if not include_disabled:
            rows = [r for r in rows if r.state in {"active", "pending"}]
        return [_to_item(r) for r in rows]

    def search(self, query: str, *, active_only: bool = True) -> list[MemoryItem]:
        terms = [t.lower() for t in query.split() if len(t) > 2]
        pool = self.active() if active_only else self.list_all()
        if not terms:
            return pool
        scored = []
        for item in pool:
            haystack = item.content.lower()
            score = sum(1 for t in terms if t in haystack)
            if score:
                scored.append((score, item))
        scored.sort(key=lambda p: (-p[0], p[1].created_at))
        return [i for _, i in scored]

    def disable(self, memory_id: str) -> None:
        self._set(memory_id, "disabled")

    def edit(self, memory_id: str, content: str) -> MemoryItem:
        assert_not_sensitive(content)
        row = self.session.get(Memory, memory_id)
        if row is None:
            raise MemoryProposalError("unknown memory")
        row.content = " ".join(content.split())[:500]
        self.session.flush()
        return _to_item(row)

    def delete(self, memory_id: str) -> None:
        row = self.session.get(Memory, memory_id)
        if row is not None:
            self.session.delete(row)
            self.session.flush()

    def forget_topic(self, keyword: str) -> int:
        """Session-scoped suppression plus hard delete of matching active memories."""
        terms = [t.lower() for t in keyword.split() if len(t) > 2]
        removed = 0
        for row in list(self.session.scalars(select(Memory)).all()):
            if any(t in row.content.lower() for t in terms):
                self.session.delete(row)
                removed += 1
        self.session.flush()
        return removed

    def delete_all(self) -> int:
        rows = list(self.session.scalars(select(Memory)).all())
        for row in rows:
            self.session.delete(row)
        self.session.flush()
        return len(rows)

    def export_json(self) -> list[dict[str, Any]]:
        return [
            {
                "id": m.id,
                "content": m.content,
                "state": m.state,
                "scope": m.scope,
                "source": m.source,
                "created_at": m.created_at.isoformat(),
            }
            for m in self.list_all()
        ]

    def _set(self, memory_id: str, state: str) -> None:
        row = self.session.get(Memory, memory_id)
        if row is not None:
            row.state = state
            self.session.flush()


def _rank(rows: list[Memory], project_id: str | None) -> list[Memory]:
    def key(r: Memory) -> tuple[int, float]:
        return (0 if (project_id and r.project_id == project_id) else 1, -r.created_at.timestamp())

    return sorted(rows, key=key)


def _to_item(row: Memory) -> MemoryItem:
    created = row.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return MemoryItem(
        id=row.id,
        content=row.content,
        state=row.state,
        scope=row.scope,
        project_id=row.project_id,
        source=row.source,
        created_at=created,
    )
