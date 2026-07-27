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
import re
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, TypeVar, cast

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from recipe_normalizer.ai.models import Conversation, ConversationKind, Message, MessageRole
from recipe_normalizer.ai.prompts import (
    COOKBOOK_QA_SYSTEM,
    RECIPE_CHAT_SYSTEM,
    SEARCH_RECIPES_TOOL,
    TRANSFORM_SYSTEM,
)
from recipe_normalizer.ai.schemas import (
    ConversationDetailOut,
    ConversationOut,
    CookbookQaOut,
    MessageOut,
    TransformOut,
)
from recipe_normalizer.config import settings
from recipe_normalizer.cookbook import service as cookbook_service
from recipe_normalizer.cookbook.schemas import RecipeOut
from recipe_normalizer.cookbook.service import SourceType
from recipe_normalizer.errors import ApiError
from recipe_normalizer.extraction.normalize import (
    NormalizedGroup,
    NormalizedLine,
    NormalizedRecipe,
    NormalizedStep,
    NormalizeResult,
)
from recipe_normalizer.extraction.persist import persist_drafts
from recipe_normalizer.ingestion import service as ingestion_service
from recipe_normalizer.llm.client import (
    BudgetExceeded,
    CostCapExceeded,
    DbUsageRecorder,
    LLMClient,
    LLMError,
)

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

