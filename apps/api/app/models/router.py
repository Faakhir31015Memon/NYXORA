"""Universal Intelligence Router.

Deterministic and explainable: the same inputs always produce the same decision,
and every decision carries a human-readable reason plus the alternatives that
were rejected. This is deliberate - an opaque router cannot be audited.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

from app.models.base import Capability, ModelInfo, ProviderKind, TaskType
from app.models.registry import ModelRegistry

logger = logging.getLogger("nyxora.router")


class RoutingInput(BaseModel):
    task_type: TaskType = TaskType.CHAT
    complexity: Literal["simple", "moderate", "complex"] = "simple"
    needs_tools: bool = False
    needs_vision: bool = False
    needs_long_context: bool = False
    needs_embedding: bool = False
    privacy_level: str = "local_only"
    prompt_chars: int = 0
    user_preferred_model: str | None = None


class RoutingAlternative(BaseModel):
    model: str
    provider: str
    score: float


class RoutingDecision(BaseModel):
    model: str | None
    provider: str | None
    reason: str
    score: float = 0.0
    alternatives: list[RoutingAlternative] = Field(default_factory=list)
    fallback_chain: list[str] = Field(default_factory=list)
    estimated_cost: float = 0.0
    privacy_local: bool = True
    setup_hint: str | None = None


class RouterWeights(BaseModel):
    capability: float = 0.40
    context: float = 0.20
    speed: float = 0.15
    preference: float = 0.15
    cost: float = 0.10
    recency_penalty: float = 0.05


def weights_from_yaml(path: Path) -> RouterWeights | None:
    """Blueprint 6.6 keeps scoring weights in config, not in code.

    Unknown keys are ignored rather than trusted, so a typo in models.yaml
    cannot silently change routing.
    """
    if not path.exists():
        return None
    raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    scoring = raw.get("scoring")
    if not isinstance(scoring, dict):
        return None
    known = {k: v for k, v in scoring.items() if k in RouterWeights.model_fields}
    if not known:
        return None
    return RouterWeights(**known)


CODER_RE = re.compile(
    r"```|\bdef \w+\(|\bclass \w+|function |\bimport \w+|error|traceback"
    r"|pytest|typescript|\bfn \w+\(",
    re.IGNORECASE,
)


def _infer_tags_from_prompt(prompt: str) -> set[Capability]:
    """Cheap rule layer so routing stays snappy on the chat fast path."""
    tags: set[Capability] = set()
    if CODER_RE.search(prompt or ""):
        tags.add(Capability.CODING)
    if len(prompt or "") > 12000:
        tags.add(Capability.LONG_CONTEXT)
    return tags


def hard_requirements(routing_input: RoutingInput) -> set[Capability]:
    """Capabilities a model physically cannot work around.

    Kept deliberately small. NYXORA executes tools itself, so TOOL_USE is a
    preference, not a gate - and refusing to answer a coding question because no
    coder-tagged model is installed would be a worse product.
    """
    required: set[Capability] = {Capability.CHAT}
    if routing_input.needs_vision:
        required.add(Capability.VISION)
    if routing_input.needs_embedding:
        required.add(Capability.EMBEDDING)
    return required


def preferred_capabilities(routing_input: RoutingInput) -> set[Capability]:
    """Capabilities that raise a model's score but never exclude one."""
    preferred: set[Capability] = {Capability.CHAT}
    if routing_input.task_type is TaskType.CODING:
        preferred.add(Capability.CODING)
    if routing_input.task_type is TaskType.RESEARCH or routing_input.complexity == "complex":
        preferred.add(Capability.REASONING)
    if routing_input.needs_tools:
        preferred.add(Capability.TOOL_USE)
    if routing_input.needs_long_context:
        preferred.add(Capability.LONG_CONTEXT)
    return preferred


# Back-compatible name for callers that want the full picture.
def required_capabilities(routing_input: RoutingInput) -> set[Capability]:
    return hard_requirements(routing_input) | preferred_capabilities(routing_input)


