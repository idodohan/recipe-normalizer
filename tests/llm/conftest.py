from collections.abc import Iterator
from typing import Any

import pytest

_LIVE_SMOKE_FILE = "test_live_smoke.py"


@pytest.fixture(autouse=True)
def clear_provider_client_cache() -> Iterator[None]:
    """Drop any process-shared provider client between tests.

    `_provider_client_cached` is an lru_cache; the network-block fixture only
    guards *construction*, so a cached real client (e.g. from the live-smoke
    opt-out) must not survive into the next test.
    """
    from recipe_normalizer.llm.client import _provider_client_cached

    _provider_client_cached.cache_clear()
    yield
    _provider_client_cached.cache_clear()


@pytest.fixture(autouse=True)
def pin_anthropic_provider_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin provider + model settings so tests are independent of local env keys.

    LLMClient tests were written against the anthropic wire format and opus/haiku
    pricing; provider resolution itself is covered in test_openrouter.py.
    """
    from recipe_normalizer.config import settings

    monkeypatch.setattr(settings, "llm_provider", "anthropic")
    monkeypatch.setattr(settings, "llm_model", "claude-opus-4-8")
    monkeypatch.setattr(settings, "llm_fast_model", "claude-haiku-4-5")


@pytest.fixture(autouse=True)
def block_real_chat_client(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """Structural no-network guarantee: constructing a real provider client fails.

    Every test in tests/llm must inject a stub via LLMClient(chat_client=...)
    (or, for the OpenRouter adapter, an httpx.MockTransport-backed http_client).
    The live smoke file opts out (it is already gated on RN_LIVE_LLM_TESTS).
    """
    if request.node.path.name == _LIVE_SMOKE_FILE:
        yield
        return

    import anthropic

    from recipe_normalizer.llm import openrouter

    def _forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(
            "tests/llm must not construct a real provider client — inject a "
            "StubAnthropicClient via LLMClient(chat_client=...) or an "
            "httpx.MockTransport via OpenRouterClient(http_client=...)"
        )

    monkeypatch.setattr(anthropic, "Anthropic", _forbidden)
    monkeypatch.setattr(openrouter, "_default_http_client", _forbidden)
    yield
