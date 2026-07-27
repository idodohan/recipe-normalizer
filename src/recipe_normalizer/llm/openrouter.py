"""OpenRouterClient — anthropic-shaped adapter over OpenRouter's OpenAI-compatible API.

Duck-types the subset of ``anthropic.Anthropic`` that LLMClient uses
(``.messages.create(**anthropic_kwargs)`` returning a Message-shaped object with
``.content`` blocks, ``.stop_reason``, and ``.usage``), so LLMClient and every caller
stay provider-agnostic. Translation happens entirely inside this module:

- anthropic message/content blocks -> OpenAI chat messages (images become data-URI
  ``image_url`` parts; ``tool_result`` blocks become ``role: tool`` messages, with any
  images hoisted into a follow-up user message since OpenAI tool messages are text-only)
- anthropic ``tools`` -> OpenAI function tools
- ``output_config`` json_schema -> ``response_format`` json_schema (strict)
- OpenAI ``finish_reason`` -> anthropic ``stop_reason``
- usage: requests OpenRouter cost accounting (``usage: {include: true}``) and exposes it
  as ``usage.cost_usd`` — the exact spend, $0 for ``:free`` models — which LLMClient
  prefers over the static pricing table.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_BASE_URL = "https://openrouter.ai/api/v1"
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 524})

_FINISH_REASON_TO_STOP_REASON = {
    "stop": "end_turn",
    "tool_calls": "tool_use",
    "length": "max_tokens",
    "content_filter": "refusal",
}


class OpenRouterError(Exception):
    """Raised on OpenRouter API failures (auth, rate limits after retries, bad requests)."""


def _default_http_client(timeout: float) -> httpx.Client:
    """Factory for the real HTTP client; tests monkeypatch this to block network."""
    return httpx.Client(timeout=timeout)


def _get(block: Any, key: str, default: Any = None) -> Any:
    """Read *key* from a content block that may be a dict or an attribute object."""
    if isinstance(block, dict):
        return block.get(key, default)
    return getattr(block, key, default)


def _image_part(block: Any) -> dict[str, Any]:
    source = _get(block, "source") or {}
    media_type = _get(source, "media_type", "image/png")
    data = _get(source, "data", "")
    return {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{data}"}}


def _tool_result_to_messages(block: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Translate one anthropic tool_result block.

    Returns the ``role: tool`` message plus any image parts that must be hoisted
    into a follow-up user message (OpenAI tool messages carry text only).
    """
    content = _get(block, "content", "")
    hoisted: list[dict[str, Any]] = []
    if isinstance(content, str):
        text = content
    else:
        texts: list[str] = []
        for part in content:
            part_type = _get(part, "type")
            if part_type == "text":
                texts.append(_get(part, "text", ""))
            elif part_type == "image":
                hoisted.append(_image_part(part))
        if hoisted:
            texts.append("(tool returned an image; see the attached image below)")
        text = "\n".join(texts) or "(empty result)"
    message = {
        "role": "tool",
        "tool_call_id": _get(block, "tool_use_id"),
        "content": text,
    }
    return message, hoisted


def _user_content_to_openai(content: Any) -> tuple[list[dict[str, Any]], Any]:
    """Split a user turn into tool messages (from tool_result blocks) and content parts."""
    if isinstance(content, str):
        return [], content
    tool_messages: list[dict[str, Any]] = []
    parts: list[dict[str, Any]] = []
    hoisted_images: list[dict[str, Any]] = []
    for block in content:
        block_type = _get(block, "type")
        if block_type == "text":
            parts.append({"type": "text", "text": _get(block, "text", "")})
        elif block_type == "image":
            parts.append(_image_part(block))
        elif block_type == "tool_result":
            message, hoisted = _tool_result_to_messages(block)
            tool_messages.append(message)
            hoisted_images.extend(hoisted)
        else:
            raise OpenRouterError(f"unsupported user content block type: {block_type!r}")
    parts.extend(hoisted_images)
    return tool_messages, parts or None


def _assistant_content_to_openai(content: Any) -> dict[str, Any]:
    if isinstance(content, str):
        return {"role": "assistant", "content": content}
    texts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for block in content:
        block_type = _get(block, "type")
        if block_type == "text":
            texts.append(_get(block, "text", ""))
        elif block_type == "tool_use":
            tool_calls.append(
                {
                    "id": _get(block, "id"),
                    "type": "function",
                    "function": {
                        "name": _get(block, "name"),
                        "arguments": json.dumps(_get(block, "input") or {}),
                    },
                }
            )
    message: dict[str, Any] = {"role": "assistant", "content": "\n".join(texts) or None}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return message


def _translate_messages(messages: list[dict[str, Any]], system: str | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if system is not None:
        out.append({"role": "system", "content": system})
    for message in messages:
        role = message["role"]
        content = message["content"]
        if role == "assistant":
            out.append(_assistant_content_to_openai(content))
        elif role == "user":
            tool_messages, parts = _user_content_to_openai(content)
            out.extend(tool_messages)
            if parts is not None:
                out.append({"role": "user", "content": parts})
        else:
            raise OpenRouterError(f"unsupported message role: {role!r}")
    return out


def _translate_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": tool["input_schema"],
            },
        }
        for tool in tools
    ]


