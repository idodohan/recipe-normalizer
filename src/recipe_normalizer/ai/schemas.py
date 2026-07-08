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
