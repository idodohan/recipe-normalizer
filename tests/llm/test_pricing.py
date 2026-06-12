import logging

import pytest

from recipe_normalizer.llm.pricing import PRICES_PER_MTOK, cost_usd


def test_prices_table_has_exact_models() -> None:
    assert PRICES_PER_MTOK["claude-opus-4-8"] == (5.0, 25.0)
    assert PRICES_PER_MTOK["claude-haiku-4-5"] == (1.0, 5.0)


def test_cost_usd_opus() -> None:
    # 1000 in / 500 out on opus: 0.005 + 0.0125 = 0.0175
    assert cost_usd("claude-opus-4-8", 1000, 500) == pytest.approx(0.0175)
    assert cost_usd("claude-opus-4-8", 1_000_000, 1_000_000) == pytest.approx(30.0)


def test_cost_usd_haiku() -> None:
    assert cost_usd("claude-haiku-4-5", 1_000_000, 1_000_000) == pytest.approx(6.0)
    assert cost_usd("claude-haiku-4-5", 1000, 500) == pytest.approx(0.0035)


def test_cost_usd_zero_tokens() -> None:
    assert cost_usd("claude-opus-4-8", 0, 0) == 0.0


def test_unknown_model_falls_back_to_opus_pricing_with_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="recipe_normalizer.llm.pricing"):
        cost = cost_usd("mystery-model-9", 1000, 500)
    assert cost == pytest.approx(0.0175)
    assert any("mystery-model-9" in record.getMessage() for record in caplog.records)