def _parse_response(payload: dict[str, Any]) -> SimpleNamespace:
    choice = payload["choices"][0]
    message = choice["message"]
    blocks: list[SimpleNamespace] = []
    text = message.get("content")
    if text:
        blocks.append(SimpleNamespace(type="text", text=text))
    for call in message.get("tool_calls") or []:
        try:
            arguments = json.loads(call["function"].get("arguments") or "{}")
        except json.JSONDecodeError:
            arguments = {}
        blocks.append(
            SimpleNamespace(
                type="tool_use",
                id=call["id"],
                name=call["function"]["name"],
                input=arguments,
            )
        )
    finish_reason = choice.get("finish_reason")
    # Some OpenAI-compatible backends return finish_reason "stop" (or null) even
    # when they emitted tool calls; trust the presence of tool_calls over the
    # label so tool_loop doesn't end a turn without dispatching the tool.
    if message.get("tool_calls"):
        stop_reason: str | None = "tool_use"
    else:
        stop_reason = _FINISH_REASON_TO_STOP_REASON.get(finish_reason)
    if stop_reason is None:
        # e.g. "error" or null from some providers — surface the raw value in
        # logs so the operator sees the real cause, then pass it through for
        # LLMClient's stop_reason handling to reject with context.
        logger.warning("openrouter returned unmapped finish_reason: %r", finish_reason)
        stop_reason = finish_reason
    usage = payload.get("usage") or {}
    return SimpleNamespace(
        content=blocks,
        stop_reason=stop_reason,
        usage=SimpleNamespace(
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
            cost_usd=usage.get("cost"),
        ),
    )


class _Messages:
    def __init__(self, client: OpenRouterClient) -> None:
        self._client = client

    def create(
        self,
        *,
        model: str,
        max_tokens: int,
        messages: list[dict[str, Any]],
        system: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        output_config: dict[str, Any] | None = None,
    ) -> SimpleNamespace:
        body: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": _translate_messages(messages, system),
            "usage": {"include": True},
            # Only route to endpoints that actually support the parameters we
            # send (response_format / tools) — critical for auto-routed models
            # like openrouter/free, where a non-supporting endpoint would
            # silently ignore the schema and return prose.
            "provider": {"require_parameters": True},
        }
        if tools is not None:
            body["tools"] = _translate_tools(tools)
        if output_config is not None:
            schema = output_config["format"]["schema"]
            # strict:false on purpose. OpenAI-strict demands every property be
            # listed in `required` (optionality via type unions with "null");
            # pydantic-derived schemas (see llm/schema.py) omit defaulted
            # fields from `required`, so strict:true would 400 on strict-backed
            # endpoints — and rewriting the schema to all-required+nullable
            # would make models emit nulls that the pydantic models reject.
            # Non-strict schema-guided output plus LLMClient's repair retry is
            # the robust combination.
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "output", "strict": False, "schema": schema},
            }
        return _parse_response(self._client._post(body))


class OpenRouterClient:
    """Anthropic-shaped client for OpenRouter; inject into ``LLMClient(chat_client=...)``."""

    def __init__(
        self,
        *,
        api_key: str,
        max_retries: int = 3,
        timeout: float = 120.0,
        http_client: httpx.Client | None = None,
        retry_sleep: Callable[[float], None] = time.sleep,
        app_title: str = "Recipe Normalizer",
    ) -> None:
        if not api_key:
            raise OpenRouterError(
                "OpenRouter API key missing — set OPENROUTER_API_KEY "
                "(free keys at https://openrouter.ai/keys)"
            )
        self._api_key = api_key
        self._max_retries = max_retries
        self._retry_sleep = retry_sleep
        self._http = http_client if http_client is not None else _default_http_client(timeout)
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            # App attribution headers (used by OpenRouter for ranking/limits).
            "HTTP-Referer": "http://localhost:5173",
            "X-Title": app_title,
        }
        self.messages = _Messages(self)

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        last_status: int | None = None
        retry_after: float | None = None
        for attempt in range(self._max_retries + 1):
            if attempt:
                # Honor the server's Retry-After on 429 (OpenRouter sends it on
                # free-tier limits); otherwise exponential backoff.
                self._retry_sleep(
                    retry_after if retry_after is not None else min(2.0**attempt, 30.0)
                )
                retry_after = None
            try:
                response = self._http.post(
                    f"{_BASE_URL}/chat/completions", json=body, headers=self._headers
                )
            except httpx.HTTPError as exc:
                last_status = None
                logger.warning("openrouter request error (attempt %d): %s", attempt + 1, exc)
                continue
            if response.status_code == 200:
                return response.json()  # type: ignore[no-any-return]
            last_status = response.status_code
            if response.status_code not in _RETRYABLE_STATUS:
                raise OpenRouterError(
                    f"openrouter request failed ({response.status_code}): {_error_detail(response)}"
                )
            header = response.headers.get("retry-after")
            if header is not None:
                try:
                    retry_after = min(float(header), 60.0)
                except ValueError:
                    retry_after = None
            logger.warning(
                "openrouter %d (attempt %d): %s",
                response.status_code,
                attempt + 1,
                _error_detail(response),
            )
        raise OpenRouterError(
            f"openrouter request failed after {self._max_retries + 1} attempts"
            + (f" (last status {last_status})" if last_status else "")
        )


def _error_detail(response: httpx.Response) -> str:
    try:
        detail = response.json().get("error", {}).get("message", "")
    except Exception:
        detail = ""
    return detail or response.text[:200]
