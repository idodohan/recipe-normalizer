"""Fake LLM for ai-service tests.

The ONLY way tests/ai fakes the LLM for `chat_turn` — never the anthropic SDK
directly (that is reserved for tests/llm) and never `ai.service`'s own
`RN_LLM_STUB=1` production stub (that one is exercised via env var by router/
e2e tests, not constructed directly here). `FakeChatLLM` implements just the
`.chat()` surface `chat_turn` actually calls.
"""

from __future__ import annotations

from typing import Any

from recipe_normalizer.extraction.normalize import NormalizeResult


class FakeChatLLM:
    """A minimal LLMClient stand-in exposing only `.chat()`.

    Construct with ``reply`` for a single scripted answer, or ``replies`` (a
    list, consumed in order) for a multi-turn script. Pass ``raises`` to make
    every call raise that exception instead (e.g. ``CostCapExceeded``) — used
    to test `chat_turn`'s error-mapping without a real cost cap or API call.

    Every call's kwargs are recorded in ``self.calls`` so tests can assert on
    the system prompt (grounding) and message history sent to the model.
    """

    def __init__(
        self,
        reply: str | None = None,
        *,
        replies: list[str] | None = None,
        raises: Exception | None = None,
    ) -> None:
        self._replies = (
            list(replies) if replies is not None else ([reply] if reply is not None else [])
        )
        self._raises = raises
        self.calls: list[dict[str, Any]] = []

    def chat(
        self,
        *,
        feature: str,
        system: str,
        messages: list[dict[str, Any]],
        max_tokens: int = 1024,
        model: str | None = None,
        fast: bool = False,
    ) -> str:
        self.calls.append(
            {
                "feature": feature,
                "system": system,
                "messages": messages,
                "max_tokens": max_tokens,
                "model": model,
                "fast": fast,
            }
        )
        if self._raises is not None:
            raise self._raises
        if not self._replies:
            raise AssertionError("FakeChatLLM.chat() called with no scripted replies left")
        return self._replies.pop(0)


class FakeStructuredLLM:
    """A minimal LLMClient stand-in exposing only `.structured()`, for `transform_recipe`.

    Construct with ``result`` (a `NormalizeResult`) for the scripted transform output, or
    ``raises`` (e.g. `CostCapExceeded`) to make the call fail instead — mirrors
    `FakeChatLLM`'s constructor shape for the same reasons.

    Also answers `persist_drafts`' catalog-matching band-choice call (an ``index`` field
    on the output model) with ``index=None`` — a "no confident match" answer, same as
    `worker._StubLLMClient` and `ai.service._StubAiLLMClient` — so a test recipe whose
    ingredient names happen to fuzzy-match something in a seeded catalog doesn't crash;
    every test in this suite uses an empty catalog, so this path is untouched in practice,
    but it's here for the same robustness reason the production stubs have it.

    Every call's kwargs are recorded in ``self.calls`` so tests can assert the system
    prompt/content passed to the model, or (for the scaling-boundary tests) assert this
    was NEVER called at all.
    """

    def __init__(
        self,
        result: NormalizeResult | None = None,
        *,
        raises: Exception | None = None,
        spent_usd: float = 0.02,
    ) -> None:
        self._result = result
        self._raises = raises
        self._spent_usd = spent_usd
        self.calls: list[dict[str, Any]] = []

    @property
    def spent_usd(self) -> float:
        return self._spent_usd

    def structured(
        self,
        *,
        feature: str,
        output_model: type[Any],
        content: str | list[dict[str, Any]],
        system: str | None = None,
        model: str | None = None,
        fast: bool = False,
        max_tokens: int = 8000,
    ) -> Any:
        self.calls.append(
            {
                "feature": feature,
                "output_model": output_model,
                "content": content,
                "system": system,
                "max_tokens": max_tokens,
            }
        )
        if self._raises is not None:
            raise self._raises
        if output_model is NormalizeResult:
            if self._result is None:
                raise AssertionError(
                    "FakeStructuredLLM.structured() called with no scripted result"
                )
            return self._result
        if "index" in output_model.model_fields:  # catalog.match band choice
            return output_model(index=None)
        raise NotImplementedError(f"FakeStructuredLLM cannot answer feature {feature!r}")
