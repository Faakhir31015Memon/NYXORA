"""Undo: every consequential file action snapshots what it replaced first.

Layout: <root>/.nyx/undo/<action_id>/manifest.json + captured original bytes.
Undo restores the exact prior state, or deletes a file the action created.
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MANIFEST = "manifest.json"


class UndoError(RuntimeError):
    pass


class UndoStore:
    def __init__(self, root: Path, retention_days: int = 7) -> None:
        self.root = Path(root)
        self.base = self.root / ".nyx" / "undo"
        self.retention_days = retention_days
        self.base.mkdir(parents=True, exist_ok=True)

    def snapshot(
        self,
        action_id: str,
        target: Path,
        *,
        tool: str,
        will_create: bool,
        extra: dict[str, Any] | None = None,
    ) -> Path:
        """Call BEFORE the mutation. Records the prior bytes or the prior tree."""
        directory = self.base / action_id
        directory.mkdir(parents=True, exist_ok=True)
        manifest: dict[str, Any] = {
            "action_id": action_id,
            "tool": tool,
            "target": str(target),
            "will_create": will_create,
            "is_dir": target.is_dir(),
            "existed": target.exists(),
            "created_at": datetime.now(UTC).isoformat(),
            "extra": extra or {},
        }
        if target.is_dir():
            archive = directory / "tree.tar"
            shutil.make_archive(
                str(archive.with_suffix("")), "tar", root_dir=target.parent, base_dir=target.name
            )
            manifest["archive"] = str(archive)
        elif target.exists():
            shutil.copy2(target, directory / "original")
            manifest["backup"] = str(directory / "original")
        (directory / MANIFEST).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        return directory

    def undo(self, action_id: str) -> str:
        directory = self.base / action_id
        manifest_path = directory / MANIFEST
        if not manifest_path.exists():
            msg = f"no undo record for action {action_id}"
            raise UndoError(msg)
        manifest: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
        target = Path(str(manifest["target"]))

        if manifest["will_create"] and not manifest["existed"]:
            # The action created this - undo means remove it.
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()
            return f"removed {target}"

        if "backup" in manifest and Path(str(manifest["backup"])).exists():
            shutil.copy2(str(manifest["backup"]), target)
            return f"restored {target}"

        if "archive" in manifest and Path(str(manifest["archive"])).exists():
            shutil.rmtree(target, ignore_errors=True)
            shutil.unpack_archive(str(manifest["archive"]), target.parent)
            return f"restored directory {target}"

        msg = f"undo record for {action_id} has no restorable payload"
        raise UndoError(msg)

    def list_actions(self) -> list[str]:
        return (
            sorted(p.name for p in self.base.iterdir() if p.is_dir()) if self.base.exists() else []
        )

    def prune(self) -> int:
        """Drop snapshots older than the retention window."""
        cutoff = datetime.now(UTC).timestamp() - self.retention_days * 86400
        removed = 0
        if not self.base.exists():
            return 0
        for directory in self.base.iterdir():
            manifest_path = directory / MANIFEST
            if not manifest_path.exists():
                continue
            try:
                created = datetime.fromisoformat(
                    str(json.loads(manifest_path.read_text(encoding="utf-8"))["created_at"])
                )
            except (ValueError, KeyError, json.JSONDecodeError):
                continue
            if created.timestamp() < cutoff:
                shutil.rmtree(directory, ignore_errors=True)
                removed += 1
        return removed
