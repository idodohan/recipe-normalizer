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
    """Compute the USD cost of a call from the static table.

    This is the fallback path only — OpenRouter responses carry exact cost
    accounting (``usage.cost_usd``) which LLMClient prefers. Here:
    - ``:free`` models cost $0 by definition.
    - Any other unknown model — including a *paid* OpenRouter slug whose
      response omitted ``usage.cost`` — falls back to the conservative opus
      price. Over-reporting a cheap model is a safe failure (it trips the cost
      cap early); silently recording $0 for a real paid model would let spend
      run unbounded, so we do NOT zero-rate unknown slugs just because they
      contain "/".
    """
    prices = PRICES_PER_MTOK.get(model)
    if prices is None:
        # ":free" variants and the "openrouter/free" auto-router are free.
        if model.endswith(":free") or model == "openrouter/free":
            return 0.0
        logger.warning(
            "no pricing for model %r — falling back to %s pricing", model, _FALLBACK_MODEL
        )
        prices = PRICES_PER_MTOK[_FALLBACK_MODEL]
    input_price, output_price = prices
    return (input_tokens / 1_000_000) * input_price + (output_tokens / 1_000_000) * output_price
