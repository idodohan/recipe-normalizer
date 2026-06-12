"""Scripted stand-ins for the anthropic SDK client.

This is the ONLY place in the codebase where the anthropic SDK gets stubbed —
everything outside tests/llm stubs LLMClient itself.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from pydantic import BaseModel, ValidationError


def usage(input_tokens: int = 1000, output_tokens: int = 500) -> SimpleNamespace:
    return SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens)


def parsed_response(
    parsed_output: Any,
    *,
    input_tokens: int = 1000,
    output_tokens: int = 500,
    stop_reason: str = "end_turn",
) -> SimpleNamespace:
    """A successful messages.parse response."""
    return SimpleNamespace(
        parsed_output=parsed_output,
        usage=usage(input_tokens, output_tokens),
        stop_reason=stop_reason,
        content=[],
    )


class FailingParsedResponse:
    """A messages.parse response whose output validation fails.

    The API call itself succeeded (usage is available and must be recorded);
    reading .parsed_output raises the scripted validation error.
    """

    def __init__(
        self, error: Exception, *, input_tokens: int = 1000, output_tokens: int = 500
    ) -> None:
        self.usage = usage(input_tokens, output_tokens)
        self.stop_reason = "end_turn"
        self.content: list[Any] = []
        self._error = error

    @property
    def parsed_output(self) -> Any:
        raise self._error


def make_validation_error() -> ValidationError:
    """Produce a real pydantic ValidationError instance."""

    class _Strict(BaseModel):
        required_field: int

    try:
        _Strict.model_validate({})
    except ValidationError as exc:
        return exc
    raise AssertionError("expected validation to fail")


def message_response(
    content: list[Any],
    stop_reason: str,
    *,
    input_tokens: int = 1000,
    output_tokens: int = 500,
) -> SimpleNamespace:
    """A messages.create response (Message-shaped)."""
    return SimpleNamespace(
        content=content, stop_reason=stop_reason, usage=usage(input_tokens, output_tokens)
    )


def tool_use_block(
    block_id: str = "toolu_1", name: str = "lookup", input: dict[str, Any] | None = None
) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", id=block_id, name=name, input=input or {})


def text_block(text: str = "done") -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


class _StubMessages:
    def __init__(self, parse_results: list[Any], create_results: list[Any]) -> None:
        self._parse_results = list(parse_results)
        self._create_results = list(create_results)
        self.parse_calls: list[dict[str, Any]] = []
        self.create_calls: list[dict[str, Any]] = []

    def parse(self, **kwargs: Any) -> Any:
        self.parse_calls.append(kwargs)
        result = self._parse_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def create(self, **kwargs: Any) -> Any:
        self.create_calls.append(kwargs)
        result = self._create_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class StubAnthropicClient:
    """Drop-in for anthropic.Anthropic with scripted responses, consumed in order."""

    def __init__(
        self,
        *,
        parse_results: list[Any] | None = None,
        create_results: list[Any] | None = None,
    ) -> None:
        self.messages = _StubMessages(parse_results or [], create_results or [])


class RecorderSpy:
    """UsageRecorder implementation that captures record() calls."""

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def record(
        self, *, feature: str, model: str, input_tokens: int, output_tokens: int, cost: float
    ) -> None:
        self.records.append(
            {
                "feature": feature,
                "model": model,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cost": cost,
            }
        )


class ExplodingRecorder:
    """UsageRecorder whose record() always raises."""

    def record(
        self, *, feature: str, model: str, input_tokens: int, output_tokens: int, cost: float
    ) -> None:
        raise RuntimeError("recorder is down")
