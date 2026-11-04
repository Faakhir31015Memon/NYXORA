"""Permission Controller: single-use, param-hash-bound, expiring, audited."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.security.permissions import (
    DecisionOutcome,
    PermissionClass,
    PermissionController,
    PermissionDenied,
    params_hash,
)


def make_controller(*, expiry_s: int = 300) -> PermissionController:
    return PermissionController(expiry_s=expiry_s)


def request_write(ctrl: PermissionController) -> str:
    approval = ctrl.request(
        "fs.create",
        {"path": "a.txt", "content": "hi"},
        PermissionClass.WRITE,
        preview="create a.txt",
    )
    return approval.id


def test_params_hash_is_stable_and_sensitive() -> None:
    a = params_hash("fs.create", {"path": "x", "content": "1"})
    b = params_hash("fs.create", {"content": "1", "path": "x"})
    assert a == b, "key order must not matter"
    assert params_hash("fs.create", {"path": "x", "content": "2"}) != a
    assert params_hash("fs.edit", {"path": "x", "content": "1"}) != a


def test_read_is_auto_allowed_and_audited() -> None:
    ctrl = make_controller()
    approval_id = ctrl.authorize_read("fs.read", {"path": "a.txt"})
    assert approval_id.startswith("read-")
    assert ctrl.audit.entries[-1]["outcome"] == DecisionOutcome.AUTO_ALLOWED.value


def test_write_requires_approval_and_is_not_granted_by_default() -> None:
    ctrl = make_controller()
    approval_id = request_write(ctrl)
    assert len(ctrl.pending()) == 1
    with pytest.raises(PermissionDenied):
        ctrl.consume(approval_id, "fs.create", {"path": "a.txt", "content": "hi"})


def test_approval_is_single_use() -> None:
    ctrl = make_controller()
    approval_id = request_write(ctrl)
    ctrl.decide(approval_id, True)
    params = {"path": "a.txt", "content": "hi"}
    ctrl.consume(approval_id, "fs.create", params)
    with pytest.raises(PermissionDenied) as exc:
        ctrl.consume(approval_id, "fs.create", params)
    assert "single-use" in str(exc.value)


def test_param_mismatch_is_rejected_after_approval() -> None:
    """The attack: approve writing README.md, then write /etc/passwd."""
    ctrl = make_controller()
    approval_id = request_write(ctrl)
    ctrl.decide(approval_id, True)
    with pytest.raises(PermissionDenied) as exc:
        ctrl.consume(approval_id, "fs.create", {"path": "../../etc/passwd", "content": "evil"})
    assert exc.value.outcome is DecisionOutcome.REJECTED_MISMATCH
    assert any(e["outcome"] == DecisionOutcome.REJECTED_MISMATCH.value for e in ctrl.audit.entries)


def test_tool_name_change_is_rejected() -> None:
    ctrl = make_controller()
    approval_id = request_write(ctrl)
    ctrl.decide(approval_id, True)
    with pytest.raises(PermissionDenied):
        ctrl.consume(approval_id, "fs.delete", {"path": "a.txt", "content": "hi"})


def test_approval_expires() -> None:
    fake_now = [datetime(2026, 1, 1, tzinfo=UTC)]

    def clock() -> datetime:
        return fake_now[0]

    ctrl = PermissionController(expiry_s=60, clock=clock)
    approval_id = ctrl.request("fs.create", {"path": "a"}, PermissionClass.WRITE, preview="p").id
    ctrl.decide(approval_id, True)
    fake_now[0] = datetime(2026, 1, 1, 0, 2, tzinfo=UTC)  # 2 minutes later
    with pytest.raises(PermissionDenied) as exc:
        ctrl.consume(approval_id, "fs.create", {"path": "a"})
    assert exc.value.outcome is DecisionOutcome.EXPIRED


def test_expiry_before_decision() -> None:
    fake_now = [datetime(2026, 1, 1, tzinfo=UTC)]
    ctrl = PermissionController(expiry_s=10, clock=lambda: fake_now[0])
    approval_id = ctrl.request("fs.create", {"path": "a"}, PermissionClass.WRITE, preview="p").id
    fake_now[0] = datetime(2026, 1, 1, 0, 5, tzinfo=UTC)
    with pytest.raises(PermissionDenied) as exc:
        ctrl.decide(approval_id, True)
    assert exc.value.outcome is DecisionOutcome.EXPIRED


def test_forbidden_is_always_denied() -> None:
    ctrl = make_controller()
    with pytest.raises(PermissionDenied) as exc:
        ctrl.request("shell.exec", {"cmd": "rm -rf /"}, PermissionClass.FORBIDDEN, preview="p")
    assert exc.value.outcome is DecisionOutcome.FORBIDDEN


def test_double_decide_is_rejected() -> None:
    ctrl = make_controller()
    approval_id = request_write(ctrl)
    ctrl.decide(approval_id, True)
    with pytest.raises(PermissionDenied):
        ctrl.decide(approval_id, False)


def test_emergency_stop_blocks_new_requests_and_executions() -> None:
    ctrl = make_controller()
    approval_id = request_write(ctrl)
    ctrl.decide(approval_id, True)
    cancelled = ctrl.emergency_stop()
    assert cancelled >= 0
    with pytest.raises(PermissionDenied) as exc:
        ctrl.request("fs.create", {"path": "b"}, PermissionClass.WRITE, preview="p")
    assert exc.value.outcome is DecisionOutcome.BLOCKED_BY_STOP
    assert ctrl.stopped is True
    ctrl.resume()
    assert ctrl.stopped is False


def test_emergency_stop_denies_pending_approvals() -> None:
    ctrl = make_controller()
    approval_id = request_write(ctrl)
    ctrl.emergency_stop()
    assert ctrl.pending() == []
    with pytest.raises(PermissionDenied):
        ctrl.consume(approval_id, "fs.create", {"path": "a.txt", "content": "hi"})


def test_run_scoped_grant_avoids_repeat_prompts_but_never_forever() -> None:
    ctrl = make_controller()
    approval = ctrl.request(
        "fs.create", {"path": "a"}, PermissionClass.WRITE, preview="p", run_id="run-1"
    )
    ctrl.decide(approval.id, True, grant_for_run=True)
    assert ctrl.requires_approval(PermissionClass.WRITE, "run-1") is False
    assert ctrl.requires_approval(PermissionClass.WRITE, "run-2") is True
    ctrl.emergency_stop()
    ctrl.resume()
    # a run grant dies with the stop - it is not a permanent permission
    assert ctrl.requires_approval(PermissionClass.WRITE, "run-1") is True


def test_every_decision_is_audited() -> None:
    ctrl = make_controller()
    approval_id = request_write(ctrl)
    ctrl.decide(approval_id, True)
    outcomes = [e["outcome"] for e in ctrl.audit.entries]
    assert "requested" in outcomes
    assert DecisionOutcome.APPROVED.value in outcomes
    assert (
        ctrl.audit.entries[-1]["hash"]
        == params_hash("fs.create", {"path": "a.txt", "content": "hi"})[:16]
    )


def test_audit_never_contains_raw_content() -> None:
    ctrl = make_controller()
    ctrl.request(
        "fs.create", {"path": "a", "content": "SECRET-DATA"}, PermissionClass.WRITE, preview="p"
    )
    assert all("SECRET-DATA" not in str(e) for e in ctrl.audit.entries)
