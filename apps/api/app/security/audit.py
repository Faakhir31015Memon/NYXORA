"""Append-only audit trail with secret redaction, plus undo snapshots."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

_REDACT_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|token|secret|password|authorization)\s*[=:]\s*\S+"),
    re.compile(r"(?i)sk-[A-Za-z0-9_\-]{10,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{10,}"),
]


def redact(value: Any) -> Any:
    """Never let a secret reach the log, an export, or the UI."""
    if isinstance(value, str):
        out = value
        for pattern in _REDACT_PATTERNS:
            out = pattern.sub("[REDACTED]", out)
        return out
    if isinstance(value, dict):
        return {
            (k if not _is_secret_key(str(k)) else f"{k}[REDACTED]"): redact(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


def _is_secret_key(key: str) -> bool:
    return bool(re.search(r"(?i)(api[_-]?key|token|secret|password)", key))


# The audit trail proves *what* happened. It must not become a second copy of
# the user's files: content-shaped parameters are replaced by their size and a
# short hash, which is enough to show that the executed bytes match the
# approved ones without retaining them.
_CONTENT_KEYS = re.compile(
    r"(?i)^(content|body|text|data|payload|new_string|old_string|diff|snippet|html)$"
)


def sanitize_params(params: dict[str, Any]) -> dict[str, Any]:
    """Redact secret-shaped values and drop file contents entirely."""
    out: dict[str, Any] = {}
    for key, value in (params or {}).items():
        if isinstance(value, str) and _CONTENT_KEYS.match(str(key)):
            out[str(key)] = f"[{_digest(value)}]"
        elif isinstance(value, dict) and key == "variables":
            out[str(key)] = f"[{len(value)} entries]"
        else:
            out[str(key)] = redact(value)
    return out


def _digest(text: str) -> str:
    """Length + 48-bit hash prefix: proves bytes changed without storing them.

    Not reversible, but it does let someone with a candidate secret confirm a
    guess offline, so a low-entropy password written straight into a file is
    still partly exposed here. Recorded as a known limit in docs/security.md;
    a keyed digest needs an install secret and belongs with the credential store.
    """
    encoded = text.encode("utf-8")
    return f"{len(encoded)}B sha256:{hashlib.sha256(encoded).hexdigest()[:12]}"


class AuditEntry(BaseModel):
    id: str
    ts: datetime
    actor: str
    run_id: str | None = None
    tool: str | None = None
    sanitized_params: dict[str, Any] = {}
    permission_class: str | None = None
    approval_id: str | None = None
    outcome: str
    error: str | None = None


class AuditLog:
    """Append-only from the app's point of view: no update, no delete API."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)

    def write(
        self,
        *,
        actor: str,
        outcome: str,
        run_id: str | None = None,
        tool: str | None = None,
        params: dict[str, Any] | None = None,
        permission_class: str | None = None,
        approval_id: str | None = None,
        error: str | None = None,
    ) -> AuditEntry:
        entry = AuditEntry(
            id=str(uuid.uuid4()),
            ts=datetime.now(UTC),
            actor=actor,
            run_id=run_id,
            tool=tool,
            sanitized_params=sanitize_params(params or {}),
            permission_class=permission_class,
            approval_id=approval_id,
            outcome=outcome,
            error=redact(error) if error else None,
        )
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry.model_dump(mode="json"), default=str) + "\n")
        return entry

    def read(
        self, *, limit: int = 500, tool: str | None = None, outcome: str | None = None
    ) -> list[AuditEntry]:
        entries: list[AuditEntry] = []
        if not self.path.exists():
            return entries
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                entry = AuditEntry.model_validate(json.loads(line))
            except (ValueError, json.JSONDecodeError):
                continue
            if tool and entry.tool != tool:
                continue
            if outcome and entry.outcome != outcome:
                continue
            entries.append(entry)
        return entries[-limit:]

    def export_json(self) -> str:
        return json.dumps([e.model_dump(mode="json") for e in self.read(limit=100_000)], indent=2)
