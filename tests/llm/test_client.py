import base64
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel

from recipe_normalizer.config import settings
from recipe_normalizer.llm.client import (
    BudgetExceeded,
    CostCapExceeded,
    LLMClient,
    LLMError,
    image_block,
)
from tests.llm.stubs import (
    ExplodingRecorder,
    FailingParsedResponse,
    RecorderSpy,
    StubAnthropicClient,
    make_validation_error,
    message_response,
    parsed_response,
    text_block,
    tool_use_block,
)


class Extraction(BaseModel):
    title: str


# --- structured ---------------------------------------------------------------


def test_structured_happy_path_returns_parsed_model_and_records_usage() -> None:
    stub = StubAnthropicClient(parse_results=[parsed_response(Extraction(title="Soup"))])
    recorder = RecorderSpy()
    client = LLMClient(recorder=recorder, anthropic_client=stub)

    result = client.structured(feature="extract", output_model=Extraction, content="some recipe")

    assert result == Extraction(title="Soup")
    [call] = stub.messages.parse_calls
    assert call["model"] == settings.llm_model
    assert call["output_format"] is Extraction
    assert call["max_tokens"] == 8000
    assert call["messages"] == [{"role": "user", "content": "some recipe"}]
    # opus, 1000 in / 500 out -> 0.005 + 0.0125 = 0.0175
    [record] = recorder.records
    assert record["feature"] == "extract"
    assert record["model"] == settings.llm_model
    assert record["input_tokens"] == 1000
    assert record["output_tokens"] == 500
    assert record["cost"] == pytest.approx(0.0175)
    assert client.spent_usd == pytest.approx(0.0175)


def test_structured_passes_system_and_list_content() -> None:
    stub = StubAnthropicClient(parse_results=[parsed_response(Extraction(title="x"))])
    client = LLMClient(anthropic_client=stub)
    blocks: list[dict[str, Any]] = [{"type": "text", "text": "hi"}]

    client.structured(
        feature="extract", output_model=Extraction, content=blocks, system="be careful"
    )

    [call] = stub.messages.parse_calls
    assert call["system"] == "be careful"
    assert call["messages"] == [{"role": "user", "content": blocks}]


def test_structured_model_resolution_explicit_beats_fast() -> None:
    stub = StubAnthropicClient(
        parse_results=[
            parsed_response(Extraction(title="a")),
            parsed_response(Extraction(title="b")),
        ]
    )
    client = LLMClient(anthropic_client=stub)

    client.structured(
        feature="f", output_model=Extraction, content="c", model="claude-sonnet-4-6", fast=True
    )
    client.structured(feature="f", output_model=Extraction, content="c", fast=True)

    assert stub.messages.parse_calls[0]["model"] == "claude-sonnet-4-6"
    assert stub.messages.parse_calls[1]["model"] == settings.llm_fast_model


def test_structured_repair_retry_succeeds_and_records_both_calls() -> None:
    stub = StubAnthropicClient(
        parse_results=[
            FailingParsedResponse(make_validation_error()),
            parsed_response(Extraction(title="Stew")),
        ]
    )
    recorder = RecorderSpy()
    client = LLMClient(recorder=recorder, anthropic_client=stub)

    result = client.structured(feature="extract", output_model=Extraction, content="raw text")

    assert result.title == "Stew"
    assert len(stub.messages.parse_calls) == 2
    repair_messages = stub.messages.parse_calls[1]["messages"]
    assert repair_messages[0] == {"role": "user", "content": "raw text"}
    assert repair_messages[1]["role"] == "user"
    assert "Your previous output failed validation:" in repair_messages[1]["content"]
    assert "Return ONLY corrected JSON matching the schema." in repair_messages[1]["content"]
    assert len(recorder.records) == 2


def test_structured_double_failure_raises_llm_error() -> None:
    stub = StubAnthropicClient(
        parse_results=[
            FailingParsedResponse(make_validation_error()),
            FailingParsedResponse(make_validation_error()),
        ]
    )
    recorder = RecorderSpy()
    client = LLMClient(recorder=recorder, anthropic_client=stub)

    with pytest.raises(LLMError):
        client.structured(feature="extract", output_model=Extraction, content="raw")

    assert len(stub.messages.parse_calls) == 2
    assert len(recorder.records) == 2


def test_structured_refusal_raises_llm_error() -> None:
    stub = StubAnthropicClient(parse_results=[parsed_response(None, stop_reason="refusal")])
    client = LLMClient(anthropic_client=stub)

    with pytest.raises(LLMError, match="refused"):
        client.structured(feature="extract", output_model=Extraction, content="raw")