__all__ = [
    "MAX_HISTORY_MESSAGES_FOR_CONTEXT",
    "MAX_MESSAGES_PER_CONVERSATION",
    "append_message",
    "chat_turn",
    "cookbook_qa_turn",
    "create_conversation",
    "enforce_message_cap",
    "get_conversation",
    "list_conversations",
    "make_ai_llm",
    "transform_recipe",
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
    recipe — checked via `cookbook_service.recipe_access` (the SAME
    cookbook-derived check `cookbook.service.get_recipe` uses: owner, member,
    or public/unlisted viewer of the recipe's cookbook), 404 otherwise.
    Validating an incoming recipe id before it's used as a foreign key both
    keeps "which recipes can I start a conversation about" identical to "which
    recipes can I read", and avoids an unhandled `IntegrityError` (FK
    violation) for a nonexistent recipe id surfacing as a 500 — this way it's
    a clean 404 instead.

    A recipe_id of ``None`` is valid (a cookbook-wide Q&A conversation) and
    skips this check entirely.

    Flushes; caller owns commit.
    """
    if recipe_id is not None and (
        cookbook_service.recipe_access(db, user_id=user_id, recipe_id=recipe_id) is None
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
        `cookbook_service.recipe_access` (404 if access has since been
        lost — e.g. the caller was removed from the cookbook holding it, or
        the recipe was moved to a cookbook they can't read, after the
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
    if cookbook_service.recipe_access(db, user_id=user_id, recipe_id=detail.recipe_id) is None:
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
# Cookbook Q&A (Phase 3 Task 4) — tool-use over search, never the full cookbook
# ---------------------------------------------------------------------------

#: Hard cap on how many recipes a single `search_recipes` call may return,
#: regardless of what the model asks for (the tool schema has no `limit`
#: field at all — this is enforced entirely server-side) — bounds tokens per
#: the phase plan's "compact JSON, not full recipes" instruction.
_COOKBOOK_QA_SEARCH_LIMIT = 20

#: How many ingredient names ride along per recipe in a search result — a
#: taste of what's in the recipe, not the full ingredient list (that would
#: defeat the point of returning a compact summary).
_COOKBOOK_QA_TOP_INGREDIENTS = 5

#: Tool loops are bounded much tighter than the general-purpose default
#: (`LLMClient.tool_loop`'s `max_iterations=15`) — a Q&A turn is "search a
#: couple of times, then answer," not an open-ended agentic task.
_COOKBOOK_QA_MAX_ITERATIONS = 6


def _compact_recipe_for_search_result(recipe: RecipeOut) -> dict[str, Any]:
    """Build the compact per-recipe JSON `search_recipes` returns to the model.

    Deliberately NOT `RecipeOut` or even `RecipeSummary` — a handful of
    identifying fields plus a taste of the ingredients, never the full
    ingredient lines (quantities/units/notes) or steps. This is what keeps a
    multi-recipe search result small enough to bound tokens/cost across a
    tool loop, per the phase plan's "compact JSON per recipe ... NOT full
    recipes" instruction.
    """
    ingredient_names = [
        (line.name or line.original_text) for group in recipe.groups for line in group.lines
    ][:_COOKBOOK_QA_TOP_INGREDIENTS]
    return {
        "id": str(recipe.id),
        "title": recipe.title,
        "cuisines": recipe.cuisines,
        "dish_types": recipe.dish_types,
        "total_min": recipe.total_min,
        "top_ingredients": ingredient_names,
    }


def _make_search_recipes_execute(
    db: Session, *, user_id: uuid.UUID, referenced_ids: list[uuid.UUID]
) -> Any:
    """Build the `execute` callback `cookbook_qa_turn` passes to `llm.tool_loop`.

    Scopes every search to *user_id*'s OWN cookbook via
    `cookbook_service.list_recipes(owner_id=user_id, ...)` — the same
    owner-only listing the cookbook's own search UI uses; shared-cookbook
    recipes aren't included (acceptable v1 per the phase plan). Every recipe
    id a search returns is appended to *referenced_ids* (mutated in place,
    duplicates and all — the caller dedupes) so `cookbook_qa_turn` can report
    which recipes the model actually saw, for the UI's "referenced recipes"
    chips.

    An unrecognised tool name or an `ApiError` from `list_recipes` (e.g. an
    invalid `dietary` value the model made up) is left to propagate as a
    plain exception — `LLMClient.tool_loop` catches it, turns it into an
    `is_error` tool_result, and feeds the message back to the model as
    feedback, so a bad tool call doesn't crash the whole turn.
    """

    def execute(name: str, args: dict[str, Any]) -> str:
        if name != "search_recipes":
            raise ValueError(f"unknown tool {name!r}")
        args = args or {}
        page = cookbook_service.list_recipes(
            db,
            owner_id=user_id,
            q=args.get("query"),
            cuisine=args.get("cuisine"),
            dish_type=args.get("dish_type"),
            tag=args.get("tag"),
            dietary=args.get("dietary"),
            max_total_min=args.get("max_total_min"),
            favorites=args.get("favorites"),
            limit=_COOKBOOK_QA_SEARCH_LIMIT,
        )
        results = []
        for summary in page.items:
            recipe = cookbook_service.get_recipe(db, owner_id=user_id, recipe_id=summary.id)
            results.append(_compact_recipe_for_search_result(recipe))
            referenced_ids.append(recipe.id)
        return json.dumps({"total_matches": page.total, "results": results})

    return execute


def _final_text(response: Any) -> str:
    """Extract the answer text from `tool_loop`'s final (`end_turn`) Message."""
    text = next((block.text for block in response.content if block.type == "text"), None)
    if text is None:
        raise LLMError("no text block in tool_loop's final response")
    return cast(str, text)


def cookbook_qa_turn(
    db: Session,
    *,
    user_id: uuid.UUID,
    content: str,
    conversation_id: uuid.UUID | None = None,
    llm: LLMClient,
) -> CookbookQaOut:
    """Run one cookbook Q&A turn: search the user's own cookbook via tool-use, persist the answer.

    Unlike `chat_turn` (grounded on one recipe's full JSON), this NEVER dumps
    the cookbook into context — the model must call `search_recipes` (see
    `ai.prompts.SEARCH_RECIPES_TOOL`) to find anything, and every search is
    scoped to *user_id*'s own recipes via `cookbook_service.list_recipes`.

    *conversation_id*, when given, must be an existing `kind=cookbook_qa`
    conversation owned by *user_id` (404 for missing/someone-else's, 422 for
    a `recipe_chat` conversation passed here by mistake). When omitted, a new
    `cookbook_qa` conversation is created — but only AFTER the LLM call
    succeeds (see below).

    Deliberately does NOT thread the conversation's prior messages into the
    model call the way `chat_turn` does: `LLMClient.tool_loop` takes a single
    opening `initial_content` turn, not a chat history array, and
    reconstructing prior assistant/tool_use/tool_result turns to prime a new
    loop is unnecessary complexity for v1 — each question is answered fresh
    off live search results (which may have changed since the last turn
    anyway). The conversation's stored transcript is still complete for the
    UI: `content` and the final answer are both persisted for
    `get_conversation` to return.

    On failure (`CostCapExceeded`, a tool-loop `BudgetExceeded`, or any other
    `LLMError`), NOTHING is persisted — not the conversation (when
    *conversation_id* was `None`, no row is created at all) and not either
    message — mirroring `chat_turn`'s "no partial exchange" contract exactly,
    just extended to conversation creation too, since here that's also a side
    effect of a successful call rather than a precondition of one.

    Flushes; caller owns commit.
    """
    existing_message_count = 0
    if conversation_id is not None:
        detail = get_conversation(db, user_id=user_id, conversation_id=conversation_id)
        if detail.kind != ConversationKind.cookbook_qa:
            raise ApiError(
                422,
                "not_cookbook_qa",
                "This conversation is not a cookbook Q&A conversation.",
            )
        existing_message_count = len(detail.messages)

    if existing_message_count + 2 > MAX_MESSAGES_PER_CONVERSATION:
        raise ApiError(
            422,
            "conversation_full",
            f"This conversation has reached its {MAX_MESSAGES_PER_CONVERSATION}-message limit.",
        )

    referenced_ids: list[uuid.UUID] = []
    execute = _make_search_recipes_execute(db, user_id=user_id, referenced_ids=referenced_ids)

    try:
        response = llm.tool_loop(
            feature="ai.cookbook_qa",
            system=COOKBOOK_QA_SYSTEM,
            tools=[SEARCH_RECIPES_TOOL],
            initial_content=[{"type": "text", "text": content}],
            execute=execute,
            max_iterations=_COOKBOOK_QA_MAX_ITERATIONS,
        )
        answer = _final_text(response)
    except CostCapExceeded as exc:
        raise ApiError(
            503,
            "cost_cap_exceeded",
            "This request would exceed the AI budget allowed for this call — try again shortly.",
        ) from exc
    except BudgetExceeded as exc:
        raise ApiError(
            503,
            "ai_tool_budget_exceeded",
            "This question needed more searching than allowed — try a narrower question.",
        ) from exc
    except LLMError as exc:
        raise ApiError(502, "llm_error", "The AI assistant could not produce an answer.") from exc

    if conversation_id is None:
        conversation_id = create_conversation(
            db, user_id=user_id, recipe_id=None, kind=ConversationKind.cookbook_qa
        ).id

    append_message(db, conversation_id=conversation_id, role=MessageRole.user, content=content)
    message = append_message(
        db, conversation_id=conversation_id, role=MessageRole.assistant, content=answer
    )
    return CookbookQaOut(
        conversation_id=conversation_id,
        message=message,
        referenced_recipe_ids=list(dict.fromkeys(referenced_ids)),
    )


# ---------------------------------------------------------------------------
# Transformations (Phase 3 Task 6) — routes through the EXISTING ingestion
# review gate rather than adding any new review UI.
#
# Integration choice (documented per the phase plan): `transform_recipe` runs
# the transform LLM call SYNCHRONOUSLY in the API request (like chat_turn /
# cookbook_qa_turn — this whole module runs in the API process, never the
# worker), converts its `NormalizeResult` into a draft via the SAME
# `extraction.persist.persist_drafts` the extraction worker uses, and then
# calls `ingestion.service.create_transform_job` to create a `Job` already in
# `needs_review` with `produced_recipe_ids` populated. That Job never touches
# the queue (`queue.claim_next` only claims `status == queued`), so the
# worker never sees it — but it is otherwise indistinguishable from a
# worker-produced job to the review surface: `GET /api/jobs`,
# `GET /api/jobs/{id}`, `accept_draft`/`reject_draft`, and
# `accept_all_high_confidence` all work on it completely unmodified. This
# was preferred over the alternative (a bare unverified recipe + a bespoke
# redirect) because it reuses 100% of the existing Inbox/Review UI and
# accept/reject bookkeeping with zero new endpoints on the read/accept side —
# only this one write path needed to change.
# ---------------------------------------------------------------------------

#: Phrasings that are ALWAYS a pure quantity-scale request and must therefore
#: be steered to the deterministic `/recipes/{id}/scaled` endpoint WITHOUT
#: spending an LLM call — see the phase plan's mandatory "scaling boundary".
#: Deliberately anchored (`^...$`) so it only matches when scaling is the
#: WHOLE instruction: "double the recipe" matches (pure scale), but "double
#: the recipe and make it vegan" does NOT (mixed/ambiguous — per the plan,
#: ambiguous instructions are ALLOWED through to the transform LLM rather
#: than guessed at here).
_PURE_SCALE_RE = re.compile(
    r"""^\s*
    (?:please\s+)?
    (?:
        (?:cut|reduce)(?:\s+(?:it|this))?\s*(?:in\s+)?half
        | (?:cut|reduce)(?:\s+(?:it|this))?\s+by\s+half
        | half\s+(?:it|this)?
        | halve(?:\s+(?:it|this))?
        | double(?:\s+(?:it|this|the\s+recipe))?
        | triple(?:\s+(?:it|this|the\s+recipe))?
        | quadruple(?:\s+(?:it|this|the\s+recipe))?
        | (?:scale|resize)\s*(?:it|this)?\s*(?:to|by|for)?\s*\d+(?:\.\d+)?\s*x?
        | [x×]\s*\d+(?:\.\d+)?
        | \d+(?:\.\d+)?\s*[x×]
        | (?:make|scale|resize)?\s*(?:it|this)?\s*for\s+\d+\s*(?:servings?|people|portions?|guests?)
    )
    \s*(?:of\s+the\s+recipe|the\s+recipe)?
    \s*\.?\s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _is_pure_scaling_instruction(instruction: str) -> bool:
    """True when *instruction* is ENTIRELY a quantity-scale request.

    See `_PURE_SCALE_RE` — this is intentionally conservative (whole-string
    match): it only fires for instructions with no qualitative content at
    all ("halve it", "for 8 servings", "×3"), never for instructions that
    merely MENTION a multiplier alongside a real qualitative ask ("double it
    but make it vegan"), which fall through to the transform LLM per the
    phase plan's "when ambiguous, allow the transform" instruction.
    """
    return bool(_PURE_SCALE_RE.match(instruction))


def transform_recipe(
    db: Session,
    *,
    user_id: uuid.UUID,
    recipe_id: uuid.UUID,
    instruction: str,
    llm: LLMClient,
) -> TransformOut:
    """Apply a qualitative instruction to a recipe, landing the result in the review gate.

    Access is checked with the SAME `cookbook_service.recipe_access` every other
    per-recipe ai feature uses (404 for a missing recipe or one the caller can't read).

    **Scaling boundary (mandatory, see the phase plan):** a PURE quantity-scale
    instruction ("halve it", "double it", "for 8 servings", "×3") is refused BEFORE any
    LLM call — `_is_pure_scaling_instruction` — with a 422 pointing at the deterministic
    `/api/recipes/{recipe_id}/scaled` endpoint. This feature is for QUALITATIVE changes
    (substitutions, dietary adaptations, technique/equipment changes); scaling is code,
    not a model call, and must stay that way. Ambiguous instructions that merely mention
    a number alongside real qualitative content ("double it and make it vegan") are NOT
    treated as scaling — they're allowed through to the LLM, per the plan.

    On success: the LLM's `NormalizeResult` is validated (`is_recipe` + a non-empty
    `recipes` list, mirroring `extraction.normalize`'s contract) and converted into a
    draft via `extraction.persist.persist_drafts` — the SAME conversion/catalog-matching
    path the extraction worker uses, run inside a savepoint so a mid-persist failure
    (e.g. the cost cap tripping during catalog matching) leaves no half-built draft
    behind. The draft is stamped `derived_from=recipe_id` and a `provenance` dict
    recording the instruction and when it ran. Finally,
    `ingestion.service.create_transform_job` creates a `Job` already in `needs_review`
    referencing the produced draft(s) — see that function's docstring for why this is
    the least-invasive way to reuse the EXISTING review gate with zero new review UI.

    Flushes; caller owns commit (same convention as the rest of this module).
    """
    if cookbook_service.recipe_access(db, user_id=user_id, recipe_id=recipe_id) is None:
        raise ApiError(404, "not_found", f"Recipe {recipe_id} not found.")

    if _is_pure_scaling_instruction(instruction):
        raise ApiError(
            422,
            "use_scale_feature",
            "This looks like a quantity-only scaling request. Use the recipe's Scale "
            "feature for that — AI transform is for qualitative changes (substitutions, "
            "dietary adaptations, cooking method, etc.).",
            extra={"scale_endpoint": f"/api/recipes/{recipe_id}/scaled"},
        )

    recipe = cookbook_service.get_recipe(db, owner_id=user_id, recipe_id=recipe_id)

    system = TRANSFORM_SYSTEM.format(
        recipe_json=_recipe_grounding_json(recipe), instruction=instruction
    )
    try:
        result = llm.structured(
            feature="ai.transform",
            output_model=NormalizeResult,
            system=system,
            content=f"Instruction: {instruction}",
            max_tokens=8000,
        )
    except CostCapExceeded as exc:
        raise ApiError(
            503,
            "cost_cap_exceeded",
            "This request would exceed the AI budget allowed for this call — try again shortly.",
        ) from exc
    except LLMError as exc:
        raise ApiError(502, "llm_error", "The AI could not produce a transformed recipe.") from exc

    if not result.is_recipe or not result.recipes:
        raise ApiError(
            422,
            "invalid_transform_output",
            result.reason or "The AI could not apply this instruction to the recipe.",
        )

    at = datetime.now(UTC).isoformat()
    provenance: dict[str, Any] = {
        "transformed_from": str(recipe_id),
        "instruction": instruction,
        "at": at,
    }
    extraction_meta: dict[str, Any] = {
        "model": llm.model_for(),
        "confidence": result.confidence,
        "transformed_from": str(recipe_id),
        "instruction": instruction,
        "extracted_at": at,
    }
    # Already-established access to `recipe_id` (checked above) satisfies the trust
    # boundary `get_recipe_image_ref_unscoped` requires — it returns the RAW
    # content-addressed ref, unlike `recipe.image_ref` (already a `/api/files/...` URL).
    image_ref = cookbook_service.get_recipe_image_ref_unscoped(db, recipe_id)

    try:
        with db.begin_nested():  # savepoint: all drafts or none, mirrors worker.process_job
            produced_ids = persist_drafts(
                db,
                owner_id=user_id,
                result=result,
                llm=llm,
                source=f"AI transform of {recipe.title!r}: {instruction}",
                source_type=SourceType.transform,
                source_fingerprint=None,
                extraction_meta=extraction_meta,
                image_ref=image_ref,
                derived_from=recipe_id,
                provenance=provenance,
            )
    except CostCapExceeded as exc:
        raise ApiError(
            503,
            "cost_cap_exceeded",
            "This request would exceed the AI budget allowed for this call — try again shortly.",
        ) from exc
    except LLMError as exc:
        raise ApiError(502, "llm_error", "The AI could not complete the transformation.") from exc

    if not produced_ids:
        raise ApiError(
            422,
            "invalid_transform_output",
            "The transformed recipe had no usable ingredient lines.",
        )

    job = ingestion_service.create_transform_job(
        db,
        user_id=user_id,
        source_recipe_id=recipe_id,
        instruction=instruction,
        produced_recipe_ids=produced_ids,
        extraction_meta=extraction_meta,
        cost_usd=Decimal(str(llm.spent_usd)),
    )
    return TransformOut(job_id=job.id, recipe_id=produced_ids[0])


# ---------------------------------------------------------------------------
# LLM factory — RN_LLM_STUB=1 swaps in a canned client (TEST/E2E ONLY)
# ---------------------------------------------------------------------------


#: Fixed reply for the ai-module stub LLM — deliberately says something
#: recognizable so e2e assertions can key off it without depending on real
#: model output.
_STUB_CHAT_REPLY = "This is a stubbed AI reply (RN_LLM_STUB=1) — no real model was called."

#: Fixed reply for the stub's `tool_loop()` — distinct wording from the chat
#: reply so e2e assertions can tell the two features' stub output apart.
_STUB_COOKBOOK_QA_REPLY = (
    "This is a stubbed cookbook Q&A reply (RN_LLM_STUB=1) — no real model was called."
)

#: Title of the stub's `.structured()` NormalizeResult — distinct wording
#: (from both the chat/Q&A replies above AND `worker._stub_normalize_result`'s
#: "Stub Recipe") so e2e assertions can tell a transform's stub output apart
#: from an extraction's.
_STUB_TRANSFORM_TITLE = "Stub Transformed Recipe (RN_LLM_STUB=1)"


def _stub_transform_result() -> NormalizeResult:
    return NormalizeResult(
        is_recipe=True,
        confidence=0.9,
        recipes=[
            NormalizedRecipe(
                title=_STUB_TRANSFORM_TITLE,
                groups=[
                    NormalizedGroup(
                        name=None,
                        lines=[
                            NormalizedLine(
                                original_text="1 cup stubbed substitute ingredient",
                                name="stubbed substitute ingredient",
                                quantity=1.0,
                                unit="cup",
                            ),
                        ],
                    )
                ],
                steps=[NormalizedStep(original_text="Mix the transformed ingredients and bake.")],
            )
        ],
    )


class _StubAiLLMClient:
    """Deterministic fake LLM for ai chat features, enabled ONLY by RN_LLM_STUB=1.

    *** TEST / E2E ONLY — NEVER ENABLE IN PRODUCTION. ***

    Mirrors `worker._StubLLMClient`'s env convention (same `RN_LLM_STUB=1`
    flag, same warning-on-use, same "TEST/E2E ONLY" contract) but for the ai
    module's `.chat()`/`.tool_loop()`/`.structured()` surfaces rather than
    extraction's structured/classify_bool/tool_loop. Answers any call with a
    fixed reply, makes zero API calls, and spends $0.
    """

    @property
    def spent_usd(self) -> float:
        return 0.0

    def model_for(self, *, fast: bool = False) -> str:
        return "stub"

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

    def structured(
        self,
        *,
        feature: str,
        output_model: type[T],
        content: str | list[dict[str, Any]],
        system: str | None = None,
        model: str | None = None,
        fast: bool = False,
        max_tokens: int = 8000,
    ) -> T:
        """Serves `transform_recipe`'s `NormalizeResult` call, and — should a transform's
        draft need catalog-match disambiguation via `persist_drafts` — the same blank
        "no match" answer `worker._StubLLMClient` gives (`index=None`, i.e. fall through
        to `create_unreviewed`), so the stub never crashes mid-persist under e2e.
        """
        if output_model is NormalizeResult:
            return cast(T, _stub_transform_result())
        if "index" in output_model.model_fields:  # catalog.match band choice
            return output_model(index=None)
        raise NotImplementedError(f"stub AI LLM cannot answer feature {feature!r}")

    def tool_loop(
        self,
        *,
        feature: str,
        system: str,
        tools: list[dict[str, Any]],
        initial_content: list[dict[str, Any]],
        execute: Callable[[str, dict[str, Any]], str | list[dict[str, Any]]],
        max_iterations: int = 15,
        max_tokens: int = 8000,
    ) -> Any:
        """Call `execute` once (an empty `search_recipes`) then return a canned reply.

        Exercises the real `execute` callback — and therefore the real
        `cookbook_service.list_recipes` scoping — even under the stub, so an
        e2e run still proves the tool wiring works end-to-end; only the
        model's own text is canned.
        """
        execute("search_recipes", {})
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=_STUB_COOKBOOK_QA_REPLY)],
            stop_reason="end_turn",
        )


def make_ai_llm(db: Session, *, user_id: uuid.UUID, feature: str) -> LLMClient:
    """Build the per-request LLM client for an ai endpoint.

    `RN_LLM_STUB=1` (env) swaps in `_StubAiLLMClient` — TEST/E2E ONLY, mirrors
    `worker.make_llm_for_job`'s convention exactly (same env var, same
    warning) so e2e runs can exercise recipe chat (and, in later tasks,
    cookbook Q&A / transformations) without an LLM API key.

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
