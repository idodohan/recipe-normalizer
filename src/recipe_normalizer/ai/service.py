"""AI service: conversation + message storage and owner-scoped access.

Talks to `cookbook` only via `cookbook.service` (never `cookbook.models`) —
see .importlinter. Transaction convention matches the rest of the codebase:
this module flushes; the HTTP layer (or test) owns commit.

No LLM calls happen in this module yet (Phase 3 Task 1) — later tasks add
`chat_turn` / `cookbook_qa_turn` / `transform_recipe` alongside this storage
layer, reusing `create_conversation` / `append_message` / `enforce_message_cap`
as building blocks.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from recipe_normalizer.ai.models import Conversation, ConversationKind, Message, MessageRole
from recipe_normalizer.ai.schemas import ConversationDetailOut, ConversationOut, MessageOut
from recipe_normalizer.cookbook import service as cookbook_service
from recipe_normalizer.errors import ApiError

__all__ = [
    "MAX_MESSAGES_PER_CONVERSATION",
    "append_message",
    "create_conversation",
    "enforce_message_cap",
    "get_conversation",
    "list_conversations",
]

#: Bounds context growth (and cost) for a single conversation — see the
#: phase plan's "Cost & abuse" global constraint. Enforced by
#: `enforce_message_cap`, called from `append_message` before every insert.
MAX_MESSAGES_PER_CONVERSATION = 50


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

    Does NOT check ownership itself — callers (e.g. the future `chat_turn`)
    are expected to have already resolved/authorized the conversation (via
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
