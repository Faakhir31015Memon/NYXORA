"""Tool Runtime: V1 file tools, all confined to the workspace root.

Every run() is preceded by PermissionController.consume() and followed by an
audit write. There is no shell tool in V1 by design.
"""

from __future__ import annotations

import difflib
import shutil
import uuid
from collections.abc import Awaitable, Callable
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, Field

from app.security.audit import AuditLog
from app.security.path_guard import GuardedPath, PathGuard
from app.security.permissions import PermissionClass, PermissionController
from app.security.undo import UndoStore


class PermissionClassTag(StrEnum):
    READ = "read"
    WRITE = "write"
    DELETE = "delete"


class ToolContext(BaseModel):
    run_id: str | None = None
    actor: str = "user"
    approval_id: str | None = None
    model_config = {"arbitrary_types_allowed": True}


class ActionPreview(BaseModel):
    summary: str
    diff: str | None = None
    files: list[str] = Field(default_factory=list)
    permission_class: str = "read"
    risk_note: str | None = None


class ToolResult(BaseModel):
    ok: bool
    data: Any = None
    error: str | None = None
    action_id: str | None = None
    undoable: bool = False


class ToolError(RuntimeError):
    pass


class Tool(Protocol):
    name: str
    description: str
    permission_class: PermissionClass

    def preview(self, params: dict[str, Any]) -> ActionPreview: ...

    async def run(self, params: dict[str, Any], ctx: ToolContext) -> ToolResult: ...


def unified_diff(old: str, new: str, name: str) -> str:
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"a/{name}",
            tofile=f"b/{name}",
        )
    )