class ModelRouter:
    def __init__(self, registry: ModelRegistry, weights: RouterWeights | None = None) -> None:
        self.registry = registry
        self.weights = weights or RouterWeights()

    def _capability_fit(
        self, model: ModelInfo, hard: set[Capability], preferred: set[Capability]
    ) -> float:
        if not hard.issubset(model.capabilities.tags):
            return -1.0
        matched = len(preferred & model.capabilities.tags)
        total = max(1, len(preferred))
        return min(1.0, 0.5 + 0.5 * (matched / total))

    def _context_fit(self, model: ModelInfo, prompt_chars: int) -> float:
        est_tokens = max(1, prompt_chars // 4)
        window = max(1, model.capabilities.context_window)
        if est_tokens > window:
            return 0.0
        ratio = est_tokens / window
        return max(0.0, 1.0 - ratio)

    def _speed_fit(self, model: ModelInfo, complexity: str) -> float:
        size_gb = model.ram_hint_gb if model.ram_hint_gb is not None else _estimate_ram_gb(model)
        if complexity == "simple":
            return max(0.0, min(1.0, 1.0 - (size_gb - 2.0) / 20.0))
        return max(0.0, min(1.0, size_gb / 8.0))

    def _cost(self, model: ModelInfo) -> float:
        if model.provider_kind is ProviderKind.LOCAL:
            return 0.0
        if model.provider_kind is ProviderKind.MOCK:
            return 0.0
        return 1.0

    def route(self, routing_input: RoutingInput) -> RoutingDecision:
        hard = hard_requirements(routing_input)
        preferred = preferred_capabilities(routing_input)
        candidates = [m for m in self.registry.all() if hard.issubset(m.capabilities.tags)]

        # Hard privacy rule: local_only excludes every external provider.
        if routing_input.privacy_level == "local_only":
            candidates = [
                m for m in candidates if m.provider_kind in (ProviderKind.LOCAL, ProviderKind.MOCK)
            ]

        if not candidates:
            return self._no_model_decision(routing_input, hard)

        scored: list[tuple[float, ModelInfo]] = []
        for model in candidates:
            cap = self._capability_fit(model, hard, preferred)
            if cap < 0:
                continue
            pref = 1.0 if routing_input.user_preferred_model == model.name else 0.0
            score = (
                self.weights.capability * cap
                + self.weights.context * self._context_fit(model, routing_input.prompt_chars)
                + self.weights.speed * self._speed_fit(model, routing_input.complexity)
                + self.weights.preference * pref
                - self.weights.cost * self._cost(model)
            )
            scored.append((round(score, 4), model))

        if not scored:
            return self._no_model_decision(routing_input, hard)

        # Deterministic tie-break: score desc, then (provider, name) asc.
        scored.sort(key=lambda t: (-t[0], t[1].provider, t[1].name))
        best_score, best = scored[0]
        chain = [f"{m.provider}:{m.name}" for _, m in scored[:4]]
        alternatives = [
            RoutingAlternative(model=m.name, provider=m.provider, score=s) for s, m in scored[1:4]
        ]
        return RoutingDecision(
            model=best.name,
            provider=best.provider,
            reason=self._reason(best, routing_input, hard, preferred, best_score, len(scored)),
            score=best_score,
            alternatives=alternatives,
            fallback_chain=chain,
            estimated_cost=self._cost(best) * 0.0,
            privacy_local=best.provider_kind in (ProviderKind.LOCAL, ProviderKind.MOCK),
        )

    def _reason(
        self,
        model: ModelInfo,
        ri: RoutingInput,
        hard: set[Capability],
        preferred: set[Capability],
        score: float,
        n: int,
    ) -> str:
        bits: list[str] = [f"selected {model.provider}:{model.name} (score {score:.2f})"]
        wanted = ", ".join(sorted(c.value for c in preferred - {Capability.CHAT}))
        if wanted:
            have = sorted(c.value for c in preferred & model.capabilities.tags - {Capability.CHAT})
            bits.append(f"wanted {wanted}; model has {', '.join(have) if have else 'none of them'}")
        missing_hard = sorted(c.value for c in hard - model.capabilities.tags)
        if missing_hard:
            bits.append(f"missing required {', '.join(missing_hard)}")
        if ri.task_type is TaskType.CODING and Capability.CODING in model.capabilities.tags:
            bits.append("coding task routed to a coder-tagged model")
        if ri.task_type is TaskType.CODING and Capability.CODING not in model.capabilities.tags:
            bits.append("no coder-tagged model installed; using the best available")
        if ri.complexity == "simple":
            bits.append("simple task, lightest capable model preferred")
        elif ri.complexity == "complex":
            bits.append("complex task, strongest available reasoning model")
        if ri.privacy_level == "local_only":
            bits.append("external providers excluded by local_only privacy rule")
        if n > 1:
            bits.append(f"{n - 1} alternative(s) available as fallback")
        return "; ".join(bits)

    def _no_model_decision(self, ri: RoutingInput, required: set[Capability]) -> RoutingDecision:
        missing = ", ".join(sorted(c.value for c in required)) or "chat"
        return RoutingDecision(
            model=None,
            provider=None,
            reason=f"no installed model satisfies: {missing}",
            privacy_local=ri.privacy_level == "local_only",
            setup_hint=(
                "ollama pull qwen2.5:3b  # or open the app's Models panel to copy a command"
            ),
        )


def _estimate_ram_gb(model: ModelInfo) -> float:
    if model.size_bytes:
        return round(model.size_bytes / 1_073_741_824, 2)
    match = re.search(r"(\d+(?:\.\d+)?)b", model.name.lower())
    if match:
        return round(float(match.group(1)) * 0.6, 2)
    return 4.0
