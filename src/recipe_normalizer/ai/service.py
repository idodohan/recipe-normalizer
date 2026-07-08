"""AI service: conversation + message storage, and the recipe-chat LLM turn.

Talks to `cookbook` only via `cookbook.service` (never `cookbook.models`) —
see .importlinter. Transaction convention matches the rest of the codebase:
this module flushes; the HTTP layer (or test) owns commit.

`chat_turn` is the first LLM-backed feature (Phase 3 Task 2); later tasks add
`cookbook_qa_turn` / `transform_recipe` alongside it, reusing
`create_conversation` / `append_message` / `enforce_message_cap` as building
blocks.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from recipe_normalizer.ai.models import Conversation, ConversationKind, Message, MessageRole
from recipe_normalizer.ai.prompts import RECIPE_CHAT_SYSTEM
from recipe_normalizer.ai.schemas import ConversationDetailOut, ConversationOut, MessageOut
from recipe_normalizer.config import settings
from recipe_normalizer.cookbook import service as cookbook_service
from recipe_normalizer.cookbook.schemas import RecipeOut
from recipe_normalizer.errors import ApiError
from recipe_normalizer.llm.client import CostCapExceeded, DbUsageRecorder, LLMClient, LLMError

logger = logging.getLogger(__name__)

__all__ = [
    "MAX_HISTORY_MESSAGES_FOR_CONTEXT",
    "MAX_MESSAGES_PER_CONVERSATION",
    "append_message",
    "chat_turn",
    "create_conversation",
    "enforce_message_cap",
    "get_conversation",
    "list_conversations",
    "make_ai_llm",
]

#: Bounds context growth (and cost) for a single conversation — see the
#: phase plan's "Cost & abuse" global constraint. Enforced by
#: `enforce_message_cap`, called from `append_message` before every insert.
MAX_MESSAGES_PER_CONVERSATION = 50

#: How many prior messages are sent back to the model as conversational
#: context on each turn. Independent of `MAX_MESSAGES_PER_CONVERSATION` (which
#: bounds how long a conversation may grow in storage) — this bounds the
#: per-call prompt size/cost. A long-running conversation still sends only
#: its most recent slice, oldest-first, to the model.
MAX_HISTORY_MESSAGES_FOR_CONTEXT = 20


def _to_conversation_out(conversation: Conversation) -> ConversationOut:
    return ConversationOut.model_validate(conversation)


def _to_message_out(message: Message) -> MessageOut:
    return MessageOut.model_validate(message)


def create_conversation(
    db: Session,
    *,
    user_id: uuid.UUID,
    recipe_id: uuid.UUID | None = None,
    kind: ConversationKind,
    title: str | None = None,
) -> ConversationOut:
    """Create a new conversation owned by *user_id*.

    When *recipe_id* is given, the caller must already have access to that
    recipe — checked via `cookbook_service.user_recipe_access` (owner OR
    shared-cookbook member, the SAME widened check `cookbook.service.get_recipe`
    uses), 404 otherwise. This mirrors `sharing.service.add_recipe_to_shared_cookbook`'s
    pattern of validating an incoming recipe id via `user_recipe_access` before
    it's used as a foreign key, both to keep "which recipes can I start a
    conversation about" identical to "which recipes can I read", and to avoid
    an unhandled `IntegrityError` (FK violation) for a nonexistent recipe id
    surfacing as a 500 — this way it's a clean 404 instead.

    A recipe_id of ``None`` is valid (a cookbook-wide Q&A conversation) and
    skips this check entirely.

    Flushes; caller owns commit.
    """
    if recipe_id is not None and (
        cookbook_service.user_recipe_access(db, user_id, recipe_id) is None
    ):
        raise ApiError(404, "not_found", f"Recipe {recipe_id} not found.")

    conversation = Conversation(
        user_id=user_id,
        recipe_id=recipe_id,
        kind=kind,
        title=title,
    )
    db.add(conversation)
    db.flush()
    return _to_conversation_out(conversation)


def _get_owned_conversation(
    db: Session, *, user_id: uuid.UUID, conversation_id: uuid.UUID
) -> Conversation:
    """Fetch a conversation, requiring *user_id* to own it.

    404 for BOTH a nonexistent conversation AND one owned by someone
    else — identical body, so a caller probing ids can't distinguish the
    two (the codebase-wide idiom — see `sharing.service._require_member`).
    """
    conversation = db.scalars(
        select(Conversation).where(
            Conversation.id == conversation_id, Conversation.user_id == user_id
        )
    ).first()
    if conversation is None:
        raise ApiError(404, "not_found", f"Conversation {conversation_id} not found.")
    return conversation


def get_conversation(
    db: Session, *, user_id: uuid.UUID, conversation_id: uuid.UUID
) -> ConversationDetailOut:
    """Fetch one conversation plus its full message history, owner-scoped.

    Messages are returned oldest-first (chronological reading order).
    """
    conversation = _get_owned_conversation(db, user_id=user_id, conversation_id=conversation_id)
    messages = db.scalars(
        select(Message)
        .where(Message.conversation_id == conversation.id)
        .order_by(Message.created_at.asc())
    ).all()
    return ConversationDetailOut(
        **_to_conversation_out(conversation).model_dump(),
        messages=[_to_message_out(m) for m in messages],
    )


def list_conversations(
    db: Session, *, user_id: uuid.UUID, recipe_id: uuid.UUID | None = None
) -> list[ConversationOut]:
    """List *user_id*'s own conversations, most-recently-active first.

    When *recipe_id* is given, only conversations about that recipe are
    returned (still owner-scoped — this never surfaces another user's
    conversations about the same recipe, e.g. a shared-cookbook co-member's
    chat history).
    """
    stmt = select(Conversation).where(Conversation.user_id == user_id)
    if recipe_id is not None:
        stmt = stmt.where(Conversation.recipe_id == recipe_id)
    stmt = stmt.order_by(Conversation.updated_at.desc())
    conversations = db.scalars(stmt).all()
    return [_to_conversation_out(c) for c in conversations]


def enforce_message_cap(db: Session, *, conversation_id: uuid.UUID) -> None:
    """Raise ApiError(422, "conversation_full", ...) if *conversation_id* is at capacity.

    Called from `append_message` before every insert — a conversation at
    exactly `MAX_MESSAGES_PER_CONVERSATION` messages rejects the next one
    rather than silently growing forever (see the phase plan's "Cost &
    abuse" global constraint).
    """
    total = (
        db.scalar(
            select(func.count())
            .select_from(Message)
            .where(Message.conversation_id == conversation_id)
        )
        or 0
    )
    if total >= MAX_MESSAGES_PER_CONVERSATION:
        raise ApiError(
            422,
            "conversation_full",
            f"This conversation has reached its {MAX_MESSAGES_PER_CONVERSATION}-message limit.",
        )


def append_message(
    db: Session, *, conversation_id: uuid.UUID, role: MessageRole, content: str
) -> MessageOut:
    """Append a message to a conversation, enforcing the message cap first.

    Does NOT check ownership itself — callers (e.g. `chat_turn`) are expected
    to have already resolved/authorized the conversation (via
    `get_conversation` or an equivalent owner-scoped lookup) before calling
    this. Bumps the parent conversation's `updated_at` so `list_conversations`'
    "most-recently-active first" ordering reflects new activity, not just the
    conversation's creation time.

    Flushes; caller owns commit.
    """
    enforce_message_cap(db, conversation_id=conversation_id)

    conversation = db.get(Conversation, conversation_id)
    if conversation is None:
        raise ApiError(404, "not_found", f"Conversation {conversation_id} not found.")

    message = Message(conversation_id=conversation_id, role=role, content=content)
    db.add(message)
    conversation.updated_at = datetime.now(UTC)
    db.flush()
    return _to_message_out(message)


# ---------------------------------------------------------------------------
# Recipe chat (Phase 3 Task 2)
# ---------------------------------------------------------------------------


def _recipe_grounding_json(recipe: RecipeOut) -> str:
    """Build the compact recipe JSON the chat system prompt grounds on.

    Cooking content only — ingredients (both the original text and whatever
    normalization/catalog matching produced), steps, servings, times,
    cuisines/dish types/tags. Personal fields (favorites, notes, ownership,
    provenance, collections, edit history) are irrelevant to "how do I cook
    this" questions and are deliberately dropped to keep the prompt small and
    the model's attention on cooking content.
    """
    payload: dict[str, Any] = {
        "title": recipe.title,
        "description": recipe.description,
        "servings": recipe.servings.model_dump() if recipe.servings is not None else None,
        "prep_min": recipe.prep_min,
        "cook_min": recipe.cook_min,
        "total_min": recipe.total_min,
        "cuisines": recipe.cuisines,
        "dish_types": recipe.dish_types,
        "tags": recipe.tags,
        "groups": [
            {
                "name": group.name,
                "lines": [
                    {
                        "original_text": line.original_text,
                        "name": line.name,
                        "quantity": line.quantity,
                        "unit": line.unit,
                        "normalized_amount": line.normalized_amount,
                        "normalized_unit": line.normalized_unit,
                        "note": line.note,
                        "is_optional": line.is_optional,
                    }
                    for line in group.lines
                ],
            }
            for group in recipe.groups
        ],
        "steps": [step.original_text for step in recipe.steps],
    }
    return json.dumps(payload, indent=2, default=str)


def chat_turn(
    db: Session,
    *,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    content: str,
    llm: LLMClient,
) -> MessageOut:
    """Run one recipe-chat turn: ground on the recipe JSON, call the LLM, persist both messages.

    Re-verifies TWO things on every call, not just once at
    `create_conversation` time:
      - conversation ownership, via `get_conversation` (404 for a missing or
        someone-else's conversation);
      - CURRENT access to the conversation's recipe, via
        `cookbook_service.user_recipe_access` (404 if access has since been
        lost — e.g. the caller was removed from a shared cookbook after the
        conversation was created; a stale `recipe_id` FK is not enough proof
        of present-day access).

    A conversation with no recipe (`kind=cookbook_qa`) has nothing to ground
    a per-recipe chat on and is rejected with 422 — that's a different
    feature (Task 4).

    Capacity for BOTH the user turn and the assistant reply is checked
    BEFORE the LLM is called (not just at `append_message` time), so a
    conversation that's already at or one short of its cap fails fast with
    422 rather than spending on a call whose answer could never be stored.

    The prompt sent to the model is: `RECIPE_CHAT_SYSTEM` filled in with the
    recipe's grounding JSON, as the system prompt; the conversation's last
    `MAX_HISTORY_MESSAGES_FOR_CONTEXT` messages plus this turn's `content`,
    oldest-first, as the message list. `CostCapExceeded`/other `LLMError`s
    from the call are mapped to API errors — no partial exchange is
    persisted when the call fails.

    Flushes; caller owns commit.
    """
    detail = get_conversation(db, user_id=user_id, conversation_id=conversation_id)
    if detail.recipe_id is None:
        raise ApiError(422, "not_recipe_chat", "This conversation has no recipe to chat about.")

    # Re-check access every turn — NOT just relying on the FK having resolved
    # once at create_conversation time.
    if cookbook_service.user_recipe_access(db, user_id, detail.recipe_id) is None:
        raise ApiError(404, "not_found", f"Recipe {detail.recipe_id} not found.")
    recipe = cookbook_service.get_recipe(db, owner_id=user_id, recipe_id=detail.recipe_id)

    if len(detail.messages) + 2 > MAX_MESSAGES_PER_CONVERSATION:
        raise ApiError(
            422,
            "conversation_full",
            f"This conversation has reached its {MAX_MESSAGES_PER_CONVERSATION}-message limit.",
        )

    history: list[dict[str, Any]] = [
        {"role": message.role.value, "content": message.content}
        for message in detail.messages[-MAX_HISTORY_MESSAGES_FOR_CONTEXT:]
    ]
    history.append({"role": "user", "content": content})

    system = RECIPE_CHAT_SYSTEM.format(recipe_json=_recipe_grounding_json(recipe))
    try:
        answer = llm.chat(
            feature="ai.recipe_chat", system=system, messages=history, max_tokens=1024
        )
    except CostCapExceeded as exc:
        raise ApiError(
            503,
            "cost_cap_exceeded",
            "This request would exceed the AI budget allowed for this call — try again shortly.",
        ) from exc
    except LLMError as exc:
        raise ApiError(502, "llm_error", "The AI assistant could not produce an answer.") from exc

    append_message(db, conversation_id=conversation_id, role=MessageRole.user, content=content)
    return append_message(
        db, conversation_id=conversation_id, role=MessageRole.assistant, content=answer
    )


# ---------------------------------------------------------------------------
# LLM factory — RN_LLM_STUB=1 swaps in a canned client (TEST/E2E ONLY)
# ---------------------------------------------------------------------------


#: Fixed reply for the ai-module stub LLM — deliberately says something
#: recognizable so e2e assertions can key off it without depending on real
#: model output.
_STUB_CHAT_REPLY = "This is a stubbed AI reply (RN_LLM_STUB=1) — no real model was called."


class _StubAiLLMClient:
    """Deterministic fake LLM for ai chat features, enabled ONLY by RN_LLM_STUB=1.

    *** TEST / E2E ONLY — NEVER ENABLE IN PRODUCTION. ***

    Mirrors `worker._StubLLMClient`'s env convention (same `RN_LLM_STUB=1`
    flag, same warning-on-use, same "TEST/E2E ONLY" contract) but for the ai
    module's `.chat()` surface rather than extraction's
    structured/classify_bool/tool_loop. Answers any call with a fixed reply,
    makes zero API calls, and spends $0.
    """

    @property
    def spent_usd(self) -> float:
        return 0.0

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
        return _STUB_CHAT_REPLY


def make_ai_llm(db: Session, *, user_id: uuid.UUID, feature: str) -> LLMClient:
    """Build the per-request LLM client for an ai endpoint.

    `RN_LLM_STUB=1` (env) swaps in `_StubAiLLMClient` — TEST/E2E ONLY, mirrors
    `worker.make_llm_for_job`'s convention exactly (same env var, same
    warning) so e2e runs can exercise recipe chat (and, in later tasks,
    cookbook Q&A / transformations) without an ANTHROPIC_API_KEY.

    Otherwise builds a real `LLMClient` capped at
    `settings.ai_request_cost_cap_usd` per request, with usage recorded to
    *db* under *feature* for *user_id* (no `job_id` — this runs in the API
    process, not a worker job).
    """
    if os.environ.get("RN_LLM_STUB") == "1":
        logger.warning(
            "RN_LLM_STUB=1 — using the stub AI LLM client (test/e2e only), feature=%s", feature
        )
        return cast(LLMClient, _StubAiLLMClient())
    return LLMClient(
        recorder=DbUsageRecorder(db, user_id=user_id),
        cost_cap_usd=settings.ai_request_cost_cap_usd,
    )
