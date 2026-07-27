"""OpenRouterClient adapter: anthropic-shaped calls in, OpenAI wire format out.

Every test injects an httpx.MockTransport — no real network (conftest blocks
the default transport factory as a structural guarantee).
"""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from recipe_normalizer.llm.openrouter import OpenRouterClient, OpenRouterError


def completion_json(
    *,
    content: str | None = "hello",
    tool_calls: list[dict[str, Any]] | None = None,
    finish_reason: str = "stop",
    prompt_tokens: int = 1000,
    completion_tokens: int = 500,
    cost: float | None = 0.0,
) -> dict[str, Any]:
    usage: dict[str, Any] = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
    }
    if cost is not None:
        usage["cost"] = cost
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return {
        "id": "gen-1",
        "choices": [{"message": message, "finish_reason": finish_reason}],
        "usage": usage,
    }


class Capture:
    """MockTransport handler that records request bodies and replays responses."""

    def __init__(self, responses: list[httpx.Response | dict[str, Any]]) -> None:
        self._responses = list(responses)
        self.bodies: list[dict[str, Any]] = []
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.bodies.append(json.loads(request.content))
        result = self._responses.pop(0)
        if isinstance(result, dict):
            return httpx.Response(200, json=result)
        return result


def make_client(
    responses: list[httpx.Response | dict[str, Any]],
    *,
    api_key: str = "sk-or-test",
) -> tuple[OpenRouterClient, Capture]:
    capture = Capture(responses)
    client = OpenRouterClient(
        api_key=api_key,
        http_client=httpx.Client(transport=httpx.MockTransport(capture)),
        retry_sleep=lambda _s: None,
    )
    return client, capture


def test_basic_text_roundtrip() -> None:
    client, capture = make_client([completion_json(content="hi there", cost=0.0012)])
    response = client.messages.create(
        model="openrouter/free",
        max_tokens=100,
        system="be brief",
        messages=[{"role": "user", "content": "hi"}],
    )

    body = capture.bodies[0]
    assert body["model"] == "openrouter/free"
    assert body["max_tokens"] == 100
    assert body["usage"] == {"include": True}
    assert body["messages"][0] == {"role": "system", "content": "be brief"}
    assert body["messages"][1] == {"role": "user", "content": "hi"}

    assert response.stop_reason == "end_turn"
    assert [b.type for b in response.content] == ["text"]
    assert response.content[0].text == "hi there"
    assert response.usage.input_tokens == 1000
    assert response.usage.output_tokens == 500
    assert response.usage.cost_usd == pytest.approx(0.0012)

    auth = capture.requests[0].headers["authorization"]
    assert auth == "Bearer sk-or-test"


def test_output_config_becomes_response_format() -> None:
    client, capture = make_client([completion_json(content='{"answer": true}')])
    schema = {"type": "object", "properties": {"answer": {"type": "boolean"}}}
    client.messages.create(
        model="m",
        max_tokens=10,
        messages=[{"role": "user", "content": "q"}],
        output_config={"format": {"type": "json_schema", "schema": schema}},
    )
    body = capture.bodies[0]
    rf = body["response_format"]
    assert rf["type"] == "json_schema"
    # strict:false on purpose — pydantic-derived schemas omit defaulted fields
    # from `required`, which OpenAI-strict rejects; see adapter comment.
    assert rf["json_schema"]["strict"] is False
    assert rf["json_schema"]["schema"] == schema
    assert body["provider"] == {"require_parameters": True}


def test_real_pipeline_schema_passes_through_adapter() -> None:
    """The actual extraction schema (NormalizeResult) must be sendable as-is."""
    from recipe_normalizer.extraction.normalize import NormalizeResult
    from recipe_normalizer.llm.schema import strict_json_schema

    schema = strict_json_schema(NormalizeResult)
    client, capture = make_client([completion_json(content="{}")])
    client.messages.create(
        model="m",
        max_tokens=10,
        messages=[{"role": "user", "content": "q"}],
        output_config={"format": {"type": "json_schema", "schema": schema}},
    )
    sent = capture.bodies[0]["response_format"]["json_schema"]
    assert sent["strict"] is False
    assert sent["schema"] == schema


def test_retry_honors_retry_after_header() -> None:
    sleeps: list[float] = []
    capture = Capture(
        [
            httpx.Response(429, json={}, headers={"retry-after": "7"}),
            completion_json(content="ok"),
        ]
    )
    client = OpenRouterClient(
        api_key="k",
        http_client=httpx.Client(transport=httpx.MockTransport(capture)),
        retry_sleep=sleeps.append,
        max_retries=2,
    )
    client.messages.create(model="m", max_tokens=10, messages=[{"role": "user", "content": "q"}])
    assert sleeps == [7.0]


