"""Per-model token pricing and cost computation."""

import logging

logger = logging.getLogger(__name__)

# model id -> (input $/MTok, output $/MTok)
PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-opus-4-8": (5.0, 25.0),
    "claude-haiku-4-5": (1.0, 5.0),
}

_FALLBACK_MODEL = "claude-opus-4-8"


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    """Compute the USD cost of a call. Unknown models fall back to opus pricing."""
    prices = PRICES_PER_MTOK.get(model)
    if prices is None:
        logger.warning(
            "no pricing for model %r — falling back to %s pricing", model, _FALLBACK_MODEL
        )
        prices = PRICES_PER_MTOK[_FALLBACK_MODEL]
    input_price, output_price = prices
    return (input_tokens / 1_000_000) * input_price + (output_tokens / 1_000_000) * output_price
