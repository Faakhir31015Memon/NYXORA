"""Shared, lazily-built singletons for the API process."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.engine import Engine

from app.config import Settings, get_settings
from app.core.agents import AgentManager
from app.core.engines import ContextBuilder
from app.core.events import EventBus, get_event_bus
from app.core.orchestrator import Orchestrator
from app.core.tools import FileTools
from app.core.voice import VoiceService
from app.db.models import init_db, make_engine
from app.models.base import ModelAdapter
from app.models.mock import MockAdapter
from app.models.ollama import OllamaAdapter
from app.models.openai_compat import OpenAICompatibleAdapter
from app.models.registry import ModelRegistry
from app.models.router import ModelRouter
from app.security.audit import AuditLog
from app.security.path_guard import PathGuard
from app.security.permissions import PermissionController
from app.security.undo import UndoStore

logger = logging.getLogger("nyxora.deps")


@dataclass
class Container:
    settings: Settings
    engine: Engine
    registry: ModelRegistry
    router: ModelRouter
    permissions: PermissionController
    guard: PathGuard
    undo: UndoStore
    audit: AuditLog
    tools: FileTools
    bus: EventBus
    orchestrator: Orchestrator
    agents: AgentManager
    voice: VoiceService
    context_builder: ContextBuilder
    adapters: dict[str, ModelAdapter] = field(default_factory=dict)


def build_container(settings: Settings | None = None, *, force_mock: bool = False) -> Container:
    cfg = settings or get_settings()
    logging.basicConfig(level=getattr(logging, cfg.log_level.upper(), logging.INFO))

    engine = make_engine(cfg.db_path)
    init_db(engine)

    registry = ModelRegistry()
    adapters: dict[str, ModelAdapter] = {}

    mock = MockAdapter()
    adapters["mock"] = mock
    registry.register_adapter(mock)

    if not force_mock:
        ollama = OllamaAdapter(cfg.ollama_base_url, timeout_s=cfg.ollama_timeout_s)
        adapters["ollama"] = ollama
        registry.register_adapter(ollama)
        if cfg.openai_api_key:
            external = OpenAICompatibleAdapter(
                base_url=cfg.openai_base_url, api_key=cfg.openai_api_key
            )
            adapters["openai"] = external
            registry.register_adapter(external)
        else:
            logger.info("external adapters disabled: no API key present")

    root = Path(cfg.workspace_root).resolve(strict=False)
    root.mkdir(parents=True, exist_ok=True)
    guard = PathGuard(root)
    undo = UndoStore(root, retention_days=cfg.undo_retention_days)
    audit = AuditLog(root / ".nyx" / "audit.log.jsonl")
    permissions = PermissionController(expiry_s=cfg.approval_expiry_s)
    tools = FileTools(guard, permissions, undo, audit, templates_root=_templates_root())

    system_prompt = (_prompts_dir() / "system_v1.md").read_text(encoding="utf-8")
    context_builder = ContextBuilder(system_prompt)

    router = ModelRouter(registry)
    bus = get_event_bus()

    active = adapters.get("ollama", mock)
    orchestrator = Orchestrator(
        adapter=active,
        router=router,
        tools=tools,
        permissions=permissions,
        bus=bus,
        context_builder=context_builder,
        max_steps=cfg.max_steps_per_run,
        default_model=cfg.ollama_default_model if active is not mock else "mock-nyxora",
    )

    container = Container(
        settings=cfg,
        engine=engine,
        registry=registry,
        router=router,
        permissions=permissions,
        guard=guard,
        undo=undo,
        audit=audit,
        tools=tools,
        bus=bus,
        orchestrator=orchestrator,
        agents=AgentManager(permissions),
        voice=VoiceService(bus),
        context_builder=context_builder,
        adapters=adapters,
    )
    return container


def _templates_root() -> Path:
    # deps.py sits at apps/api/app/, so parents[3] is the repository root where
    # templates/ lives. Relative entries are tried first for a CWD-local set.
    for candidate in (
        Path("templates"),
        Path(__file__).resolve().parents[3] / "templates",
    ):
        if candidate.is_dir():
            return candidate.resolve()
    return Path("templates").resolve()


def _prompts_dir() -> Path:
    return Path(__file__).resolve().parent / "prompts"
