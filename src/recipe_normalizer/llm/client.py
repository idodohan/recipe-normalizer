"""LLMClient — the single seam through which every LLM call in the product flows.

Model ids, retries, timeouts, structured output, vision, tool loops, and
token/cost logging all live here. Everything outside this package talks to
LLMClient; only tests in tests/llm stub the anthropic SDK directly.
"""

from __future__ import annotations

import base64
import logging
import uuid
from collections.abc import Callable
from decimal import Decimal
from typing import Any, Protocol, TypeVar

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
                model=model,
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


class LLMClient:
    def __init__(
        self,
        *,
        recorder: UsageRecorder | None = None,
        cost_cap_usd: float | None = None,
        anthropic_client: Any | None = None,
        already_spent_usd: float = 0.0,
    ) -> None:
        self._recorder = recorder
        self._cost_cap_usd = cost_cap_usd
        self._anthropic_client = anthropic_client
        self._spent_usd = already_spent_usd

    @property
    def spent_usd(self) -> float:
        return self._spent_usd

    # -- internals -------------------------------------------------------------

    def _client(self) -> Any:
        """Return the injected client, or lazily construct the real one."""
        if self._anthropic_client is None:
            import anthropic

            self._anthropic_client = anthropic.Anthropic(
                max_retries=settings.llm_max_retries,
                timeout=settings.llm_timeout_s,
            )
        return self._anthropic_client

    def _resolve_model(self, model: str | None, fast: bool) -> str:
        if model is not None:
            return model
        return settings.llm_fast_model if fast else settings.llm_model

    def _check_cost_cap(self) -> None:
        if self._cost_cap_usd is not None and self._spent_usd >= self._cost_cap_usd:
            raise CostCapExceeded(
                f"spent ${self._spent_usd:.6f} >= cost cap ${self._cost_cap_usd:.6f}"
            )

    def _record_usage(self, feature: str, model: str, usage: Any) -> None:
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
        model = settings.llm_model
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