def test_tools_translation_and_tool_calls_response() -> None:
    tool_calls = [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "lookup", "arguments": '{"q": "flour"}'},
        }
    ]
    client, capture = make_client(
        [completion_json(content=None, tool_calls=tool_calls, finish_reason="tool_calls")]
    )
    response = client.messages.create(
        model="m",
        max_tokens=10,
        messages=[{"role": "user", "content": "q"}],
        tools=[
            {
                "name": "lookup",
                "description": "Search recipes",
                "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}},
            }
        ],
    )
    sent_tool = capture.bodies[0]["tools"][0]
    assert sent_tool["type"] == "function"
    assert sent_tool["function"]["name"] == "lookup"
    assert sent_tool["function"]["description"] == "Search recipes"
    assert sent_tool["function"]["parameters"]["properties"]["q"] == {"type": "string"}

    assert response.stop_reason == "tool_use"
    block = response.content[0]
    assert (block.type, block.id, block.name, block.input) == (
        "tool_use",
        "call_1",
        "lookup",
        {"q": "flour"},
    )


def test_assistant_blocks_and_tool_results_roundtrip() -> None:
    """The tool_loop replays our response blocks + anthropic tool_result dicts."""
    client, capture = make_client([completion_json()])
    assistant_blocks = [
        SimpleNamespace(type="text", text="checking"),
        SimpleNamespace(type="tool_use", id="call_1", name="lookup", input={"q": "gin"}),
    ]
    tool_results = [
        {
            "type": "tool_result",
            "tool_use_id": "call_1",
            "content": "gin: 40% abv",
            "is_error": False,
        }
    ]
    client.messages.create(
        model="m",
        max_tokens=10,
        messages=[
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": assistant_blocks},
            {"role": "user", "content": tool_results},
        ],
    )
    messages = capture.bodies[0]["messages"]
    assistant = messages[1]
    assert assistant["role"] == "assistant"
    assert assistant["content"] == "checking"
    assert assistant["tool_calls"][0]["id"] == "call_1"
    assert json.loads(assistant["tool_calls"][0]["function"]["arguments"]) == {"q": "gin"}
    tool_msg = messages[2]
    assert tool_msg == {"role": "tool", "tool_call_id": "call_1", "content": "gin: 40% abv"}


def test_tool_result_with_image_hoists_to_user_message() -> None:
    client, capture = make_client([completion_json()])
    png = b"\x89PNG fake"
    image = {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": base64.standard_b64encode(png).decode("ascii"),
        },
    }
    client.messages.create(
        model="m",
        max_tokens=10,
        messages=[
            {"role": "user", "content": "q"},
            {
                "role": "assistant",
                "content": [
                    SimpleNamespace(type="tool_use", id="call_1", name="screenshot", input={})
                ],
            },
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "call_1", "content": [image]}],
            },
        ],
    )
    messages = capture.bodies[0]["messages"]
    tool_msg = messages[2]
    assert tool_msg["role"] == "tool"
    assert "image" in tool_msg["content"]  # placeholder text pointing at hoisted image
    hoisted = messages[3]
    assert hoisted["role"] == "user"
    assert hoisted["content"][0]["type"] == "image_url"
    assert hoisted["content"][0]["image_url"]["url"].startswith("data:image/png;base64,")


def test_user_image_blocks_become_image_url_parts() -> None:
    client, capture = make_client([completion_json()])
    data = base64.standard_b64encode(b"jpegbytes").decode("ascii")
    client.messages.create(
        model="m",
        max_tokens=10,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "what recipe is this?"},
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": data,
                        },
                    },
                ],
            }
        ],
    )
    parts = capture.bodies[0]["messages"][0]["content"]
    assert parts[0] == {"type": "text", "text": "what recipe is this?"}
    assert parts[1]["type"] == "image_url"
    assert parts[1]["image_url"]["url"] == f"data:image/jpeg;base64,{data}"


@pytest.mark.parametrize(
    ("finish_reason", "expected"),
    [
        ("stop", "end_turn"),
        ("tool_calls", "tool_use"),
        ("length", "max_tokens"),
        ("content_filter", "refusal"),
    ],
)
def test_finish_reason_mapping(finish_reason: str, expected: str) -> None:
    tool_calls = (
        [{"id": "c", "type": "function", "function": {"name": "t", "arguments": "{}"}}]
        if finish_reason == "tool_calls"
        else None
    )
    client, _ = make_client(
        [completion_json(content="x", tool_calls=tool_calls, finish_reason=finish_reason)]
    )
    response = client.messages.create(
        model="m", max_tokens=10, messages=[{"role": "user", "content": "q"}]
    )
    assert response.stop_reason == expected