def test_structured_recorder_errors_do_not_crash_the_call() -> None:
    stub = StubAnthropicClient(parse_results=[parsed_response(Extraction(title="ok"))])
    client = LLMClient(recorder=ExplodingRecorder(), anthropic_client=stub)

    result = client.structured(feature="extract", output_model=Extraction, content="raw")

    assert result.title == "ok"
    assert client.spent_usd == pytest.approx(0.0175)


# --- classify_bool ------------------------------------------------------------


def test_classify_bool_routes_to_fast_model_and_returns_answer() -> None:
    stub = StubAnthropicClient(parse_results=[parsed_response(SimpleNamespace(answer=True))])
    client = LLMClient(anthropic_client=stub)

    answer = client.classify_bool(
        feature="moderation", question="Is water wet?", content="Water makes things wet."
    )

    assert answer is True
    [call] = stub.messages.parse_calls
    assert call["model"] == settings.llm_fast_model
    assert call["system"] == "Answer the question about the provided content."
    sent = call["messages"][0]["content"]
    assert "Is water wet?" in sent
    assert "Water makes things wet." in sent


def test_classify_bool_returns_false() -> None:
    stub = StubAnthropicClient(parse_results=[parsed_response(SimpleNamespace(answer=False))])
    client = LLMClient(anthropic_client=stub)

    assert client.classify_bool(feature="f", question="q?", content="c") is False


def test_classify_bool_truncates_long_content() -> None:
    stub = StubAnthropicClient(parse_results=[parsed_response(SimpleNamespace(answer=True))])
    client = LLMClient(anthropic_client=stub)

    client.classify_bool(feature="f", question="q?", content="x" * 50, max_chars=10)

    sent = stub.messages.parse_calls[0]["messages"][0]["content"]
    assert "x" * 10 + " (truncated)" in sent
    assert "x" * 11 not in sent


def test_classify_bool_does_not_truncate_short_content() -> None:
    stub = StubAnthropicClient(parse_results=[parsed_response(SimpleNamespace(answer=True))])
    client = LLMClient(anthropic_client=stub)

    client.classify_bool(feature="f", question="q?", content="short", max_chars=10)

    sent = stub.messages.parse_calls[0]["messages"][0]["content"]
    assert "(truncated)" not in sent


# --- cost cap -----------------------------------------------------------------


def test_cost_cap_raises_before_hitting_the_api() -> None:
    # Each scripted call costs 0.0175 (opus, 1000 in / 500 out). With a cap of
    # 0.015 the first call is allowed (nothing spent yet); the second must raise
    # BEFORE the stub is hit.
    stub = StubAnthropicClient(
        parse_results=[
            parsed_response(Extraction(title="a")),
            parsed_response(Extraction(title="b")),
        ]
    )
    client = LLMClient(anthropic_client=stub, cost_cap_usd=0.015)

    client.structured(feature="f", output_model=Extraction, content="c")
    with pytest.raises(CostCapExceeded):
        client.structured(feature="f", output_model=Extraction, content="c")

    assert len(stub.messages.parse_calls) == 1
    assert client.spent_usd == pytest.approx(0.0175)


def test_cost_cap_allows_calls_under_the_cap() -> None:
    # Cap 0.02 with calls costing 0.0175: spent (0.0175) stays under the cap
    # after one call, so the second call is still allowed; the third raises.
    stub = StubAnthropicClient(
        parse_results=[
            parsed_response(Extraction(title="a")),
            parsed_response(Extraction(title="b")),
        ]
    )
    client = LLMClient(anthropic_client=stub, cost_cap_usd=0.02)

    client.structured(feature="f", output_model=Extraction, content="c")
    client.structured(feature="f", output_model=Extraction, content="c")
    with pytest.raises(CostCapExceeded):
        client.structured(feature="f", output_model=Extraction, content="c")

    assert len(stub.messages.parse_calls) == 2


def test_cost_cap_applies_to_tool_loop_rounds() -> None:
    stub = StubAnthropicClient(
        create_results=[message_response([tool_use_block("t1")], "tool_use")]
    )
    client = LLMClient(anthropic_client=stub, cost_cap_usd=0.01)

    with pytest.raises(CostCapExceeded):
        client.tool_loop(
            feature="agent",
            system="sys",
            tools=[],
            initial_content=[{"type": "text", "text": "go"}],
            execute=lambda name, args: "ok",
        )

    # first round allowed (spent 0 < cap), second round blocked before the API
    assert len(stub.messages.create_calls) == 1


def test_no_cost_cap_means_unlimited() -> None:
    stub = StubAnthropicClient(
        parse_results=[parsed_response(Extraction(title="a"))],
    )
    client = LLMClient(anthropic_client=stub)
    client.structured(feature="f", output_model=Extraction, content="c")
    assert client.spent_usd > 0  # no cap configured, no exception


# --- tool_loop ----------------------------------------------------------------


