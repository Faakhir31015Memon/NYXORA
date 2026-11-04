"""Permission Controller - the sole authority for whether an action may run.

Properties enforced here (each is separately tested):
  * single-use   - an approval is consumed the moment it is spent
  * bound        - the executed parameters must hash to the approved parameters
  * expiring     - approvals die after a configurable TTL
  * scoped       - "approve for this run" only; "forever" is not offered in V1
  * audited      - every request and decision is written to the audit log

Nothing else in the system may grant permission. Agents cannot grant permission
to other agents. Model output cannot grant permission.
"""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from pydantic import BaseModel


class PermissionClass(StrEnum):
    READ = "read"
    WRITE = "write"
    DELETE = "delete"
    EXTERNAL = "external"
    FORBIDDEN = "forbidden"


class DecisionOutcome(StrEnum):
    AUTO_ALLOWED = "auto_allowed"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"
    REJECTED_MISMATCH = "rejected_param_mismatch"
    FORBIDDEN = "forbidden"
    BLOCKED_BY_STOP = "blocked_by_emergency_stop"


def params_hash(tool: str, params: dict[str, Any]) -> str:
    """Canonical hash of (tool, params). Different params -> different hash."""
    canonical = json.dumps({"tool": tool, "params": params}, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ApprovalRequest(BaseModel):
    id: str
    run_id: str | None
    tool: str
    permission_class: PermissionClass
    params: dict[str, Any]
    hash: str
    preview: str
    risk_note: str | None = None
    diff: str | None = None
    created_at: datetime
    expires_at: datetime
    consumed: bool = False
    decided: bool = False
    approved: bool | None = None

    def is_expired(self, now: datetime) -> bool:
        """Expiry is always evaluated against the controller's clock, never wall time,
        so a test or a time-shifted host cannot silently change the policy."""
        return now >= self.expires_at


class PermissionDenied(PermissionError):  # noqa: N818 - name comes from the blueprint's §12 vocabulary
    def __init__(self, outcome: DecisionOutcome, detail: str) -> None:
        self.outcome = outcome
        super().__init__(detail)


@dataclass
class AuditSink:
    entries: list[dict[str, Any]] = field(default_factory=list)

    def record(self, entry: dict[str, Any]) -> None:
        self.entries.append(entry)


class PermissionController:
    def __init__(
        self,
        *,
        expiry_s: int = 300,
        read_policy: PermissionClass = PermissionClass.READ,
        audit: AuditSink | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),  # noqa: E731 - test seam
    ) -> None:
        self.expiry = timedelta(seconds=expiry_s)
        self.read_policy = read_policy
        self._approvals: dict[str, ApprovalRequest] = {}
        self._lock = threading.Lock()
        self._stopped = False
        self._run_grants: dict[str, set[str]] = {}
        self.audit = audit or AuditSink()
        self._clock = clock

    # --- emergency stop -----------------------------------------------------
    @property
    def stopped(self) -> bool:
        return self._stopped

    def emergency_stop(self) -> int:
        """Cancel pending approvals and block all new executions."""
        with self._lock:
            self._stopped = True
            cancelled = 0
            for approval in self._approvals.values():
                if not approval.decided:
                    approval.decided = True
                    approval.approved = False
                    cancelled += 1
                    self._record(approval, DecisionOutcome.DENIED, "emergency stop")
            self._run_grants.clear()
        return cancelled

    def resume(self) -> None:
        with self._lock:
            self._stopped = False

    # --- classification -----------------------------------------------------
    def classify(self, permission_class: PermissionClass) -> PermissionClass:
        return permission_class

    def requires_approval(
        self, permission_class: PermissionClass, run_id: str | None = None
    ) -> bool:
        if self._stopped:
            return False  # caller must block, not ask
        if permission_class is PermissionClass.FORBIDDEN:
            return False
        if permission_class is PermissionClass.READ:
            return self.read_policy is not PermissionClass.READ
        granted = self._run_grants.get(run_id or "", set())
        return permission_class.value not in granted

    # --- request / decide / consume ---------------------------------------
    def request(
        self,
        tool: str,
        params: dict[str, Any],
        permission_class: PermissionClass,
        *,
        preview: str,
        run_id: str | None = None,
        risk_note: str | None = None,
        diff: str | None = None,
    ) -> ApprovalRequest:
        if permission_class is PermissionClass.FORBIDDEN:
            raise PermissionDenied(DecisionOutcome.FORBIDDEN, f"{tool} is forbidden in V1")
        if self._stopped:
            raise PermissionDenied(
                DecisionOutcome.BLOCKED_BY_STOP, "emergency stop active: no new execution"
            )
        approval = ApprovalRequest(
            id=str(uuid.uuid4()),
            run_id=run_id,
            tool=tool,
            permission_class=permission_class,
            params=params,
            hash=params_hash(tool, params),
            preview=preview,
            risk_note=risk_note,
            diff=diff,
            created_at=self._clock(),
            expires_at=self._clock() + self.expiry,
        )
        with self._lock:
            self._approvals[approval.id] = approval
        self._record(approval, "requested", None)
        return approval

    def pending(self) -> list[ApprovalRequest]:
        with self._lock:
            return [
                a
                for a in self._approvals.values()
                if not a.decided and not a.is_expired(self._clock())
            ]

    def get(self, approval_id: str) -> ApprovalRequest | None:
        return self._approvals.get(approval_id)

    def _find_decided(
        self, tool: str, params: dict[str, Any], run_id: str | None, approved: bool
    ) -> ApprovalRequest | None:
        """The user already answered for exactly this action in this run."""
        wanted = params_hash(tool, params)
        now = self._clock()
        with self._lock:
            for approval in self._approvals.values():
                if approval.tool != tool or approval.hash != wanted:
                    continue
                if not approval.decided or approval.approved is not approved:
                    continue
                if run_id is not None and approval.run_id != run_id:
                    continue
                if approved and (approval.consumed or approval.is_expired(now)):
                    continue
                return approval
        return None

    def find_approved(
        self, tool: str, params: dict[str, Any], run_id: str | None
    ) -> ApprovalRequest | None:
        """Consumable, unexpired, matching grant. This is what makes approve-then-resume
        work; without it a resumed run would re-ask forever."""
        return self._find_decided(tool, params, run_id, True)

    def find_denied(
        self, tool: str, params: dict[str, Any], run_id: str | None
    ) -> ApprovalRequest | None:
        """A recorded denial for exactly this action. Resume must honour it rather than
        silently re-prompting the user for the thing they already refused."""
        return self._find_decided(tool, params, run_id, False)

    def decide(
        self,
        approval_id: str,
        approved: bool,
        *,
        grant_for_run: bool = False,
    ) -> ApprovalRequest:
        with self._lock:
            approval = self._approvals.get(approval_id)
            if approval is None:
                msg = f"unknown approval {approval_id}"
                raise KeyError(msg)
            if approval.decided:
                msg = "approval already decided (single-use)"
                raise PermissionDenied(DecisionOutcome.DENIED, msg)
            if approval.is_expired(self._clock()):
                approval.decided = True
                approval.approved = False
                self._record(approval, DecisionOutcome.EXPIRED, None)
                raise PermissionDenied(DecisionOutcome.EXPIRED, "approval expired")
            approval.decided = True
            approval.approved = approved
            if approved and grant_for_run and approval.run_id:
                self._run_grants.setdefault(approval.run_id, set()).add(
                    approval.permission_class.value
                )
            outcome = DecisionOutcome.APPROVED if approved else DecisionOutcome.DENIED
            self._record(approval, outcome, None)
            return approval

    def consume(self, approval_id: str, tool: str, params: dict[str, Any]) -> None:
        """Called immediately before execution. Raises if anything is off."""
        if self._stopped:
            raise PermissionDenied(
                DecisionOutcome.BLOCKED_BY_STOP, "emergency stop active: no new execution"
            )
        with self._lock:
            approval = self._approvals.get(approval_id)
            if approval is None:
                raise PermissionDenied(DecisionOutcome.DENIED, "unknown approval")
            if approval.consumed:
                raise PermissionDenied(
                    DecisionOutcome.DENIED, "approval already used: approvals are single-use"
                )
            if not approval.decided or approval.approved is not True:
                raise PermissionDenied(DecisionOutcome.DENIED, "approval not granted")
            if approval.is_expired(self._clock()):
                raise PermissionDenied(DecisionOutcome.EXPIRED, "approval expired before execution")
            if approval.tool != tool or approval.hash != params_hash(tool, params):
                approval.consumed = True
                self._record(approval, DecisionOutcome.REJECTED_MISMATCH, None)
                raise PermissionDenied(
                    DecisionOutcome.REJECTED_MISMATCH,
                    "executed parameters differ from approved parameters",
                )
            approval.consumed = True

    def authorize_read(self, tool: str, params: dict[str, Any], run_id: str | None = None) -> str:
        """READ in-root is auto-allowed under the default policy; still audited."""
        approval_id = f"read-{uuid.uuid4()}"
        stub = ApprovalRequest(
            id=approval_id,
            run_id=run_id,
            tool=tool,
            permission_class=PermissionClass.READ,
            params=params,
            hash=params_hash(tool, params),
            preview="read-only",
            created_at=self._clock(),
            expires_at=self._clock() + self.expiry,
            decided=True,
            approved=True,
            consumed=True,
        )
        self._record(stub, DecisionOutcome.AUTO_ALLOWED, None)
        return approval_id

    def _record(self, approval: ApprovalRequest, outcome: Any, error: str | None) -> None:
        self.audit.record(
            {
                "ts": self._clock().isoformat(),
                "approval_id": approval.id,
                "run_id": approval.run_id,
                "tool": approval.tool,
                "permission_class": approval.permission_class.value,
                "outcome": str(outcome),
                "hash": approval.hash[:16],
                "error": error,
            }
        )
