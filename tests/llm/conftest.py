from collections.abc import Iterator
from typing import Any

import pytest

_LIVE_SMOKE_FILE = "test_live_smoke.py"


@pytest.fixture(autouse=True)
def block_real_anthropic_client(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """Structural no-network guarantee: constructing a real anthropic client fails.

    Every test in tests/llm must inject a stub via LLMClient(anthropic_client=...).
    The live smoke file opts out (it is already gated on RN_LIVE_LLM_TESTS).
    """
    if request.node.path.name == _LIVE_SMOKE_FILE:
        yield
        return

    import anthropic

    def _forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(
            "tests/llm must not construct a real anthropic.Anthropic client — "
            "inject a StubAnthropicClient via LLMClient(anthropic_client=...)"
        )

    monkeypatch.setattr(anthropic, "Anthropic", _forbidden)
    yield