def _loop_kwargs(execute: Any, **overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "feature": "agent",
        "system": "you are an agent",
        "tools": [{"name": "lookup", "description": "look up", "input_schema": {"type": "object"}}],
        "initial_content": [{"type": "text", "text": "go"}],
        "execute": execute,
    }
    kwargs.update(overrides)
    return kwargs


def test_tool_loop_executes_tools_and_returns_final_message() -> None:
    block1 = tool_use_block("toolu_1", "lookup", {"q": "carrots"})
    block2 = tool_use_block("toolu_2", "lookup", {"q": "onions"})
    final = message_response([text_block("all done")], "end_turn")
    stub = StubAnthropicClient(
        create_results=[
            message_response([block1], "tool_use"),
            message_response([block2], "tool_use"),
            final,
        ]
    )
    recorder = RecorderSpy()
    client = LLMClient(recorder=recorder, anthropic_client=stub)
    executed: list[tuple[str, dict[str, Any]]] = []

    def execute(name: str, args: dict[str, Any]) -> str:
        executed.append((name, args))
        return "ok:" + args["q"]

    result = client.tool_loop(**_loop_kwargs(execute))

    assert result is final
    assert executed == [("lookup", {"q": "carrots"}), ("lookup", {"q": "onions"})]

    first_call = stub.messages.create_calls[0]
    assert first_call["model"] == settings.llm_model
    assert first_call["system"] == "you are an agent"
    assert first_call["tools"] == _loop_kwargs(execute)["tools"]
    assert first_call["messages"][0] == {
        "role": "user",
        "content": [{"type": "text", "text": "go"}],
    }

    second_messages = stub.messages.create_calls[1]["messages"]
    assert second_messages[1] == {"role": "assistant", "content": [block1]}
    [tool_result] = second_messages[2]["content"]
    assert second_messages[2]["role"] == "user"
    assert tool_result["type"] == "tool_result"
    assert tool_result["tool_use_id"] == "toolu_1"
    assert tool_result["content"] == "ok:carrots"
    assert tool_result["is_error"] is False

    # usage recorded every round
    assert len(recorder.records) == 3
    assert client.spent_usd == pytest.approx(3 * 0.0175)


def test_tool_loop_execute_exception_becomes_is_error_tool_result() -> None:
    stub = StubAnthropicClient(
        create_results=[
            message_response([tool_use_block("toolu_9")], "tool_use"),
            message_response([text_block()], "end_turn"),
        ]
    )
    client = LLMClient(anthropic_client=stub)

    def execute(name: str, args: dict[str, Any]) -> str:
        raise RuntimeError("boom: lookup failed")

    client.tool_loop(**_loop_kwargs(execute))

    [tool_result] = stub.messages.create_calls[1]["messages"][2]["content"]
    assert tool_result["tool_use_id"] == "toolu_9"
    assert tool_result["is_error"] is True
    assert "boom: lookup failed" in tool_result["content"]


def test_tool_loop_budget_exceeded_after_max_iterations() -> None:
    last = message_response([tool_use_block("t2")], "tool_use")
    stub = StubAnthropicClient(
        create_results=[message_response([tool_use_block("t1")], "tool_use"), last]
    )
    recorder = RecorderSpy()
    client = LLMClient(recorder=recorder, anthropic_client=stub)

    with pytest.raises(BudgetExceeded) as exc_info:
        client.tool_loop(**_loop_kwargs(lambda name, args: "ok", max_iterations=2))

    assert exc_info.value.last_response is last
    assert len(stub.messages.create_calls) == 2
    assert len(recorder.records) == 2


@pytest.mark.parametrize(
    ("stop_reason", "match"),
    [
        ("refusal", "refused"),
        ("max_tokens", "output truncated"),
        ("pause_turn", "stop_reason"),
    ],
)
def test_tool_loop_bad_stop_reasons_raise(stop_reason: str, match: str) -> None:
    stub = StubAnthropicClient(create_results=[message_response([], stop_reason)])
    client = LLMClient(anthropic_client=stub)

    with pytest.raises(LLMError, match=match):
        client.tool_loop(**_loop_kwargs(lambda name, args: "ok"))


def test_tool_loop_records_usage_even_on_error_stop() -> None:
    recorder = RecorderSpy()
    stub = StubAnthropicClient(create_results=[message_response([], "max_tokens")])
    client = LLMClient(recorder=recorder, anthropic_client=stub)

    with pytest.raises(LLMError):
        client.tool_loop(**_loop_kwargs(lambda name, args: "ok"))

    assert len(recorder.records) == 1


# --- image_block --------------------------------------------------------------


def test_image_block_builds_base64_source() -> None:
    data = b"\x89PNG\r\n\x1a\nfakebytes"
    block = image_block(data, "image/png")
    assert block == {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": base64.standard_b64encode(data).decode("ascii"),
        },
    }
