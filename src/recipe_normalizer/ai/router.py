"""AI API routes: conversation storage + recipe chat.

All routes are authenticated (`get_current_user`) and owner-scoped — a user
only ever sees their own conversations, never another user's, even for the
same recipe (see ai/service.py).

Cookbook Q&A and transformation routes land in later Phase 3 tasks.
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
    CookbookQaCreateIn,
    CookbookQaOut,
    MessageCreateIn,
    MessageOut,
    TransformCreateIn,
    TransformOut,
)
from recipe_normalizer.api_deps import get_current_user, limit_by_user
from recipe_normalizer.db import get_db

router = APIRouter(prefix="/api/ai", tags=["ai"])

#: 60 chat turns/hour/user — see the phase plan's "Cost & abuse" global
#: constraint (every AI endpoint is rate-limited on top of the per-request
#: LLM cost cap).
_chat_limit = limit_by_user("ai_chat", 60, 3600.0)

#: 100 conversation creates/hour/user — modest limit for conversation creation
#: to prevent unbounded empty conversation rows per the phase plan spec.
_conversation_create_limit = limit_by_user("ai_conversation_create", 100, 3600.0)

#: Same shape as `_chat_limit` — a separate bucket (own name, own counter) so
#: heavy recipe-chat use doesn't eat into a user's cookbook Q&A allowance or
#: vice versa.
_cookbook_qa_limit = limit_by_user("ai_cookbook_qa", 60, 3600.0)

#: Tighter than chat/Q&A — transforms are the most expensive ai call (a full
#: structured-output pass plus catalog matching over a whole recipe), so the
#: phase plan calls for a tighter cap here.
_transform_limit = limit_by_user("ai_transform", 20, 3600.0)


@router.get("/conversations", response_model=list[ConversationOut])
def list_conversations(
    recipe_id: uuid.UUID | None = Query(default=None),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
    current_user: Any = Depends(get_current_user),  # noqa: B008
) -> list[ConversationOut]:
    return service.list_conversations(db, user_id=current_user.id, recipe_id=recipe_id)


@router.post(
    "/conversations",
    status_code=201,
    response_model=ConversationOut,
    dependencies=[Depends(_conversation_create_limit)],
)
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


@router.post(
    "/conversations/{conversation_id}/messages",
    status_code=201,
    response_model=MessageOut,
    dependencies=[Depends(_chat_limit)],
)
def post_chat_message(
    conversation_id: uuid.UUID,
    body: MessageCreateIn,
    db: Session = Depends(get_db),  # noqa: B008
    current_user: Any = Depends(get_current_user),  # noqa: B008
) -> MessageOut:
    """Post a user message to a recipe-chat conversation; returns the assistant's reply.

    The LLM client is built per-request via `service.make_ai_llm` (a real,
    cost-capped `LLMClient` unless `RN_LLM_STUB=1`) — never constructed ad hoc
    here, so the stub swap point stays centralized in one place.
    """
    llm = service.make_ai_llm(db, user_id=current_user.id, feature="ai.recipe_chat")
    return service.chat_turn(
        db,
        user_id=current_user.id,
        conversation_id=conversation_id,
        content=body.content,
        llm=llm,
    )


@router.post(
    "/cookbook-qa",
    status_code=201,
    response_model=CookbookQaOut,
    dependencies=[Depends(_cookbook_qa_limit)],
)
def post_cookbook_qa(
    body: CookbookQaCreateIn,
    db: Session = Depends(get_db),  # noqa: B008
    current_user: Any = Depends(get_current_user),  # noqa: B008
) -> CookbookQaOut:
    """Ask a question over the user's own cookbook; the model searches via tool-use.

    Returns the assistant `Message` plus `referenced_recipe_ids` — the ids of
    every recipe `search_recipes` returned during this turn, for the UI to
    render as clickable chips (see `ai.service.cookbook_qa_turn`).
    """
    llm = service.make_ai_llm(db, user_id=current_user.id, feature="ai.cookbook_qa")
    return service.cookbook_qa_turn(
        db,
        user_id=current_user.id,
        content=body.content,
        conversation_id=body.conversation_id,
        llm=llm,
    )


@router.post(
    "/recipes/{recipe_id}/transform",
    status_code=201,
    response_model=TransformOut,
    dependencies=[Depends(_transform_limit)],
)
def post_transform_recipe(
    recipe_id: uuid.UUID,
    body: TransformCreateIn,
    db: Session = Depends(get_db),  # noqa: B008
    current_user: Any = Depends(get_current_user),  # noqa: B008
) -> TransformOut:
    """Apply a qualitative instruction to a recipe; the result lands in the review gate.

    Returns `{job_id, recipe_id}` — NOT the draft recipe itself. There is deliberately no
    new review UI for this: `job_id` is the SAME kind of id `GET /api/jobs/{job_id}` (the
    existing Inbox/Review screen) already handles, and the caller is expected to navigate
    there (or straight to `recipe_id`) exactly as it would after any other extraction job
    reaches `needs_review`. See `ai.service.transform_recipe`'s docstring for the scaling
    boundary (a pure "halve it"/"double it" instruction is refused here with a 422 before
    any LLM call, pointing at the deterministic `/api/recipes/{recipe_id}/scaled` endpoint).
    """
    llm = service.make_ai_llm(db, user_id=current_user.id, feature="ai.transform")
    return service.transform_recipe(
        db,
        user_id=current_user.id,
        recipe_id=recipe_id,
        instruction=body.instruction,
        llm=llm,
    )
