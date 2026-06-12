"""Live round-trip against the real Anthropic API.

Opt in with: RN_LIVE_LLM_TESTS=1 uv run pytest tests/llm/test_live_smoke.py
Requires ANTHROPIC_API_KEY in the environment.
"""

import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("RN_LIVE_LLM_TESTS"),
    reason="set RN_LIVE_LLM_TESTS=1 to run live LLM smoke tests",
)


def test_classify_bool_live_round_trip_on_haiku() -> None:
    from recipe_normalizer.llm.client import LLMClient

    client = LLMClient()
    answer = client.classify_bool(
        feature="live-smoke",
        question="Does the content describe water as wet?",
        content="Water is a liquid. When it touches things, it makes them wet.",
    )
    assert answer is True
    assert client.spent_usd > 0
