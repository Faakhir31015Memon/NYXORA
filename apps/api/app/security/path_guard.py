"""Path Guard: the single choke point between a model's intent and the filesystem.

Every tool path resolution goes through here. The rules are ordered so that the
cheapest, most obvious rejection fires first, and so that a symlink that points
outside the root is caught *after* the OS resolves it.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PureWindowsPath

# Windows reserved device names are never valid path segments.
_RESERVED = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}

_DRIVE_RE = re.compile(r"^[a-zA-Z]:")
_UNC_RE = re.compile(r"^\\\\")
_DOTDOT_RE = re.compile(r"(^|[\\/])\.\.([\\/]|$)")


class PathViolation(StrEnum):
    EMPTY = "empty path"
    DOTDOT = "parent-directory traversal"
    ABSOLUTE = "absolute path outside root"
    SYMLINK_ESCAPE = "resolves outside root through a link"
    ESCAPES_ROOT = "resolves outside root"
    RESERVED_NAME = "reserved device name"
    UNC_PATH = "UNC path rejected"
    CONTROL_CHARS = "control characters rejected"
    DENY_LISTED = "path is on the deny list"


@dataclass(frozen=True)
class GuardedPath:
    path: Path
    relative: str


class PathGuardError(ValueError):
    def __init__(self, violation: PathViolation, target: str) -> None:
        self.violation = violation
        super().__init__(f"{violation.value}: {target!r}")


DEFAULT_DENY_GLOBS: tuple[str, ...] = (
    ".env",
    ".env.*",
    "*.token",
    "id_rsa",
    "id_ed25519",
    "*.pem",
    "*.key",
    ".git/config",
    ".git/hooks/*",
    "credentials",
    "secrets.json",
    "known_hosts",
    "authorized_keys",
    "shadow",
    "passwd",
)


class PathGuard:
    def __init__(
        self,
        root: Path,
        *,
        deny_globs: tuple[str, ...] = DEFAULT_DENY_GLOBS,
        allow_root_link: bool = False,
    ) -> None:
        self.root = Path(root).expanduser().resolve(strict=False)
        self.deny_globs = deny_globs
        self._allow_root_link = allow_root_link

    def resolve(self, raw: str, *, must_be_relative: bool = True) -> GuardedPath:
        if raw is None or not str(raw).strip():
            raise PathGuardError(PathViolation.EMPTY, str(raw))
        target = str(raw)

        if any(ord(c) < 32 for c in target):
            raise PathGuardError(PathViolation.CONTROL_CHARS, target)
        if _UNC_RE.match(target):
            raise PathGuardError(PathViolation.UNC_PATH, target)
        if _DOTDOT_RE.search(target):
            raise PathGuardError(PathViolation.DOTDOT, target)
        if _DRIVE_RE.match(target):
            # An absolute drive path is only acceptable if it is inside root.
            candidate = Path(target)
            if not self._inside_root(candidate):
                raise PathGuardError(PathViolation.ABSOLUTE, target)

        for segment in PureWindowsPath(target.replace("\\", "/")).parts:
            # Windows reserves the name part before the first dot, so "con.txt" is
            # just as invalid as "con".
            stem = segment.lower().split(".")[0].strip()
            if stem in _RESERVED:
                raise PathGuardError(PathViolation.RESERVED_NAME, target)

        if must_be_relative:
            candidate = (self.root / target.lstrip("/\\")).resolve(strict=False)
        else:
            candidate = Path(target).resolve(strict=False)

        if not self._inside_root(candidate):
            raise PathGuardError(PathViolation.ESCAPES_ROOT, target)

        # Re-check after resolution: this is what catches symlink/junction escapes,
        # because resolve() followed the link but the result left the root.
        real = _realpath(candidate)
        real_root = _realpath(self.root)
        outside = _common_prefix(real) != real_root and not _is_within(real, real_root)
        if real != real_root and outside:
            raise PathGuardError(PathViolation.SYMLINK_ESCAPE, target)

        rel = _relativize(real, real_root)
        self._check_deny(rel, target)
        return GuardedPath(path=real, relative=rel)

    def _inside_root(self, candidate: Path) -> bool:
        try:
            candidate.resolve(strict=False).relative_to(self.root)
            return True
        except ValueError:
            return _is_within(candidate, self.root)

    def _check_deny(self, rel: str, original: str) -> None:
        norm = rel.replace("\\", "/").lower()
        parts = [p for p in norm.split("/") if p]
        for pattern in self.deny_globs:
            pat = pattern.lower()
            if _glob_matches(norm, pat) or _glob_matches(parts[-1] if parts else "", pat):
                raise PathGuardError(PathViolation.DENY_LISTED, original)
            # Also catch nested secrets, e.g. sub/.env or apps/.env.local
            for i in range(len(parts)):
                if _glob_matches("/".join(parts[i:]), pat):
                    raise PathGuardError(PathViolation.DENY_LISTED, original)


def _realpath(p: Path) -> Path:
    """Resolve symlinks/junctions where possible; tolerate non-existent tails."""
    try:
        return Path(os.path.realpath(str(p)))
    except OSError:
        return Path(os.path.normpath(str(p)))


def _common_prefix(p: Path) -> Path:
    parts = p.parts
    return Path(*parts[:1]) if parts else p


def _is_within(child: Path, parent: Path) -> bool:
    try:
        Path(str(child)).relative_to(Path(str(parent)))
        return True
    except ValueError:
        return False


def _relativize(child: Path, parent: Path) -> str:
    try:
        return str(child.relative_to(parent))
    except ValueError:
        return str(child)


def _glob_matches(value: str, pattern: str) -> bool:
    """Minimal glob: '*' matches any run, '?' one char. Case-insensitive input assumed."""
    regex = (
        "^"
        + re.escape(pattern)
        .replace(r"\*\*", "§§")
        .replace(r"\*", "[^/]*")
        .replace("§§", ".*")
        .replace(r"\?", ".")
        + "$"
    )
    return re.match(regex, value) is not None
