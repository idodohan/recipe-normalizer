"""LLMClient — the single seam through which every LLM call in the product flows.

Model ids, retries, timeouts, structured output, vision, tool loops, and
token/cost logging all live here. Everything outside this package talks to
LLMClient; only tests in tests/llm stub the provider client directly.

Providers (RN_LLM_PROVIDER): "openrouter" (default when OPENROUTER_API_KEY is
set — free-tier friendly), "anthropic" (ANTHROPIC_API_KEY), or "auto" (pick
from whichever key is present, preferring OpenRouter). Both providers speak
the same anthropic-shaped ``messages.create`` protocol; see
:mod:`recipe_normalizer.llm.openrouter` for the adapter.
"""

from __future__ import annotations

import base64
import logging
import os
import uuid
from collections.abc import Callable
from decimal import Decimal
from functools import lru_cache
from typing import Any, Protocol, TypeVar, cast

from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from recipe_normalizer.config import settings
from recipe_normalizer.llm.models import LlmUsage
from recipe_normalizer.llm.pricing import cost_usd
from recipe_normalizer.llm.schema import strict_json_schema

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class LLMError(Exception):
    """Base error for LLM-call failures (refusals, truncation, bad output)."""


class CostCapExceeded(LLMError):
    """Raised before an API call when the configured cost cap has been reached."""


class BudgetExceeded(LLMError):
    """Raised when a tool loop runs past its iteration budget."""

    def __init__(self, message: str, last_response: Any | None = None) -> None:
        super().__init__(message)
        self.last_response = last_response


class UsageRecorder(Protocol):
    def record(
        self, *, feature: str, model: str, input_tokens: int, output_tokens: int, cost: float
    ) -> None: ...


class DbUsageRecorder:
    """Writes LlmUsage rows; constructed with (session, user_id=None, job_id=None); flush-only."""

    def __init__(
        self,
        session: Session,
        user_id: uuid.UUID | None = None,
        job_id: uuid.UUID | None = None,
    ) -> None:
        self._session = session
        self._user_id = user_id
        self._job_id = job_id

    def record(
        self, *, feature: str, model: str, input_tokens: int, output_tokens: int, cost: float
    ) -> None:
        self._session.add(
            LlmUsage(
                feature=feature,
                user_id=self._user_id,
                job_id=self._job_id,
                model=model[:200],  # column bound; never poison the session over a long slug
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=Decimal(str(round(cost, 6))),
            )
        )
        self._session.flush()


def image_block(data: bytes, media_type: str) -> dict[str, Any]:
    """Build a base64 image content block for vision requests."""
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": base64.standard_b64encode(data).decode("ascii"),
        },
    }


class _Bool(BaseModel):
    answer: bool


_REPAIR_TEMPLATE = (
    "Your previous output failed validation: {error}. "
    "Your previous output was:\n{output}\n"
    "Return ONLY corrected JSON matching the schema."
)

#: (default model, fast model) per provider when RN_LLM_MODEL / RN_LLM_FAST_MODEL are unset.
#: ``openrouter/free`` is OpenRouter's auto-router over currently-free models
#: (vision + tools + json_schema) — rotation-proof as the free lineup changes.
_PROVIDER_DEFAULT_MODELS: dict[str, tuple[str, str]] = {
    "anthropic": ("claude-opus-4-8", "claude-haiku-4-5"),
    "openrouter": ("openrouter/free", "openrouter/free"),
}


def resolve_provider() -> str:
    """Resolve the configured provider; "auto" prefers OpenRouter when its key is set."""
    provider = settings.llm_provider
    if provider not in ("auto", "anthropic", "openrouter"):
        raise ValueError(
            f"unknown RN_LLM_PROVIDER: {provider!r} (expected auto, anthropic, or openrouter)"
        )
    if provider != "auto":
        return provider
    if os.environ.get("OPENROUTER_API_KEY"):
        return "openrouter"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    return "openrouter"


