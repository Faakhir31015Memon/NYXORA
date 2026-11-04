"""Model registry: catalogue + YAML overrides + live availability from adapters."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from app.models.base import Capability, ModelAdapter, ModelCapabilities, ModelInfo, ProviderKind

logger = logging.getLogger("nyxora.registry")


class ModelRegistry:
    def __init__(self) -> None:
        self._models: dict[tuple[str, str], ModelInfo] = {}
        self._adapters: dict[str, ModelAdapter] = {}

    def register_adapter(self, adapter: ModelAdapter) -> None:
        self._adapters[adapter.provider] = adapter

    @property
    def adapters(self) -> dict[str, ModelAdapter]:
        return dict(self._adapters)

    def adapter(self, provider: str) -> ModelAdapter | None:
        return self._adapters.get(provider)

    def upsert(self, info: ModelInfo) -> None:
        self._models[(info.provider, info.name)] = info

    def all(self) -> list[ModelInfo]:
        return [m for m in self._models.values() if m.enabled]

    def get(self, provider: str, name: str) -> ModelInfo | None:
        return self._models.get((provider, name))

    def clear_provider(self, provider: str) -> None:
        for key in [k for k in self._models if k[0] == provider]:
            del self._models[key]

    async def refresh(self) -> list[ModelInfo]:
        """Query every adapter for what actually exists. Never assume."""
        discovered: list[ModelInfo] = []
        for provider, adapter in list(self._adapters.items()):
            health = await adapter.health()
            if not health.ok:
                logger.info("adapter_unavailable", extra={"provider": provider})
                self.clear_provider(provider)
                continue
            try:
                models = await adapter.list_models()
            except Exception:  # noqa: BLE001
                logger.exception("adapter_list_failed", extra={"provider": provider})
                continue
            self.clear_provider(provider)
            for info in models:
                self.upsert(info)
                discovered.append(info)
        return discovered

    def load_yaml_overrides(self, path: Path) -> int:
        """User-editable catalogue. Overrides capabilities/license/enabled flags."""
        if not path.exists():
            return 0
        raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        count = 0
        for entry in raw.get("models", []):
            name = entry.get("name")
            if not name:
                continue
            provider = str(entry.get("provider", "ollama"))
            key = (provider, str(name))
            existing = self._models.get(key)
            tags = (
                {
                    Capability(t)
                    for t in entry.get("capabilities", [])
                    if t in Capability.__members__.values()
                }
                if entry.get("capabilities")
                else set()
            )
            if existing is not None and tags:
                existing.capabilities.tags |= tags
            if existing is not None:
                if entry.get("license"):
                    existing.license = str(entry["license"])
                if entry.get("ram_hint_gb") is not None:
                    existing.ram_hint_gb = float(entry["ram_hint_gb"])
                if "enabled" in entry:
                    existing.enabled = bool(entry["enabled"])
                if entry.get("context_window"):
                    existing.capabilities.context_window = int(entry["context_window"])
                count += 1
                continue
            self.upsert(
                ModelInfo(
                    provider=provider,
                    name=str(name),
                    provider_kind=ProviderKind(entry.get("provider_kind", "local")),
                    capabilities=ModelCapabilities(
                        tags=tags or {Capability.CHAT},
                        context_window=int(entry.get("context_window", 4096)),
                    ),
                    ram_hint_gb=entry.get("ram_hint_gb"),
                    license=entry.get("license"),
                    enabled=bool(entry.get("enabled", True)),
                )
            )
            count += 1
        return count