class FileTools:
    """fs.list, fs.read, fs.mkdir, fs.create, fs.edit, fs.delete."""

    def __init__(
        self,
        guard: PathGuard,
        permissions: PermissionController,
        undo: UndoStore,
        audit: AuditLog,
        templates_root: Path | None = None,
    ) -> None:
        self.guard = guard
        self.permissions = permissions
        self.undo = undo
        self.audit = audit
        self.templates_root = templates_root

    # -- helpers -----------------------------------------------------------
    def _path(self, raw: str) -> GuardedPath:
        return self.guard.resolve(raw)

    # -- READ --------------------------------------------------------------
    async def fs_list(self, params: dict[str, Any], ctx: ToolContext) -> ToolResult:
        target = self._path(str(params.get("path", ".")))
        self.permissions.authorize_read("fs.list", params, ctx.run_id)
        if not target.path.is_dir():
            raise ToolError("not a directory")
        limit = int(params.get("limit", 500))
        entries = []
        for child in sorted(target.path.iterdir())[:limit]:
            entries.append(
                {
                    "name": child.name,
                    "path": str(Path(target.relative) / child.name).replace("\\", "/"),
                    "is_dir": child.is_dir(),
                    "size": child.stat().st_size if child.is_file() else None,
                }
            )
        self.audit.write(
            actor=ctx.actor,
            outcome="executed",
            tool="fs.list",
            params=params,
            permission_class="read",
            run_id=ctx.run_id,
        )
        return ToolResult(ok=True, data={"entries": entries, "count": len(entries)})

    async def fs_read(self, params: dict[str, Any], ctx: ToolContext) -> ToolResult:
        target = self._path(str(params.get("path", "")))
        self.permissions.authorize_read("fs.read", params, ctx.run_id)
        if not target.path.is_file():
            raise ToolError("not a file")
        max_bytes = int(params.get("max_bytes", 262_144))
        raw = target.path.read_bytes()[:max_bytes]
        self.audit.write(
            actor=ctx.actor,
            outcome="executed",
            tool="fs.read",
            params=params,
            permission_class="read",
            run_id=ctx.run_id,
        )
        return ToolResult(
            ok=True,
            data={
                "content": raw.decode("utf-8", errors="replace"),
                "truncated": target.path.stat().st_size > max_bytes,
            },
        )

    # -- WRITE -------------------------------------------------------------
    async def fs_mkdir(self, params: dict[str, Any], ctx: ToolContext) -> ToolResult:
        target = self._path(str(params.get("path", "")))
        self.permissions.consume(_req(ctx), "fs.mkdir", params)
        action_id = str(uuid.uuid4())
        self.undo.snapshot(action_id, target.path, tool="fs.mkdir", will_create=True)
        target.path.mkdir(parents=True, exist_ok=True)
        self.audit.write(
            actor=ctx.actor,
            outcome="executed",
            tool="fs.mkdir",
            params=params,
            permission_class="write",
            approval_id=ctx.approval_id,
            run_id=ctx.run_id,
        )
        return ToolResult(
            ok=True, data={"path": target.relative}, action_id=action_id, undoable=True
        )

    async def fs_create(self, params: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path = self._path(str(params.get("path", "")))
        content = str(params.get("content", ""))
        self.permissions.consume(_req(ctx), "fs.create", params)
        action_id = str(uuid.uuid4())
        self.undo.snapshot(action_id, path.path, tool="fs.create", will_create=True)
        path.path.parent.mkdir(parents=True, exist_ok=True)
        path.path.write_text(content, encoding="utf-8")
        self.audit.write(
            actor=ctx.actor,
            outcome="executed",
            tool="fs.create",
            params={**params, "content": f"<{len(content)} chars>"},
            permission_class="write",
            approval_id=ctx.approval_id,
            run_id=ctx.run_id,
        )
        return ToolResult(
            ok=True,
            data={"path": path.relative, "bytes": len(content)},
            action_id=action_id,
            undoable=True,
        )

    async def fs_edit(self, params: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path = self._path(str(params.get("path", "")))
        content = str(params.get("content", ""))
        if not path.path.is_file():
            raise ToolError("fs.edit requires an existing file; use fs.create")
        self.permissions.consume(_req(ctx), "fs.edit", params)
        action_id = str(uuid.uuid4())
        self.undo.snapshot(action_id, path.path, tool="fs.edit", will_create=False)
        path.path.write_text(content, encoding="utf-8")
        self.audit.write(
            actor=ctx.actor,
            outcome="executed",
            tool="fs.edit",
            params={**params, "content": f"<{len(content)} chars>"},
            permission_class="write",
            approval_id=ctx.approval_id,
            run_id=ctx.run_id,
        )
        return ToolResult(ok=True, data={"path": path.relative}, action_id=action_id, undoable=True)

    # -- DELETE ------------------------------------------------------------
    async def fs_delete(self, params: dict[str, Any], ctx: ToolContext) -> ToolResult:
        targets = params.get("paths")
        if not isinstance(targets, list) or not targets:
            raise ToolError("fs.delete requires an explicit non-empty 'paths' list")
        resolved = [self._path(str(p)) for p in targets]
        self.permissions.consume(_req(ctx), "fs.delete", params)
        action_id = str(uuid.uuid4())
        for item in resolved:
            self.undo.snapshot(action_id, item.path, tool="fs.delete", will_create=False)
            if item.path.is_dir():
                shutil.rmtree(item.path)
            elif item.path.exists():
                item.path.unlink()
        self.audit.write(
            actor=ctx.actor,
            outcome="executed",
            tool="fs.delete",
            params={"count": len(resolved)},
            permission_class="delete",
            approval_id=ctx.approval_id,
            run_id=ctx.run_id,
        )
        return ToolResult(
            ok=True,
            data={"deleted": [r.relative for r in resolved]},
            action_id=action_id,
            undoable=True,
        )

    # -- SCAFFOLD ----------------------------------------------------------
    async def project_scaffold(self, params: dict[str, Any], ctx: ToolContext) -> ToolResult:
        template = str(params.get("template", "empty"))
        name = str(params.get("project_name", "untitled"))
        if self.templates_root is None:
            raise ToolError("no scaffold templates configured")
        src = self.templates_root / template
        if not src.is_dir():
            available = [p.name for p in self.templates_root.iterdir() if p.is_dir()]
            raise ToolError(f"unknown template {template!r}; available: {available}")
        dest = self._path(f"{params.get('parent', '.')}/{_slug(name)}")
        self.permissions.consume(_req(ctx), "project.scaffold", params)
        action_id = str(uuid.uuid4())
        self.undo.snapshot(action_id, dest.path, tool="project.scaffold", will_create=True)
        written: list[str] = []
        variables = {"project_name": name, "slug": _slug(name)}
        for file in sorted(src.rglob("*")):
            if not file.is_file():
                continue
            rel = file.relative_to(src)
            out_rel = _substitute(str(rel), variables)
            out_path = dest.path / out_rel
            out_path.parent.mkdir(parents=True, exist_ok=True)
            text = file.read_text(encoding="utf-8", errors="replace")
            out_path.write_text(_substitute(text, variables), encoding="utf-8")
            written.append(str(Path(dest.relative) / out_rel).replace("\\", "/"))
        self.audit.write(
            actor=ctx.actor,
            outcome="executed",
            tool="project.scaffold",
            params=params,
            permission_class="write",
            approval_id=ctx.approval_id,
            run_id=ctx.run_id,
        )
        return ToolResult(ok=True, data={"files": written}, action_id=action_id, undoable=True)

    # -- previews ----------------------------------------------------------
    def preview_write(self, tool: str, params: dict[str, Any]) -> ActionPreview:
        if tool in {"fs.edit"}:
            path = self._path(str(params.get("path", "")))
            old = (
                path.path.read_text(encoding="utf-8", errors="replace")
                if path.path.exists()
                else ""
            )
            return ActionPreview(
                summary=f"Edit {path.relative}",
                diff=unified_diff(old, str(params.get("content", "")), path.relative),
                files=[path.relative],
                permission_class="write",
                risk_note="Existing file contents will be replaced. Undo snapshot is taken first.",
            )
        if tool in {"fs.create", "fs.mkdir"}:
            path = self._path(str(params.get("path", "")))
            note = (
                "A new file will be created."
                if tool == "fs.create"
                else "A new directory will be created."
            )
            if path.path.exists():
                note = "Target already exists - contents will be overwritten."
            return ActionPreview(
                summary=f"Create {path.relative}",
                diff=unified_diff("", str(params.get("content", "")), path.relative)
                if tool == "fs.create"
                else None,
                files=[path.relative],
                permission_class="write",
                risk_note=note,
            )
        if tool == "project.scaffold":
            template = str(params.get("template", "empty"))
            name = str(params.get("project_name", "untitled"))
            slug = _slug(name)
            files: list[str] = []
            if self.templates_root and (self.templates_root / template).is_dir():
                for f in sorted((self.templates_root / template).rglob("*")):
                    if f.is_file():
                        rel = _substitute(
                            str(f.relative_to(self.templates_root / template)), {"slug": slug}
                        )
                        files.append(f"{slug}/{rel}".replace("\\", "/"))
            return ActionPreview(
                summary=f"Scaffold '{name}' from template '{template}'",
                files=files,
                permission_class="write",
                risk_note=f"{len(files)} new file(s) will be written under the workspace root.",
            )
        if tool == "fs.delete":
            paths = [self._path(str(p)).relative for p in params.get("paths", [])]
            return ActionPreview(
                summary=f"Delete {len(paths)} target(s)",
                files=paths,
                permission_class="delete",
                risk_note="Permanent from the app's view unless undone; originals are snapshotted.",
            )
        return ActionPreview(summary=f"{tool}", permission_class="write")


def _req(ctx: ToolContext) -> str:
    if not ctx.approval_id:
        raise ToolError("execution blocked: no approval supplied for a consequential action")
    return ctx.approval_id


def _slug(value: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in value.lower()).strip("_") or "project"


def _substitute(text: str, variables: dict[str, str]) -> str:
    """Plain {{var}} replacement. Templates never execute code in V1."""
    out = text
    for key, value in variables.items():
        out = out.replace("{{" + key + "}}", value)
    return out


ToolFn = Callable[[dict[str, Any], ToolContext], Awaitable[ToolResult]]

TOOL_PERMISSIONS: dict[str, PermissionClass] = {
    "fs.list": PermissionClass.READ,
    "fs.read": PermissionClass.READ,
    "fs.mkdir": PermissionClass.WRITE,
    "fs.create": PermissionClass.WRITE,
    "fs.edit": PermissionClass.WRITE,
    "project.scaffold": PermissionClass.WRITE,
    "fs.delete": PermissionClass.DELETE,
}