@lru_cache(maxsize=8)
def _provider_client_cached(provider: str, api_key: str) -> Any:
    """One shared client (and connection pool) per (provider, api_key) per process.

    LLMClient is constructed per request/per job; without memoization each
    one would open a fresh httpx pool that is never closed (fd leak) and pay
    TLS handshakes on every call. Mirrors filestore._make_store.

    Keyed on the api_key too so an in-process key rotation yields a fresh
    client instead of reusing one that now 401s.
    """
    if provider == "anthropic":
        import anthropic

        return anthropic.Anthropic(
            max_retries=settings.llm_max_retries,
            timeout=settings.llm_timeout_s,
        )
    from recipe_normalizer.llm.openrouter import OpenRouterClient

    return OpenRouterClient(
        api_key=api_key,
        max_retries=settings.llm_max_retries,
        timeout=settings.llm_timeout_s,
    )


def _provider_client(provider: str) -> Any:
    api_key = os.environ.get("OPENROUTER_API_KEY", "") if provider == "openrouter" else ""
    return _provider_client_cached(provider, api_key)


class LLMClient:
    def __init__(
        self,
        *,
        recorder: UsageRecorder | None = None,
        cost_cap_usd: float | None = None,
        chat_client: Any | None = None,
        already_spent_usd: float = 0.0,
    ) -> None:
        self._recorder = recorder
        self._cost_cap_usd = cost_cap_usd
        self._chat_client = chat_client
        self._spent_usd = already_spent_usd

    @property
    def spent_usd(self) -> float:
        return self._spent_usd

    # -- internals -------------------------------------------------------------

    def _client(self) -> Any:
        """Return the injected client, or the process-shared provider client."""
        if self._chat_client is None:
            self._chat_client = _provider_client(resolve_provider())
        return self._chat_client

    def model_for(self, *, fast: bool = False) -> str:
        """The model id calls will actually use — for provenance/metadata.

        Callers must record this rather than reading ``settings.llm_model``
        directly, which is empty when provider defaults are in play.
        """
        return self._resolve_model(None, fast)

    def _resolve_model(self, model: str | None, fast: bool) -> str:
        if model is not None:
            return model
        configured = settings.llm_fast_model if fast else settings.llm_model
        if configured:
            return configured
        defaults = _PROVIDER_DEFAULT_MODELS[resolve_provider()]
        return defaults[1] if fast else defaults[0]

    def _check_cost_cap(self) -> None:
        if self._cost_cap_usd is not None and self._spent_usd >= self._cost_cap_usd:
            raise CostCapExceeded(
                f"spent ${self._spent_usd:.6f} >= cost cap ${self._cost_cap_usd:.6f}"
            )

    def _record_usage(self, feature: str, model: str, usage: Any) -> None:
        # Prefer the provider's own cost accounting (OpenRouter reports exact
        # spend, $0 for :free models); fall back to the static pricing table.
        cost = getattr(usage, "cost_usd", None)
        if cost is None:
            cost = cost_usd(model, usage.input_tokens, usage.output_tokens)
        self._spent_usd += cost
        if self._recorder is None:
            return
        try:
            self._recorder.record(
                feature=feature,
                model=model,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cost=cost,
            )
        except Exception:
            logger.exception("usage recorder failed (feature=%s, model=%s)", feature, model)

    # -- public surface ----------------------------------------------------------

    def structured(
        self,
        *,
        feature: str,
        output_model: type[T],
        content: str | list[dict[str, Any]],
        system: str | None = None,
        model: str | None = None,
        fast: bool = False,
        max_tokens: int = 8000,
    ) -> T:
        """Request schema-constrained output and return a validated instance.

        Uses messages.create with output_config json_schema (NOT messages.parse:
        the SDK validates eagerly inside parse(), which would raise before the
        response usage is readable — failed attempts would go unbilled and the
        cost cap would under-enforce). Validation runs here, after usage is
        recorded and after the refusal check. On validation failure, retries
        ONCE with the failed output appended as an extra user message; a second
        failure raises LLMError.
        """
        resolved = self._resolve_model(model, fast)
        schema = strict_json_schema(output_model)
        messages: list[dict[str, Any]] = [{"role": "user", "content": content}]
        last_error: ValidationError | None = None

        for _attempt in range(2):
            self._check_cost_cap()
            kwargs: dict[str, Any] = {
                "model": resolved,
                "max_tokens": max_tokens,
                "messages": list(messages),
                "output_config": {"format": {"type": "json_schema", "schema": schema}},
            }
            if system is not None:
                kwargs["system"] = system
            response = self._client().messages.create(**kwargs)
            self._record_usage(feature, resolved, response.usage)

            if response.stop_reason == "refusal":
                raise LLMError("model refused")
            text = next((block.text for block in response.content if block.type == "text"), None)
            if text is None:
                raise LLMError("no text block in structured output response")
            try:
                return output_model.model_validate_json(text)
            except ValidationError as exc:
                last_error = exc
                messages.append(
                    {"role": "user", "content": _REPAIR_TEMPLATE.format(error=exc, output=text)}
                )

        raise LLMError(
            f"structured output failed validation after repair retry: {last_error}"
        ) from last_error

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
        """Free-form conversational call: send *messages* + *system*, return the reply text.

        Unlike `structured`, no output schema is enforced — this is the seam
        for chat/Q&A features (ai.recipe_chat, ai.cookbook_qa) where the
        response is prose, not a validated model. A single request/response
        turn only (the caller is responsible for building `messages` — prior
        turns plus the new one — and for persisting the result); this method
        does not loop or retry.

        Checks the cost cap before calling (consistent with `structured` and
        `tool_loop`), records usage after the call, and raises `LLMError` if
        the model issues an SDK-level refusal or the response has no text
        block. A model saying "I don't know" in ordinary prose is NOT an
        error — that text is returned normally like any other answer.
        """
        resolved = self._resolve_model(model, fast)
        self._check_cost_cap()
        response = self._client().messages.create(
            model=resolved,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
        )
        self._record_usage(feature, resolved, response.usage)

        if response.stop_reason == "refusal":
            raise LLMError("model refused")
        text = next((block.text for block in response.content if block.type == "text"), None)
        if text is None:
            raise LLMError("no text block in chat response")
        return cast(str, text)

    def classify_bool(
        self,
        *,
        feature: str,
        question: str,
        content: str,
        max_chars: int = 30000,
    ) -> bool:
        """Fast yes/no classification on the cheap model."""
        if len(content) > max_chars:
            content = content[:max_chars] + " (truncated)"
        result = self.structured(
            feature=feature,
            output_model=_Bool,
            content=f"Question: {question}\n\nContent:\n{content}",
            system="Answer the question about the provided content.",
            fast=True,
        )
        return result.answer

    def tool_loop(
        self,
        *,
        feature: str,
        system: str,
        tools: list[dict[str, Any]],
        initial_content: list[dict[str, Any]],
        execute: Callable[[str, dict[str, Any]], str | list[dict[str, Any]]],
        max_iterations: int = 15,
        max_tokens: int = 8000,
    ) -> Any:
        """Run a manual tool-use loop; returns the final Message at end_turn."""
        model = self._resolve_model(None, fast=False)
        messages: list[dict[str, Any]] = [{"role": "user", "content": initial_content}]
        response: Any = None

        for _iteration in range(max_iterations):
            self._check_cost_cap()
            response = self._client().messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                tools=tools,
                messages=messages,
            )
            self._record_usage(feature, model, response.usage)

            stop_reason = response.stop_reason
            if stop_reason == "end_turn":
                return response
            if stop_reason == "refusal":
                raise LLMError("model refused")
            if stop_reason == "max_tokens":
                raise LLMError("output truncated")
            if stop_reason != "tool_use":
                raise LLMError(f"unexpected stop_reason: {stop_reason!r}")

            messages.append({"role": "assistant", "content": response.content})
            tool_results: list[dict[str, Any]] = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                try:
                    result = execute(block.name, block.input)
                    is_error = False
                except Exception as exc:
                    result = str(exc)
                    is_error = True
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                        "is_error": is_error,
                    }
                )
            messages.append({"role": "user", "content": tool_results})

        raise BudgetExceeded(
            f"tool loop exceeded {max_iterations} iterations", last_response=response
        )