def test_stop_finish_reason_with_tool_calls_maps_to_tool_use() -> None:
    """A backend that returns finish_reason 'stop' but populated tool_calls
    must still surface as tool_use so tool_loop dispatches the tool."""
    tool_calls = [
        {"id": "c1", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}
    ]
    client, _ = make_client(
        [completion_json(content=None, tool_calls=tool_calls, finish_reason="stop")]
    )
    response = client.messages.create(
        model="m", max_tokens=10, messages=[{"role": "user", "content": "q"}]
    )
    assert response.stop_reason == "tool_use"
    assert response.content[0].type == "tool_use"


def test_usage_cost_absent_maps_to_none() -> None:
    client, _ = make_client([completion_json(cost=None)])
    response = client.messages.create(
        model="m", max_tokens=10, messages=[{"role": "user", "content": "q"}]
    )
    assert response.usage.cost_usd is None


def test_retries_on_429_then_succeeds() -> None:
    sleeps: list[float] = []
    capture = Capture(
        [
            httpx.Response(429, json={"error": {"message": "rate limited"}}),
            completion_json(content="ok"),
        ]
    )
    client = OpenRouterClient(
        api_key="k",
        http_client=httpx.Client(transport=httpx.MockTransport(capture)),
        retry_sleep=sleeps.append,
        max_retries=2,
    )
    response = client.messages.create(
        model="m", max_tokens=10, messages=[{"role": "user", "content": "q"}]
    )
    assert response.content[0].text == "ok"
    assert len(sleeps) == 1


def test_gives_up_after_max_retries() -> None:
    capture = Capture([httpx.Response(429, json={}) for _ in range(3)])
    client = OpenRouterClient(
        api_key="k",
        http_client=httpx.Client(transport=httpx.MockTransport(capture)),
        retry_sleep=lambda _s: None,
        max_retries=2,
    )
    with pytest.raises(OpenRouterError, match="429"):
        client.messages.create(
            model="m", max_tokens=10, messages=[{"role": "user", "content": "q"}]
        )
    assert len(capture.bodies) == 3  # initial + 2 retries


def test_4xx_does_not_retry() -> None:
    capture = Capture([httpx.Response(400, json={"error": {"message": "bad schema"}})])
    client = OpenRouterClient(
        api_key="k",
        http_client=httpx.Client(transport=httpx.MockTransport(capture)),
        retry_sleep=lambda _s: None,
        max_retries=2,
    )
    with pytest.raises(OpenRouterError, match="bad schema"):
        client.messages.create(
            model="m", max_tokens=10, messages=[{"role": "user", "content": "q"}]
        )
    assert len(capture.bodies) == 1


def test_missing_api_key_raises_actionable_error() -> None:
    with pytest.raises(OpenRouterError, match="OPENROUTER_API_KEY"):
        OpenRouterClient(api_key="")


# --- provider resolution (client.py) -------------------------------------------


def test_resolve_provider_auto_prefers_openrouter(monkeypatch: pytest.MonkeyPatch) -> None:
    from recipe_normalizer.config import settings
    from recipe_normalizer.llm.client import resolve_provider

    monkeypatch.setattr(settings, "llm_provider", "auto")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    assert resolve_provider() == "openrouter"

    monkeypatch.delenv("OPENROUTER_API_KEY")
    assert resolve_provider() == "anthropic"

    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert resolve_provider() == "openrouter"  # no keys: openrouter errors actionably

    monkeypatch.setattr(settings, "llm_provider", "anthropic")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-x")
    assert resolve_provider() == "anthropic"  # explicit pin beats auto


def test_default_models_follow_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    from recipe_normalizer.config import settings
    from recipe_normalizer.llm.client import LLMClient

    monkeypatch.setattr(settings, "llm_provider", "openrouter")
    monkeypatch.setattr(settings, "llm_model", "")
    monkeypatch.setattr(settings, "llm_fast_model", "")
    client = LLMClient()
    assert client._resolve_model(None, fast=False) == "openrouter/free"
    assert client._resolve_model(None, fast=True) == "openrouter/free"
    assert client._resolve_model("custom/model", fast=True) == "custom/model"

    monkeypatch.setattr(settings, "llm_model", "nvidia/nemotron-3-super-120b-a12b:free")
    assert client._resolve_model(None, fast=False) == "nvidia/nemotron-3-super-120b-a12b:free"
