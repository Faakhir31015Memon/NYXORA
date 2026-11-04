"""Shared fixtures. Everything runs against MockAdapter + a temp workspace."""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine

from app.config import Settings
from app.core.tools import FileTools
from app.db.models import init_db, make_engine
from app.main import create_app
from app.models.mock import MockAdapter
from app.models.registry import ModelRegistry
from app.models.router import ModelRouter
from app.security.audit import AuditLog
from app.security.path_guard import PathGuard
from app.security.permissions import PermissionController
from app.security.undo import UndoStore


@pytest.fixture
def tmp_root() -> Iterator[Path]:
    path = Path(tempfile.mkdtemp(prefix="nyxora-test-"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def workspace(tmp_root: Path) -> Path:
    root = tmp_root / "workspace"
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def templates(tmp_root: Path) -> Path:
    root = tmp_root / "templates"
    (root / "python_dashboard").mkdir(parents=True)
    (root / "python_dashboard" / "README.md").write_text("# {{project_name}}\n", encoding="utf-8")
    (root / "python_dashboard" / "app.py").write_text("print('{{slug}}')\n", encoding="utf-8")
    (root / "empty").mkdir()
    (root / "empty" / "README.md").write_text("# {{project_name}}\n", encoding="utf-8")
    return root


@pytest.fixture
def guard(workspace: Path) -> PathGuard:
    return PathGuard(workspace)


@pytest.fixture
def permissions(tmp_root: Path) -> PermissionController:
    return PermissionController(expiry_s=300)


@pytest.fixture
def undo(workspace: Path) -> UndoStore:
    return UndoStore(workspace)


@pytest.fixture
def audit(tmp_root: Path) -> AuditLog:
    return AuditLog(tmp_root / "audit.jsonl")


@pytest.fixture
def file_tools(
    guard: PathGuard,
    permissions: PermissionController,
    undo: UndoStore,
    audit: AuditLog,
    templates: Path,
) -> FileTools:
    return FileTools(guard, permissions, undo, audit, templates_root=templates)


@pytest.fixture
def registry() -> ModelRegistry:
    reg = ModelRegistry()
    mock = MockAdapter()
    reg.register_adapter(mock)
    return reg


@pytest.fixture
def router_(registry: ModelRegistry) -> ModelRouter:
    return ModelRouter(registry)


@pytest.fixture
def engine(tmp_root: Path) -> Engine:
    eng = make_engine(tmp_root / "test.db")
    init_db(eng)
    return eng


def make_settings(tmp_root: Path) -> Settings:
    return Settings(
        NYXORA_DB_PATH=str(tmp_root / "data" / "nyxora.db"),
        NYXORA_WORKSPACE_ROOT=str(tmp_root / "workspace"),
        NYXORA_API_TOKEN="test-token",
        OLLAMA_BASE_URL="http://127.0.0.1:1",
        PRIVACY_LEVEL="local_only",
        MAX_UPLOAD_SIZE_MB=5,
    )


@pytest.fixture
def client(tmp_root: Path) -> Iterator[TestClient]:
    settings = make_settings(tmp_root)
    (tmp_root / "workspace").mkdir(parents=True, exist_ok=True)
    app = create_app(settings, force_mock=True, require_token=True)
    headers = {"X-NYXORA-Token": "test-token"}
    with TestClient(app, headers=headers) as test_client:
        yield test_client


@pytest.fixture
def unauth_client(tmp_root: Path) -> Iterator[TestClient]:
    settings = make_settings(tmp_root)
    (tmp_root / "workspace").mkdir(parents=True, exist_ok=True)
    app = create_app(settings, force_mock=True, require_token=True)
    with TestClient(app) as test_client:
        yield test_client
