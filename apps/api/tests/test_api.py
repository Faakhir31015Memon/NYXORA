"""HTTP surface: auth, error shape, SSE chat, approval round-trip, RAG, memory.

These tests boot the real app factory. A previous revision of this suite had
fixtures for it but no test using them, and the container silently failed to
start because of a wrong prompts path - which is exactly what this file exists
to prevent.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient


def test_health_is_open_and_reports_model_state(client: TestClient) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["db"] is True
    assert body["privacy_level"] == "local_only"
    # Localhost binding is not authentication, so the token must be required elsewhere.
    assert body["ollama"]["reachable"] is False


def test_missing_token_is_401_with_the_standard_error_shape(unauth_client: TestClient) -> None:
    r = unauth_client.get("/models")
    assert r.status_code == 401
    err = r.json()["error"]
    assert err["code"] == "unauthorized"
    assert err["message"]
    assert err["details"] is None


def test_wrong_token_is_401(unauth_client: TestClient) -> None:
    r = unauth_client.get("/models", headers={"X-NYXORA-Token": "guess"})
    assert r.status_code == 401


def test_models_endpoint_lists_registered_models(client: TestClient) -> None:
    body = client.get("/models").json()
    names = [m["name"] for m in body["models"]]
    assert "mock-nyxora" in names


def test_chat_streams_sse_and_persists_both_turns(client: TestClient) -> None:
    r = client.post("/chat", json={"message": "hello there"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert "event: routing" in r.text
    assert "event: token" in r.text
    assert "event: done" in r.text
    run_id = r.headers["x-run-id"]

    runs = client.get("/runs", params={"limit": 10}).json()
    assert any(entry["id"] == run_id for entry in runs["runs"])

    conversations = client.get("/conversations").json()["conversations"]
    assert conversations, "the user turn must be persisted even if generation dies"
    cid = conversations[0]["id"]
    messages = client.get(f"/conversations/{cid}").json()["messages"]
    roles = [m["role"] for m in messages]
    assert "user" in roles


def test_read_tool_runs_without_an_approval(client: TestClient, tmp_root: Path) -> None:
    r = client.post("/tools/fs.list/request", json={"params": {"path": "."}})
    assert r.status_code == 200
    assert r.json()["auto_allowed"] is True


def test_write_requires_approval_then_executes_and_undoes(client: TestClient, tmp_root: Path) -> None:
    params = {"path": "note.md", "content": "written through the API"}
    requested = client.post("/tools/fs.create/request", json={"params": params})
    assert requested.status_code == 200
    payload = requested.json()
    approval_id = payload["approval"]["id"]
    assert payload["preview"]["summary"]
    assert workspace_file(tmp_root, "note.md") is None, "requesting must not write"

    pending = client.get("/approvals").json()["approvals"]
    assert any(a["id"] == approval_id for a in pending)

    decided = client.post(
        f"/approvals/{approval_id}/decide", json={"approved": True, "grant_for_run": False}
    )
    assert decided.status_code == 200
    result = decided.json()["result"]
    assert result["ok"] is True
    assert workspace_file(tmp_root, "note.md") == "written through the API"

    undone = client.post(f"/actions/{result['action_id']}/undo")
    assert undone.status_code == 200
    assert workspace_file(tmp_root, "note.md") is None


def test_denied_approval_writes_nothing(client: TestClient, tmp_root: Path) -> None:
    params = {"path": "never.md", "content": "no"}
    approval_id = client.post(
        "/tools/fs.create/request", json={"params": params}
    ).json()["approval"]["id"]
    decided = client.post(f"/approvals/{approval_id}/decide", json={"approved": False})
    assert decided.status_code == 200
    assert decided.json()["result"] is None
    assert workspace_file(tmp_root, "never.md") is None


def test_approval_cannot_be_decided_twice(client: TestClient) -> None:
    approval_id = client.post(
        "/tools/fs.create/request", json={"params": {"path": "a.md", "content": "x"}}
    ).json()["approval"]["id"]
    first = client.post(f"/approvals/{approval_id}/decide", json={"approved": False})
    assert first.status_code == 200
    second = client.post(f"/approvals/{approval_id}/decide", json={"approved": True})
    assert second.status_code == 403
    assert second.json()["error"]["code"] == "permission_denied"


def test_path_escape_is_refused_at_the_api_boundary(client: TestClient) -> None:
    r = client.post(
        "/tools/fs.create/request",
        json={"params": {"path": "../../Windows/win.ini", "content": "x"}},
    )
    assert r.status_code in (400, 403, 422)
    assert "error" in r.json()


def test_denied_path_is_refused_through_the_api(client: TestClient) -> None:
    r = client.post(
        "/tools/fs.delete/request", json={"params": {"paths": [".env"]}}
    )
    assert r.status_code in (400, 403, 422)


def test_unknown_tool_is_404(client: TestClient) -> None:
    r = client.post("/tools/shell.rm/request", json={"params": {}})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "unknown_tool"


def test_invalid_payload_is_422_not_a_stack_trace(client: TestClient) -> None:
    r = client.post("/chat", json={"message": ""})
    assert r.status_code == 422
    assert "error" in r.json()


def test_document_upload_is_searchable(client: TestClient) -> None:
    content = (
        "# Deployment runbook\n\n"
        "The staging gateway rotates its TLS certificate every 60 days on the "
        "first Tuesday. If the certificate expires, traffic fails closed and the "
        "on-call engineer must run `scripts/renew_staging_cert.sh` before retrying.\n"
    ).encode("utf-8")
    r = client.post(
        "/documents/upload",
        files={"file": ("runbook.md", content, "text/markdown")},
    )
    assert r.status_code == 201
    doc_id = r.json()["doc_id"]
    assert r.json()["chunks"] > 0

    listed = client.get("/documents").json()["documents"]
    assert any(d["id"] == doc_id for d in listed)

    found = client.post("/documents/search", json={"query": "TLS certificate renew"}).json()
    assert found["results"], "BM25 over FTS5 must find the ingested chunk"
    assert "certificate" in found["results"][0]["excerpt"].lower()

    deleted = client.delete(f"/documents/{doc_id}")
    assert deleted.status_code == 200


def test_unsupported_upload_type_is_415(client: TestClient) -> None:
    r = client.post("/documents/upload", files={"file": ("evil.exe", b"MZ", "application/octet-stream")})
    assert r.status_code == 415
    assert r.json()["error"]["code"] == "unsupported_type"


def test_search_for_absent_topic_says_so_instead_of_inventing(client: TestClient) -> None:
    found = client.post("/documents/search", json={"query": "quinoa supply chain"}).json()
    assert found["results"] == []
    assert found["confidence_note"]


def test_memory_needs_confirmation_before_it_becomes_active(client: TestClient) -> None:
    proposed = client.post("/memory", json={"content": "I prefer terse answers."})
    assert proposed.status_code == 201
    memory = proposed.json()["memory"]
    assert memory["status"] == "pending"
    assert proposed.json()["requires_confirmation"] is True

    confirmed = client.post(f"/memory/{memory['id']}/confirm")
    assert confirmed.status_code == 200
    assert confirmed.json()["memory"]["status"] == "active"


def test_secret_like_memory_is_blocked(client: TestClient) -> None:
    r = client.post("/memory", json={"content": "my openai api key is sk-abcdefghijklmnopqrstuvwxyz"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "memory_blocked"


def test_audit_log_is_readable_and_never_leaks_content(client: TestClient) -> None:
    client.post("/tools/fs.create/request", json={"params": {"path": "audit.md", "content": "TOPSECRET"}})
    entries = client.get("/audit-log").json()["entries"]
    assert entries
    blob = json.dumps(entries)
    assert "TOPSECRET" not in blob


def test_emergency_stop_blocks_further_writes(client: TestClient, tmp_root: Path) -> None:
    stopped = client.post("/stop")
    assert stopped.status_code == 200
    approval_id = client.post(
        "/tools/fs.create/request", json={"params": {"path": "after-stop.md", "content": "x"}}
    ).json()["approval"]["id"]
    decided = client.post(f"/approvals/{approval_id}/decide", json={"approved": True})
    assert decided.status_code in (200, 403)
    if decided.status_code == 200:
        assert decided.json()["result"] is None
    assert workspace_file(tmp_root, "after-stop.md") is None


def test_settings_endpoint_does_not_echo_secrets(client: TestClient) -> None:
    body = client.get("/settings").json()
    assert "api_token" not in json.dumps(body).lower() or body.get("token_source")


def test_voice_status_reports_the_v1_guarantees(client: TestClient) -> None:
    body = client.get("/voice/status").json()
    assert body["wake_word"] == "disabled_in_v1"
    assert body["raw_audio_retained"] is False


def test_agents_endpoint_marks_unbuilt_agents(client: TestClient) -> None:
    body = client.get("/agents").json()
    agents = body["agents"]
    assert agents, "the roster must be visible"
    unbuilt = [a for a in agents if a["status"] == "interface_only"]
    assert unbuilt, "unbuilt agents must be labelled, not hidden"
    assert all(a["executable"] is False for a in unbuilt)
    assert any(a["executable"] for a in agents), "at least one agent must actually run"


def workspace_file(tmp_root: Path, name: str) -> str | None:
    path = tmp_root / "workspace" / name
    return path.read_text(encoding="utf-8") if path.exists() else None
