"""Fake LLM for ai-service tests.

The ONLY way tests/ai fakes the LLM for `chat_turn` — never the anthropic SDK
directly (that is reserved for tests/llm) and never `ai.service`'s own
`RN_LLM_STUB=1` production stub (that one is exercised via env var by router/
e2e tests, not constructed directly here). `FakeChatLLM` implements just the
`.chat()` surface `chat_turn` actually calls.
"""

from __future__ import annotations

from typing import Any


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
