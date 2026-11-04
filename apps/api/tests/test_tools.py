"""File tools + undo + audit. Everything confined to the workspace root."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.core.tools import FileTools, ToolContext, ToolError, ToolResult
from app.security.path_guard import PathGuardError
from app.security.permissions import PermissionClass, PermissionDenied


@pytest.fixture
def ctx() -> ToolContext:
    return ToolContext(run_id=None, actor="test")


async def approve(
    file_tools: FileTools, tool: str, params: dict[str, Any], cls: PermissionClass
) -> str:
    approval = file_tools.permissions.request(tool, params, cls, preview="test")
    file_tools.permissions.decide(approval.id, True)
    return approval.id


def undo_id(result: ToolResult) -> str:
    """A successful mutation must always register an undo action."""
    assert result.action_id is not None
    return result.action_id


async def test_read_and_list_are_allowed_without_approval(
    file_tools: FileTools, workspace: Path, ctx: ToolContext
) -> None:
    (workspace / "hello.txt").write_text("hi there", encoding="utf-8")
    listing = await file_tools.fs_list({"path": "."}, ctx)
    assert listing.ok and any(e["name"] == "hello.txt" for e in listing.data["entries"])
    read = await file_tools.fs_read({"path": "hello.txt"}, ctx)
    assert read.data["content"] == "hi there"


async def test_create_requires_approval_and_blocks_without_it(
    file_tools: FileTools, ctx: ToolContext
) -> None:
    with pytest.raises(ToolError):
        await file_tools.fs_create({"path": "x.txt", "content": "x"}, ctx)
    params = {"path": "x.txt", "content": "x"}
    approval_id = await approve(file_tools, "fs.create", params, PermissionClass.WRITE)
    result = await file_tools.fs_create(params, ToolContext(approval_id=approval_id, actor="test"))
    assert result.ok
    assert (file_tools.guard.root / "x.txt").read_text(encoding="utf-8") == "x"


async def test_write_outside_root_is_blocked_even_with_approval(
    file_tools: FileTools, tmp_root: Path
) -> None:
    params = {"path": "../escape.txt", "content": "nope"}
    with pytest.raises(PathGuardError):
        file_tools.preview_write("fs.create", params)


async def test_approval_cannot_be_reused_for_different_content(
    file_tools: FileTools, workspace: Path
) -> None:
    params = {"path": "a.txt", "content": "safe"}
    approval_id = await approve(file_tools, "fs.create", params, PermissionClass.WRITE)
    with pytest.raises(PermissionDenied):
        await file_tools.fs_create(
            {"path": "a.txt", "content": "evil"}, ToolContext(approval_id=approval_id, actor="test")
        )
    assert not (workspace / "a.txt").exists()


async def test_create_is_undone_by_deleting_the_file(
    file_tools: FileTools, workspace: Path
) -> None:
    params = {"path": "new.txt", "content": "created"}
    approval_id = await approve(file_tools, "fs.create", params, PermissionClass.WRITE)
    result = await file_tools.fs_create(params, ToolContext(approval_id=approval_id, actor="test"))
    assert (workspace / "new.txt").exists()
    file_tools.undo.undo(undo_id(result))
    assert not (workspace / "new.txt").exists()


async def test_edit_snapshot_restores_original(file_tools: FileTools, workspace: Path) -> None:
    (workspace / "doc.md").write_text("ORIGINAL", encoding="utf-8")
    params = {"path": "doc.md", "content": "REPLACED"}
    approval_id = await approve(file_tools, "fs.edit", params, PermissionClass.WRITE)
    result = await file_tools.fs_edit(params, ToolContext(approval_id=approval_id, actor="test"))
    assert (workspace / "doc.md").read_text(encoding="utf-8") == "REPLACED"
    file_tools.undo.undo(undo_id(result))
    assert (workspace / "doc.md").read_text(encoding="utf-8") == "ORIGINAL"


async def test_edit_preview_shows_unified_diff(file_tools: FileTools, workspace: Path) -> None:
    (workspace / "f.py").write_text("a = 1\n", encoding="utf-8")
    preview = file_tools.preview_write("fs.edit", {"path": "f.py", "content": "a = 2\n"})
    assert "-a = 1" in (preview.diff or "")
    assert "+a = 2" in (preview.diff or "")
    assert preview.permission_class == "write"


async def test_delete_requires_explicit_list_and_is_undoable(
    file_tools: FileTools, workspace: Path
) -> None:
    (workspace / "doomed.txt").write_text("bye", encoding="utf-8")
    with pytest.raises(ToolError):
        await file_tools.fs_delete({}, ToolContext(actor="test"))
    with pytest.raises(ToolError):
        await file_tools.fs_delete({"paths": []}, ToolContext(actor="test"))
    params = {"paths": ["doomed.txt"]}
    approval_id = await approve(file_tools, "fs.delete", params, PermissionClass.DELETE)
    result = await file_tools.fs_delete(params, ToolContext(approval_id=approval_id, actor="test"))
    assert not (workspace / "doomed.txt").exists()
    file_tools.undo.undo(undo_id(result))
    assert (workspace / "doomed.txt").read_text(encoding="utf-8") == "bye"


async def test_delete_of_denied_path_is_blocked(file_tools: FileTools, workspace: Path) -> None:
    (workspace / ".env").write_text("SECRET=1", encoding="utf-8")
    with pytest.raises(PathGuardError):
        file_tools.preview_write("fs.delete", {"paths": [".env"]})


async def test_scaffold_writes_files_and_substitutes_variables(
    file_tools: FileTools, workspace: Path
) -> None:
    params = {"template": "python_dashboard", "project_name": "My Dash", "parent": "."}
    approval_id = await approve(file_tools, "project.scaffold", params, PermissionClass.WRITE)
    result = await file_tools.project_scaffold(
        params, ToolContext(approval_id=approval_id, actor="test")
    )
    assert result.ok
    project_dir = workspace / "My_Dash"
    assert project_dir.is_dir()
    assert "My Dash" in (project_dir / "README.md").read_text(encoding="utf-8")
    assert "my_dash" in (project_dir / "app.py").read_text(encoding="utf-8")
    preview = file_tools.preview_write("project.scaffold", params)
    assert len(preview.files) == 2


async def test_scaffold_undo_removes_whole_project(file_tools: FileTools, workspace: Path) -> None:
    params = {"template": "python_dashboard", "project_name": "temp", "parent": "."}
    approval_id = await approve(file_tools, "project.scaffold", params, PermissionClass.WRITE)
    result = await file_tools.project_scaffold(
        params, ToolContext(approval_id=approval_id, actor="test")
    )
    assert (workspace / "temp").is_dir()
    file_tools.undo.undo(undo_id(result))
    assert not (workspace / "temp").exists()


async def test_unknown_template_lists_available(file_tools: FileTools) -> None:
    with pytest.raises(ToolError) as exc:
        await file_tools.project_scaffold(
            {"template": "nope", "project_name": "x"}, ToolContext(approval_id="none", actor="test")
        )
    assert "python_dashboard" in str(exc.value)


async def test_every_execution_is_audited(
    file_tools: FileTools, workspace: Path, ctx: ToolContext
) -> None:
    await file_tools.fs_list({"path": "."}, ctx)
    entries = file_tools.audit.read()
    assert any(e.tool == "fs.list" and e.outcome == "executed" for e in entries)


async def test_audit_redacts_secrets_before_writing(file_tools: FileTools, tmp_root: Path) -> None:
    file_tools.audit.write(
        actor="test",
        outcome="executed",
        tool="fs.create",
        params={"path": "a", "content": "API_KEY=sk-live-abcdef123456"},
        permission_class="write",
    )
    raw = (tmp_root / "audit.jsonl").read_text(encoding="utf-8")
    assert "sk-live-abcdef123456" not in raw
    assert "REDACTED" in raw


async def test_emergency_stop_blocks_tool_execution_entirely(
    file_tools: FileTools, workspace: Path
) -> None:
    file_tools.permissions.emergency_stop()
    with pytest.raises(PermissionDenied):
        file_tools.permissions.request(
            "fs.create", {"path": "a"}, PermissionClass.WRITE, preview="p"
        )
