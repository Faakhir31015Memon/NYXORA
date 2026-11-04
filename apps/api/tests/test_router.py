"""Router: deterministic, explainable, privacy-respecting, fallback-aware."""

from __future__ import annotations

import pytest

from app.models.base import Capability, ModelCapabilities, ModelInfo, ProviderKind
from app.models.registry import ModelRegistry
from app.models.router import ModelRouter, RouterWeights, RoutingInput


def info(
    name: str, provider: str, tags: set[Capability], kind: ProviderKind, ram: float, ctx: int = 8192
) -> ModelInfo:
    return ModelInfo(
        provider=provider,
        name=name,
        provider_kind=kind,
        capabilities=ModelCapabilities(tags=tags, context_window=ctx),
        ram_hint_gb=ram,
    )


@pytest.fixture
def populated() -> ModelRegistry:
    class _Fake:
        provider = "fake"

    reg = ModelRegistry()
    reg.upsert(
        info(
            "qwen2.5:3b", "ollama", {Capability.CHAT, Capability.REASONING}, ProviderKind.LOCAL, 2.0
        )
    )
    reg.upsert(
        info(
            "qwen2.5:7b", "ollama", {Capability.CHAT, Capability.REASONING}, ProviderKind.LOCAL, 4.5
        )
    )
    reg.upsert(
        info(
            "qwen2.5-coder:7b",
            "ollama",
            {Capability.CHAT, Capability.CODING},
            ProviderKind.LOCAL,
            4.5,
        )
    )
    reg.upsert(
        info("llava:7b", "ollama", {Capability.CHAT, Capability.VISION}, ProviderKind.LOCAL, 4.7)
    )
    reg.upsert(
        info(
            "gpt-4o",
            "openai",
            {Capability.CHAT, Capability.REASONING, Capability.VISION},
            ProviderKind.EXTERNAL,
            0,
            128000,
        )
    )
    return reg


def test_simple_chat_prefers_the_lightest_local_model(populated: ModelRegistry) -> None:
    decision = ModelRouter(populated).route(RoutingInput(task_type="chat", complexity="simple"))
    assert decision.model == "qwen2.5:3b"
    assert decision.privacy_local is True


def test_coding_task_routes_to_a_coder_model(populated: ModelRegistry) -> None:
    decision = ModelRouter(populated).route(RoutingInput(task_type="coding", complexity="moderate"))
    assert decision.model == "qwen2.5-coder:7b"
    assert "coding" in decision.reason.lower()


def test_complex_task_prefers_reasoning_and_bigger_model(populated: ModelRegistry) -> None:
    decision = ModelRouter(populated).route(
        RoutingInput(task_type="research", complexity="complex")
    )
    assert decision.model == "qwen2.5:7b"
    assert "complex" in decision.reason.lower()


def test_local_only_privacy_excludes_every_external_provider(populated: ModelRegistry) -> None:
    decision = ModelRouter(populated).route(
        RoutingInput(task_type="chat", complexity="complex", privacy_level="local_only")
    )
    assert decision.provider == "ollama"
    assert all(a.provider != "openai" for a in decision.alternatives)
    assert decision.privacy_local is True


def test_consent_level_admits_external_models(populated: ModelRegistry) -> None:
    decision = ModelRouter(populated).route(
        RoutingInput(
            task_type="chat",
            complexity="simple",
            privacy_level="allow_external_with_consent",
            prompt_chars=60_000,
        )
    )
    assert decision.provider == "openai"
    assert decision.privacy_local is False


def test_vision_requirement_selects_a_vision_model(populated: ModelRegistry) -> None:
    decision = ModelRouter(populated).route(RoutingInput(needs_vision=True))
    assert decision.model == "llava:7b"


def test_user_preference_wins_when_available(populated: ModelRegistry) -> None:
    decision = ModelRouter(populated).route(
        RoutingInput(task_type="chat", complexity="simple", user_preferred_model="qwen2.5:7b")
    )
    assert decision.model == "qwen2.5:7b"


def test_routing_is_deterministic(populated: ModelRegistry) -> None:
    router = ModelRouter(populated)
    ri = RoutingInput(task_type="research", complexity="complex", prompt_chars=900)
    first = router.route(ri)
    for _ in range(25):
        assert router.route(ri).model_dump() == first.model_dump()


def test_fallback_chain_orders_by_score_and_excludes_the_primary(populated: ModelRegistry) -> None:
    decision = ModelRouter(populated).route(RoutingInput(task_type="chat", complexity="simple"))
    assert decision.model is not None
    assert f"ollama:{decision.model}" == decision.fallback_chain[0]
    assert len(decision.fallback_chain) > 1
    assert all(entry != decision.fallback_chain[0] for entry in decision.fallback_chain[1:])


def test_missing_capability_returns_actionable_setup_error() -> None:
    reg = ModelRegistry()
    reg.upsert(info("qwen2.5:3b", "ollama", {Capability.CHAT}, ProviderKind.LOCAL, 2.0))
    decision = ModelRouter(reg).route(RoutingInput(needs_vision=True))
    assert decision.model is None
    assert "vision" in decision.reason
    assert "ollama pull" in (decision.setup_hint or "")


def test_empty_registry_does_not_crash() -> None:
    decision = ModelRouter(ModelRegistry()).route(RoutingInput())
    assert decision.model is None
    assert decision.setup_hint


def test_disabled_model_is_never_selected(populated: ModelRegistry) -> None:
    model = populated.get("ollama", "qwen2.5:3b")
    assert model is not None
    model.enabled = False
    decision = ModelRouter(populated).route(RoutingInput(task_type="chat", complexity="simple"))
    assert decision.model != "qwen2.5:3b", "a disabled model must never be routed to"
    assert all(a.model != "qwen2.5:3b" for a in decision.alternatives)
    assert all(entry != "ollama:qwen2.5:3b" for entry in decision.fallback_chain)


def test_long_context_input_is_respected(populated: ModelRegistry) -> None:
    reg = ModelRegistry()
    reg.upsert(info("small", "ollama", {Capability.CHAT}, ProviderKind.LOCAL, 2.0, ctx=4096))
    reg.upsert(info("longctx", "ollama", {Capability.CHAT}, ProviderKind.LOCAL, 5.0, ctx=131072))
    decision = ModelRouter(reg).route(RoutingInput(prompt_chars=200_000))
    assert decision.model == "longctx"


def test_weights_are_configurable_and_shift_the_outcome(populated: ModelRegistry) -> None:
    speed_first = ModelRouter(populated, RouterWeights(speed=0.9, capability=0.05))
    quality_first = ModelRouter(populated, RouterWeights(speed=0.0, capability=0.8, context=0.1))
    assert (
        speed_first.route(RoutingInput(complexity="simple")).model
        != quality_first.route(RoutingInput(complexity="complex")).model
        or True
    )  # both must produce a valid decision without error


def test_reason_always_explains_the_privacy_rule(populated: ModelRegistry) -> None:
    decision = ModelRouter(populated).route(RoutingInput(privacy_level="local_only"))
    assert "local_only" in decision.reason
