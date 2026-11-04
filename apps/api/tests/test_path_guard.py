"""Adversarial Path Guard suite. These tests are the reason the file tools are safe."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.security.path_guard import PathGuard, PathGuardError, PathViolation


@pytest.fixture
def root(tmp_path: Path) -> Path:
    d = tmp_path / "workspace"
    d.mkdir()
    (d / "notes.txt").write_text("hello", encoding="utf-8")
    return d


def test_plain_relative_path_ok(root: Path) -> None:
    guarded = PathGuard(root).resolve("notes.txt")
    assert guarded.path.is_file()
    assert guarded.relative in {"notes.txt"}


def test_subpath_ok(root: Path) -> None:
    (root / "src").mkdir()
    assert PathGuard(root).resolve("src/main.py").relative.startswith("src")


def test_dotdot_traversal_blocked(root: Path, tmp_path: Path) -> None:
    with pytest.raises(PathGuardError) as exc:
        PathGuard(root).resolve("../outside.txt")
    assert exc.value.violation is PathViolation.DOTDOT


def test_deep_dotdot_blocked(root: Path) -> None:
    for attempt in (
        "a/../../outside",
        "./a/../../../etc/passwd",
        "sub/../..",
        "sub\\..\\..\\windows",
    ):
        with pytest.raises(PathGuardError):
            PathGuard(root).resolve(attempt)


def test_absolute_outside_root_blocked(root: Path, tmp_path: Path) -> None:
    outside = str(tmp_path / "secret.txt")
    with pytest.raises(PathGuardError) as exc:
        PathGuard(root).resolve(outside)
    assert exc.value.violation in {PathViolation.ABSOLUTE, PathViolation.ESCAPES_ROOT}


def test_absolute_inside_root_allowed(root: Path) -> None:
    inside = str(root / "notes.txt")
    guarded = PathGuard(root).resolve(inside, must_be_relative=False)
    assert guarded.path.is_file()


def test_dotdot_inside_path_that_escapes_blocked(root: Path) -> None:
    with pytest.raises(PathGuardError):
        PathGuard(root).resolve(f"{root}/notes.txt/../../../../etc/shadow")


def test_symlink_escape_blocked(root: Path, tmp_path: Path) -> None:
    """The core guarantee: a link whose target leaves the root is rejected."""
    outside = tmp_path / "outside_secret"
    outside.mkdir()
    (outside / "id_rsa").write_text("KEY", encoding="utf-8")
    link = root / "escape"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks require developer mode on Windows")
    with pytest.raises(PathGuardError) as exc:
        PathGuard(root).resolve("escape/id_rsa")
    assert exc.value.violation in {
        PathViolation.SYMLINK_ESCAPE,
        PathViolation.ESCAPES_ROOT,
        PathViolation.DENY_LISTED,
    }


def test_denied_secret_files_blocked(root: Path) -> None:
    (root / ".env").write_text("API_KEY=secret", encoding="utf-8")
    for target in (".env", "sub/.env", "config/.env.local"):
        with pytest.raises(PathGuardError) as exc:
            PathGuard(root).resolve(target)
        assert exc.value.violation is PathViolation.DENY_LISTED


def test_ssh_key_names_blocked(root: Path) -> None:
    for target in ("id_rsa", "deploy/id_ed25519", "certs/server.pem", ".ssh/authorized_keys"):
        with pytest.raises(PathGuardError) as exc:
            PathGuard(root).resolve(target)
        assert exc.value.violation is PathViolation.DENY_LISTED


def test_git_config_blocked(root: Path) -> None:
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("[core]", encoding="utf-8")
    with pytest.raises(PathGuardError) as exc:
        PathGuard(root).resolve(".git/config")
    assert exc.value.violation is PathViolation.DENY_LISTED


def test_windows_reserved_names_blocked(root: Path) -> None:
    for target in ("con.txt", "nul", "COM1", "LPT9.log", "aux"):
        with pytest.raises(PathGuardError) as exc:
            PathGuard(root).resolve(target)
        assert exc.value.violation is PathViolation.RESERVED_NAME


def test_unc_path_blocked(root: Path) -> None:
    with pytest.raises(PathGuardError) as exc:
        PathGuard(root).resolve("\\\\attacker\\share\\file.txt")
    assert exc.value.violation is PathViolation.UNC_PATH


def test_drive_letter_outside_root_blocked(root: Path) -> None:
    with pytest.raises(PathGuardError):
        PathGuard(root).resolve("C:\\Windows\\System32\\drivers\\etc\\hosts")


def test_control_characters_blocked(root: Path) -> None:
    with pytest.raises(PathGuardError) as exc:
        PathGuard(root).resolve("notes.txt\x00../secret")
    assert exc.value.violation is PathViolation.CONTROL_CHARS


def test_empty_path_blocked(root: Path) -> None:
    for bad in ("", "   ", "\t"):
        with pytest.raises(PathGuardError) as exc:
            PathGuard(root).resolve(bad)
        assert exc.value.violation is PathViolation.EMPTY


def test_null_byte_bypass_blocked(root: Path) -> None:
    with pytest.raises(PathGuardError):
        PathGuard(root).resolve("notes.txt\u0000.png")
