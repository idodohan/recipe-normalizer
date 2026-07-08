"""Pydantic schemas for the ai API."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from recipe_normalizer.ai.models import ConversationKind, MessageRole


class ConversationCreateIn(BaseModel):
    """Body for POST /api/ai/conversations."""

    recipe_id: uuid.UUID | None = None
    kind: ConversationKind
    title: str | None = Field(default=None, max_length=200)


class MessageCreateIn(BaseModel):
    """Body for POST /api/ai/conversations/{id}/messages."""

    content: str = Field(min_length=1, max_length=4000)


class CookbookQaCreateIn(BaseModel):
    """Body for POST /api/ai/cookbook-qa."""

    content: str = Field(min_length=1, max_length=4000)
    conversation_id: uuid.UUID | None = None


class MessageOut(BaseModel):
    id: uuid.UUID
    role: MessageRole
    content: str
    created_at: datetime

    model_config = {"from_attributes": True}


class ConversationOut(BaseModel):
    """One conversation, as it appears in list/create responses (no messages)."""

    id: uuid.UUID
    user_id: uuid.UUID
    recipe_id: uuid.UUID | None
    kind: ConversationKind
    title: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ConversationDetailOut(ConversationOut):
    """GET /api/ai/conversations/{id} — the conversation plus its full message history."""

    messages: list[MessageOut] = []


class CookbookQaOut(BaseModel):
    """Response for POST /api/ai/cookbook-qa.

    `conversation_id` is always present (the conversation the turn was recorded
    to — newly created when the request didn't pass one). `referenced_recipe_ids`
    are the ids of every recipe `search_recipes` returned during this turn's tool
    loop (deduped, first-seen order) — NOT an attempt to parse the model's prose
    for which ones it actually cited; the UI renders these as clickable chips.
    """

    conversation_id: uuid.UUID
    message: MessageOut
    referenced_recipe_ids: list[uuid.UUID] = []
