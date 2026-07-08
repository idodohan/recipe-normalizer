"""AI API routes: conversation storage.

All routes are authenticated (`get_current_user`) and owner-scoped — a user
only ever sees their own conversations, never another user's, even for the
same recipe (see ai/service.py).

LLM-backed routes (chat, cookbook Q&A, transformations) land in later
Phase 3 tasks; this router only exposes the storage CRUD.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from recipe_normalizer.ai import service
from recipe_normalizer.ai.schemas import (
    ConversationCreateIn,
    ConversationDetailOut,
    ConversationOut,
)
from recipe_normalizer.api_deps import get_current_user
from recipe_normalizer.db import get_db

router = APIRouter(prefix="/api/ai", tags=["ai"])


@router.get("/conversations", response_model=list[ConversationOut])
def list_conversations(
    recipe_id: uuid.UUID | None = Query(default=None),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
    current_user: Any = Depends(get_current_user),  # noqa: B008
) -> list[ConversationOut]:
    return service.list_conversations(db, user_id=current_user.id, recipe_id=recipe_id)


@router.post("/conversations", status_code=201, response_model=ConversationOut)
def create_conversation(
    body: ConversationCreateIn,
    db: Session = Depends(get_db),  # noqa: B008
    current_user: Any = Depends(get_current_user),  # noqa: B008
) -> ConversationOut:
    return service.create_conversation(
        db,
        user_id=current_user.id,
        recipe_id=body.recipe_id,
        kind=body.kind,
        title=body.title,
    )


@router.get("/conversations/{conversation_id}", response_model=ConversationDetailOut)
def get_conversation(
    conversation_id: uuid.UUID,
    db: Session = Depends(get_db),  # noqa: B008
    current_user: Any = Depends(get_current_user),  # noqa: B008
) -> ConversationDetailOut:
    return service.get_conversation(db, user_id=current_user.id, conversation_id=conversation_id)
