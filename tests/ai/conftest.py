import pytest


@pytest.fixture(autouse=True)
def pin_anthropic_provider_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin provider + model settings so the suite is independent of local env keys.

    The tests here that drive a real ``LLMClient`` do so with a ``StubAnthropicClient``
    and assert against opus/haiku pricing (e.g. the cost-cap abort test). Without this,
    provider resolution falls through to ``openrouter/free`` ($0), the cost cap never
    trips, and the anthropic-shaped stub is asked for one more turn than it has scripted.
    """
    from recipe_normalizer.config import settings

    monkeypatch.setattr(settings, "llm_provider", "anthropic")
    monkeypatch.setattr(settings, "llm_model", "claude-opus-4-8")
    monkeypatch.setattr(settings, "llm_fast_model", "claude-haiku-4-5")
