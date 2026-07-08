"""Service-level tests for ai.service: conversation + message storage."""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from recipe_normalizer.ai import service as ai_service
from recipe_normalizer.ai.models import Conversation, ConversationKind, Message, MessageRole
from recipe_normalizer.ai.prompts import SEARCH_RECIPES_TOOL
from recipe_normalizer.ai.schemas import ConversationOut
from recipe_normalizer.cookbook import service as cookbook_service
from recipe_normalizer.cookbook.schemas import (
    IngredientGroupIn,
    IngredientLineIn,
    RecipeIn,
    RecipeOut,
)
from recipe_normalizer.errors import ApiError
from recipe_normalizer.llm.client import CostCapExceeded, DbUsageRecorder, LLMClient, LLMError
from recipe_normalizer.llm.models import LlmUsage

# Importing sharing.service self-registers the shared-cookbook membership
# checker into cookbook_service (bottom-of-module call, see worker.py's own
# comment on the same idiom) — needed for the access-lost test below, which
# grants `other_user` access via shared-cookbook membership rather than
# ownership.
from recipe_normalizer.sharing import service as sharing_service
from recipe_normalizer.users.models import User
from tests.ai.fakes import FakeChatLLM
from tests.llm.stubs import (
    StubAnthropicClient,
    message_response,
    text_block,
    text_response,
    tool_use_block,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_user(db: Session, suffix: str = "") -> User:
    user = User(
        email=f"chef{suffix}@example.com",
        password_hash="hash",
        display_name=f"Chef{suffix}",
    )
    db.add(user)
    db.flush()
    return user


def _simple_recipe_in(title: str = "Test Cake") -> RecipeIn:
    return RecipeIn(
        title=title,
        groups=[IngredientGroupIn(name="Main", lines=[IngredientLineIn(original_text="salt")])],
    )


def _create_recipe(db: Session, owner_id: uuid.UUID, title: str = "Test Cake") -> RecipeOut:
    return cookbook_service.create_recipe(db, owner_id=owner_id, data=_simple_recipe_in(title))


@pytest.fixture()
def owner(db_session: Session) -> User:
    return make_user(db_session, suffix=str(uuid.uuid4())[:8])


@pytest.fixture()
def other_user(db_session: Session) -> User:
    return make_user(db_session, suffix=str(uuid.uuid4())[:8])


# ---------------------------------------------------------------------------
# create_conversation
# ---------------------------------------------------------------------------


def test_create_conversation_recipe_chat_happy_path(db_session: Session, owner: User) -> None:
    recipe = _create_recipe(db_session, owner.id)

    out = ai_service.create_conversation(
        db_session,
        user_id=owner.id,
        recipe_id=recipe.id,
        kind=ConversationKind.recipe_chat,
        title="About this cake",
    )

    assert out.id is not None
    assert out.user_id == owner.id
    assert out.recipe_id == recipe.id
    assert out.kind == ConversationKind.recipe_chat
    assert out.title == "About this cake"
    assert out.created_at is not None
    assert out.updated_at is not None


def test_create_conversation_cookbook_qa_has_no_recipe(db_session: Session, owner: User) -> None:
    out = ai_service.create_conversation(
        db_session, user_id=owner.id, recipe_id=None, kind=ConversationKind.cookbook_qa
    )
    assert out.recipe_id is None
    assert out.kind == ConversationKind.cookbook_qa


def test_create_conversation_missing_recipe_raises_404(db_session: Session, owner: User) -> None:
    with pytest.raises(ApiError) as exc_info:
        ai_service.create_conversation(
            db_session,
            user_id=owner.id,
            recipe_id=uuid.uuid4(),
            kind=ConversationKind.recipe_chat,
        )
    assert exc_info.value.status_code == 404


def test_create_conversation_foreign_recipe_raises_404(
    db_session: Session, owner: User, other_user: User
) -> None:
    foreign_recipe = _create_recipe(db_session, other_user.id)
    with pytest.raises(ApiError) as exc_info:
        ai_service.create_conversation(
            db_session,
            user_id=owner.id,
            recipe_id=foreign_recipe.id,
            kind=ConversationKind.recipe_chat,
        )
    assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# get_conversation
# ---------------------------------------------------------------------------


def test_get_conversation_returns_messages_in_chronological_order(
    db_session: Session, owner: User
) -> None:
    recipe = _create_recipe(db_session, owner.id)
    conversation = ai_service.create_conversation(
        db_session, user_id=owner.id, recipe_id=recipe.id, kind=ConversationKind.recipe_chat
    )
    ai_service.append_message(
        db_session,
        conversation_id=conversation.id,
        role=MessageRole.user,
        content="How long do I bake this?",
    )
    ai_service.append_message(
        db_session,
        conversation_id=conversation.id,
        role=MessageRole.assistant,
        content="30 minutes.",
    )

    detail = ai_service.get_conversation(
        db_session, user_id=owner.id, conversation_id=conversation.id
    )
    assert [m.role for m in detail.messages] == [MessageRole.user, MessageRole.assistant]
    assert [m.content for m in detail.messages] == ["How long do I bake this?", "30 minutes."]


def test_get_conversation_wrong_owner_raises_404(
    db_session: Session, owner: User, other_user: User
) -> None:
    conversation = ai_service.create_conversation(
        db_session, user_id=owner.id, recipe_id=None, kind=ConversationKind.cookbook_qa
    )
    with pytest.raises(ApiError) as exc_info:
        ai_service.get_conversation(
            db_session, user_id=other_user.id, conversation_id=conversation.id
        )
    assert exc_info.value.status_code == 404


def test_get_conversation_missing_raises_404(db_session: Session, owner: User) -> None:
    with pytest.raises(ApiError) as exc_info:
        ai_service.get_conversation(db_session, user_id=owner.id, conversation_id=uuid.uuid4())
    assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# list_conversations
# ---------------------------------------------------------------------------


def test_list_conversations_is_owner_scoped(
    db_session: Session, owner: User, other_user: User
) -> None:
    mine = ai_service.create_conversation(
        db_session, user_id=owner.id, recipe_id=None, kind=ConversationKind.cookbook_qa
    )
    ai_service.create_conversation(
        db_session, user_id=other_user.id, recipe_id=None, kind=ConversationKind.cookbook_qa
    )

    out = ai_service.list_conversations(db_session, user_id=owner.id)
    assert [c.id for c in out] == [mine.id]


def test_list_conversations_filtered_by_recipe_id(db_session: Session, owner: User) -> None:
    recipe_a = _create_recipe(db_session, owner.id, title="Cake A")
    recipe_b = _create_recipe(db_session, owner.id, title="Cake B")
    conv_a = ai_service.create_conversation(
        db_session, user_id=owner.id, recipe_id=recipe_a.id, kind=ConversationKind.recipe_chat
    )
    ai_service.create_conversation(
        db_session, user_id=owner.id, recipe_id=recipe_b.id, kind=ConversationKind.recipe_chat
    )

    out = ai_service.list_conversations(db_session, user_id=owner.id, recipe_id=recipe_a.id)
    assert [c.id for c in out] == [conv_a.id]


def test_list_conversations_orders_most_recently_active_first(
    db_session: Session, owner: User
) -> None:
    first = ai_service.create_conversation(
        db_session, user_id=owner.id, recipe_id=None, kind=ConversationKind.cookbook_qa
    )
    second = ai_service.create_conversation(
        db_session, user_id=owner.id, recipe_id=None, kind=ConversationKind.cookbook_qa
    )
    # Touch `first` after `second` was created — it should now sort first.
    ai_service.append_message(
        db_session, conversation_id=first.id, role=MessageRole.user, content="hi"
    )

    out = ai_service.list_conversations(db_session, user_id=owner.id)
    assert [c.id for c in out] == [first.id, second.id]


# ---------------------------------------------------------------------------
# append_message / enforce_message_cap
# ---------------------------------------------------------------------------


def test_append_message_enforces_cap(db_session: Session, owner: User) -> None:
    conversation = ai_service.create_conversation(
        db_session, user_id=owner.id, recipe_id=None, kind=ConversationKind.cookbook_qa
    )
    for i in range(ai_service.MAX_MESSAGES_PER_CONVERSATION):
        ai_service.append_message(
            db_session,
            conversation_id=conversation.id,
            role=MessageRole.user if i % 2 == 0 else MessageRole.assistant,
            content=f"message {i}",
        )

    with pytest.raises(ApiError) as exc_info:
        ai_service.append_message(
            db_session,
            conversation_id=conversation.id,
            role=MessageRole.user,
            content="one too many",
        )
    assert exc_info.value.status_code == 422
    assert exc_info.value.code == "conversation_full"

    rows = db_session.scalars(
        select(Message).where(Message.conversation_id == conversation.id)
    ).all()
    assert len(rows) == ai_service.MAX_MESSAGES_PER_CONVERSATION


# ---------------------------------------------------------------------------
# Cascade behavior
# ---------------------------------------------------------------------------


def test_deleting_recipe_cascades_to_conversation(db_session: Session, owner: User) -> None:
    recipe = _create_recipe(db_session, owner.id)
    conversation = ai_service.create_conversation(
        db_session, user_id=owner.id, recipe_id=recipe.id, kind=ConversationKind.recipe_chat
    )
    ai_service.append_message(
        db_session, conversation_id=conversation.id, role=MessageRole.user, content="hi"
    )

    cookbook_service.delete_recipe(db_session, owner_id=owner.id, recipe_id=recipe.id)
    db_session.flush()

    assert db_session.get(Conversation, conversation.id) is None
    remaining_messages = db_session.scalars(
        select(Message).where(Message.conversation_id == conversation.id)
    ).all()
    assert remaining_messages == []


def test_deleting_conversation_cascades_to_messages(db_session: Session, owner: User) -> None:
    conversation = ai_service.create_conversation(
        db_session, user_id=owner.id, recipe_id=None, kind=ConversationKind.cookbook_qa
    )
    ai_service.append_message(
        db_session, conversation_id=conversation.id, role=MessageRole.user, content="hi"
    )
    ai_service.append_message(
        db_session, conversation_id=conversation.id, role=MessageRole.assistant, content="hello"
    )

    row = db_session.get(Conversation, conversation.id)
    assert row is not None
    db_session.delete(row)
    db_session.flush()

    remaining_messages = db_session.scalars(
        select(Message).where(Message.conversation_id == conversation.id)
    ).all()
    assert remaining_messages == []


# ---------------------------------------------------------------------------
# chat_turn
# ---------------------------------------------------------------------------


def _recipe_chat_conversation(
    db: Session, user_id: uuid.UUID, title: str = "Test Cake"
) -> ConversationOut:
    recipe = _create_recipe(db, user_id, title=title)
    return ai_service.create_conversation(
        db, user_id=user_id, recipe_id=recipe.id, kind=ConversationKind.recipe_chat
    )


def test_chat_turn_grounded_answer_persists_both_messages(db_session: Session, owner: User) -> None:
    conversation = _recipe_chat_conversation(db_session, owner.id)
    fake = FakeChatLLM(reply="Bake at 350F for 30 minutes.")

    result = ai_service.chat_turn(
        db_session,
        user_id=owner.id,
        conversation_id=conversation.id,
        content="How long do I bake this?",
        llm=fake,  # type: ignore[arg-type]
    )

    assert result.role == MessageRole.assistant
    assert result.content == "Bake at 350F for 30 minutes."

    detail = ai_service.get_conversation(
        db_session, user_id=owner.id, conversation_id=conversation.id
    )
    assert [m.role for m in detail.messages] == [MessageRole.user, MessageRole.assistant]
    assert detail.messages[0].content == "How long do I bake this?"
    assert detail.messages[1].content == "Bake at 350F for 30 minutes."

    [call] = fake.calls
    assert call["feature"] == "ai.recipe_chat"
    assert "Test Cake" in call["system"]  # grounded on the recipe's JSON
    assert call["messages"][-1] == {"role": "user", "content": "How long do I bake this?"}


def test_chat_turn_stores_a_plain_i_dont_know_answer_without_raising(
    db_session: Session, owner: User
) -> None:
    # A model saying "this recipe doesn't say" in ordinary prose is the
    # CORRECT grounded, no-fabrication behavior — it must be stored like any
    # other answer, not treated as an error.
    conversation = _recipe_chat_conversation(db_session, owner.id)
    fake = FakeChatLLM(reply="This recipe doesn't say what oven temperature to use.")

    result = ai_service.chat_turn(
        db_session,
        user_id=owner.id,
        conversation_id=conversation.id,
        content="What oven temperature?",
        llm=fake,  # type: ignore[arg-type]
    )

    assert result.content == "This recipe doesn't say what oven temperature to use."


def test_chat_turn_rejects_cookbook_qa_conversation(db_session: Session, owner: User) -> None:
    conversation = ai_service.create_conversation(
        db_session, user_id=owner.id, recipe_id=None, kind=ConversationKind.cookbook_qa
    )
    fake = FakeChatLLM(reply="n/a")

    with pytest.raises(ApiError) as exc_info:
        ai_service.chat_turn(
            db_session,
            user_id=owner.id,
            conversation_id=conversation.id,
            content="hi",
            llm=fake,  # type: ignore[arg-type]
        )

    assert exc_info.value.status_code == 422
    assert fake.calls == []


def test_chat_turn_wrong_owner_raises_404(
    db_session: Session, owner: User, other_user: User
) -> None:
    conversation = _recipe_chat_conversation(db_session, owner.id)
    fake = FakeChatLLM(reply="n/a")

    with pytest.raises(ApiError) as exc_info:
        ai_service.chat_turn(
            db_session,
            user_id=other_user.id,
            conversation_id=conversation.id,
            content="hi",
            llm=fake,  # type: ignore[arg-type]
        )

    assert exc_info.value.status_code == 404
    assert fake.calls == []


def test_chat_turn_access_lost_after_shared_cookbook_removal_raises_404(
    db_session: Session, owner: User, other_user: User
) -> None:
    recipe = _create_recipe(db_session, owner.id)
    cookbook = sharing_service.create_shared_cookbook(
        db_session, creator_id=owner.id, name="Friends"
    )
    sharing_service.invite_member(
        db_session, user_id=owner.id, cookbook_id=cookbook.id, email=other_user.email
    )
    sharing_service.add_recipe_to_shared_cookbook(
        db_session, user_id=owner.id, cookbook_id=cookbook.id, recipe_id=recipe.id
    )
    conversation = ai_service.create_conversation(
        db_session, user_id=other_user.id, recipe_id=recipe.id, kind=ConversationKind.recipe_chat
    )

    # Access still holds — the member can chat.
    ai_service.chat_turn(
        db_session,
        user_id=other_user.id,
        conversation_id=conversation.id,
        content="hi",
        llm=FakeChatLLM(reply="First answer."),  # type: ignore[arg-type]
    )

    sharing_service.remove_member(
        db_session, user_id=owner.id, cookbook_id=cookbook.id, target_user_id=other_user.id
    )

    # The conversation row (and its FK to the still-existing recipe) is
    # unchanged, but current access is gone — re-checked on every turn.
    fake = FakeChatLLM(reply="n/a")
    with pytest.raises(ApiError) as exc_info:
        ai_service.chat_turn(
            db_session,
            user_id=other_user.id,
            conversation_id=conversation.id,
            content="hi again",
            llm=fake,  # type: ignore[arg-type]
        )

    assert exc_info.value.status_code == 404
    assert fake.calls == []


def test_chat_turn_cost_cap_exceeded_maps_to_api_error_and_persists_nothing(
    db_session: Session, owner: User
) -> None:
    conversation = _recipe_chat_conversation(db_session, owner.id)
    fake = FakeChatLLM(raises=CostCapExceeded("budget exhausted"))

    with pytest.raises(ApiError) as exc_info:
        ai_service.chat_turn(
            db_session,
            user_id=owner.id,
            conversation_id=conversation.id,
            content="hi",
            llm=fake,  # type: ignore[arg-type]
        )

    assert exc_info.value.status_code == 503
    assert exc_info.value.code == "cost_cap_exceeded"

    detail = ai_service.get_conversation(
        db_session, user_id=owner.id, conversation_id=conversation.id
    )
    assert detail.messages == []


def test_chat_turn_llm_error_maps_to_502(db_session: Session, owner: User) -> None:
    conversation = _recipe_chat_conversation(db_session, owner.id)
    fake = FakeChatLLM(raises=LLMError("model refused"))

    with pytest.raises(ApiError) as exc_info:
        ai_service.chat_turn(
            db_session,
            user_id=owner.id,
            conversation_id=conversation.id,
            content="hi",
            llm=fake,  # type: ignore[arg-type]
        )

    assert exc_info.value.status_code == 502
    assert exc_info.value.code == "llm_error"


def test_chat_turn_message_cap_rejects_before_calling_the_llm(
    db_session: Session, owner: User
) -> None:
    conversation = _recipe_chat_conversation(db_session, owner.id)
    # One below the storage cap: the next turn would add 2 (user + assistant),
    # which would exceed it — rejected before any LLM call is made.
    for i in range(ai_service.MAX_MESSAGES_PER_CONVERSATION - 1):
        ai_service.append_message(
            db_session,
            conversation_id=conversation.id,
            role=MessageRole.user if i % 2 == 0 else MessageRole.assistant,
            content=f"message {i}",
        )
    fake = FakeChatLLM(reply="should never be sent")

    with pytest.raises(ApiError) as exc_info:
        ai_service.chat_turn(
            db_session,
            user_id=owner.id,
            conversation_id=conversation.id,
            content="one more",
            llm=fake,  # type: ignore[arg-type]
        )

    assert exc_info.value.status_code == 422
    assert exc_info.value.code == "conversation_full"
    assert fake.calls == []


def test_chat_turn_bounds_history_sent_to_the_model(db_session: Session, owner: User) -> None:
    conversation = _recipe_chat_conversation(db_session, owner.id)
    # More prior messages than the context bound, but well under the storage cap.
    total_prior = ai_service.MAX_HISTORY_MESSAGES_FOR_CONTEXT + 6
    for i in range(total_prior):
        ai_service.append_message(
            db_session,
            conversation_id=conversation.id,
            role=MessageRole.user if i % 2 == 0 else MessageRole.assistant,
            content=f"message {i}",
        )
    fake = FakeChatLLM(reply="ok")

    ai_service.chat_turn(
        db_session,
        user_id=owner.id,
        conversation_id=conversation.id,
        content="latest",
        llm=fake,  # type: ignore[arg-type]
    )

    [call] = fake.calls
    # the bounded prior slice, plus this turn's message appended in-memory
    assert len(call["messages"]) == ai_service.MAX_HISTORY_MESSAGES_FOR_CONTEXT + 1
    assert call["messages"][-1] == {"role": "user", "content": "latest"}
    assert call["messages"][0]["content"] != "message 0"  # earliest fell outside the window


def test_chat_turn_records_llm_usage(db_session: Session, owner: User) -> None:
    conversation = _recipe_chat_conversation(db_session, owner.id)
    stub_anthropic = StubAnthropicClient(create_results=[text_response("Answer.")])
    llm = LLMClient(
        recorder=DbUsageRecorder(db_session, user_id=owner.id), anthropic_client=stub_anthropic
    )

    ai_service.chat_turn(
        db_session,
        user_id=owner.id,
        conversation_id=conversation.id,
        content="hi",
        llm=llm,
    )

    usage_rows = db_session.scalars(select(LlmUsage).where(LlmUsage.user_id == owner.id)).all()
    assert len(usage_rows) == 1


# ---------------------------------------------------------------------------
# cookbook_qa_turn
#
# Unlike chat_turn's FakeChatLLM, these exercise the REAL `LLMClient.tool_loop`
# by injecting a scripted `StubAnthropicClient` — the anthropic SDK is faked,
# not LLMClient itself, so `execute()`'s real dispatch into
# `cookbook_service.list_recipes` and the loop's own iteration/cost-cap logic
# both run for real. See tests/llm/stubs.py and tests/llm/test_client.py's
# tool_loop tests, which this mirrors.
# ---------------------------------------------------------------------------


def _recipe_in(
    title: str,
    *,
    cuisines: list[str] | None = None,
    dish_types: list[str] | None = None,
    total_min: int | None = None,
    ingredient_names: list[str] | None = None,
) -> RecipeIn:
    names = ingredient_names or ["salt"]
    return RecipeIn(
        title=title,
        cuisines=cuisines or [],
        dish_types=dish_types or [],
        total_min=total_min,
        groups=[
            IngredientGroupIn(
                name="Main",
                lines=[IngredientLineIn(original_text=name) for name in names],
            )
        ],
        steps=[],
    )


def _create_full_recipe(db: Session, owner_id: uuid.UUID, **kwargs: object) -> RecipeOut:
    return cookbook_service.create_recipe(db, owner_id=owner_id, data=_recipe_in(**kwargs))  # type: ignore[arg-type]


def _tool_use_then_answer(
    tool_input: dict[str, object], answer: str, **kwargs: object
) -> StubAnthropicClient:
    return StubAnthropicClient(
        create_results=[
            message_response([tool_use_block("toolu_1", "search_recipes", tool_input)], "tool_use"),
            message_response([text_block(answer)], "end_turn"),
        ]
    )


def test_cookbook_qa_turn_executes_search_scoped_to_user_with_parsed_filters(
    db_session: Session, owner: User, other_user: User
) -> None:
    pasta = _create_full_recipe(
        db_session,
        owner.id,
        title="Pasta Bolognese",
        cuisines=["Italian"],
        dish_types=["main"],
        total_min=25,
        ingredient_names=["ground beef", "tomato", "pasta"],
    )
    # Owned by someone else, same filter — must NOT leak into owner's search.
    _create_full_recipe(
        db_session,
        other_user.id,
        title="Someone Else's Lasagna",
        cuisines=["Italian"],
        total_min=20,
    )

    stub = _tool_use_then_answer(
        {"cuisine": "Italian", "max_total_min": 30},
        "You should try Pasta Bolognese — it's Italian and ready in 25 minutes.",
    )
    llm = LLMClient(anthropic_client=stub)

    result = ai_service.cookbook_qa_turn(
        db_session, user_id=owner.id, content="Quick Italian dishes?", llm=llm
    )

    assert result.message.role == MessageRole.assistant
    assert "Pasta Bolognese" in result.message.content
    assert result.referenced_recipe_ids == [pasta.id]

    first_call = stub.messages.create_calls[0]
    assert first_call["tools"] == [SEARCH_RECIPES_TOOL]
    assert first_call["system"] == ai_service.COOKBOOK_QA_SYSTEM

    # persisted: a new cookbook_qa conversation with the question + answer
    conversation_id = result.conversation_id
    detail = ai_service.get_conversation(
        db_session, user_id=owner.id, conversation_id=conversation_id
    )
    assert detail.kind == ConversationKind.cookbook_qa
    assert [m.role for m in detail.messages] == [MessageRole.user, MessageRole.assistant]
    assert detail.messages[0].content == "Quick Italian dishes?"


def test_cookbook_qa_turn_zero_results_still_answers(db_session: Session, owner: User) -> None:
    stub = _tool_use_then_answer(
        {"cuisine": "Klingon"}, "I couldn't find any matching recipes in your cookbook."
    )
    llm = LLMClient(anthropic_client=stub)

    result = ai_service.cookbook_qa_turn(
        db_session, user_id=owner.id, content="Any Klingon recipes?", llm=llm
    )

    assert "couldn't find" in result.message.content
    assert result.referenced_recipe_ids == []


def test_cookbook_qa_turn_search_results_are_compact_not_full_recipes(
    db_session: Session, owner: User
) -> None:
    _create_full_recipe(
        db_session,
        owner.id,
        title="Pasta Bolognese",
        cuisines=["Italian"],
        dish_types=["main"],
        total_min=25,
        ingredient_names=["ground beef", "tomato", "pasta", "onion", "garlic", "basil", "salt"],
    )
    stub = _tool_use_then_answer({"cuisine": "Italian"}, "Try Pasta Bolognese.")
    llm = LLMClient(anthropic_client=stub)

    ai_service.cookbook_qa_turn(db_session, user_id=owner.id, content="Italian?", llm=llm)

    # The tool_result content sent back to the model on the second create() call.
    second_call_messages = stub.messages.create_calls[1]["messages"]
    [tool_result] = second_call_messages[2]["content"]
    payload = json.loads(tool_result["content"])
    [compact] = payload["results"]

    assert set(compact.keys()) == {
        "id",
        "title",
        "cuisines",
        "dish_types",
        "total_min",
        "top_ingredients",
    }
    # Bounded to a handful of ingredient names, not the full 7-line list.
    assert len(compact["top_ingredients"]) == ai_service._COOKBOOK_QA_TOP_INGREDIENTS
    # No full ingredient-line detail (quantity/unit/note) or steps leak through.
    raw = tool_result["content"]
    assert "quantity" not in raw
    assert "steps" not in raw
    assert "original_text" not in raw


def test_cookbook_qa_turn_cost_cap_abort_persists_nothing(db_session: Session, owner: User) -> None:
    _create_full_recipe(db_session, owner.id, title="Pasta", cuisines=["Italian"])
    stub = StubAnthropicClient(
        create_results=[
            message_response([tool_use_block("toolu_1", "search_recipes", {})], "tool_use"),
            message_response([tool_use_block("toolu_2", "search_recipes", {})], "tool_use"),
        ]
    )
    # First round's default-usage cost (~$0.0175 at opus pricing) already
    # exceeds this cap, so the SECOND create() call is blocked before it
    # happens — the loop aborts mid-way, having executed one search.
    llm = LLMClient(anthropic_client=stub, cost_cap_usd=0.01)

    with pytest.raises(ApiError) as exc_info:
        ai_service.cookbook_qa_turn(
            db_session, user_id=owner.id, content="Italian dishes?", llm=llm
        )

    assert exc_info.value.status_code == 503
    assert exc_info.value.code == "cost_cap_exceeded"
    # only the first round's API call happened
    assert len(stub.messages.create_calls) == 1
    # NOTHING persisted — no conversation, no messages, matching chat_turn's
    # "no partial exchange" contract.
    assert ai_service.list_conversations(db_session, user_id=owner.id) == []


def test_cookbook_qa_turn_tool_budget_exceeded_maps_to_503_and_persists_nothing(
    db_session: Session, owner: User
) -> None:
    # Every round returns tool_use — the loop never reaches end_turn and
    # exhausts its (lowered) iteration budget.
    stub = StubAnthropicClient(
        create_results=[
            message_response([tool_use_block(f"toolu_{i}", "search_recipes", {})], "tool_use")
            for i in range(ai_service._COOKBOOK_QA_MAX_ITERATIONS)
        ]
    )
    llm = LLMClient(anthropic_client=stub)

    with pytest.raises(ApiError) as exc_info:
        ai_service.cookbook_qa_turn(db_session, user_id=owner.id, content="hi", llm=llm)

    assert exc_info.value.status_code == 503
    assert exc_info.value.code == "ai_tool_budget_exceeded"
    assert ai_service.list_conversations(db_session, user_id=owner.id) == []


def test_cookbook_qa_turn_appends_to_existing_conversation(
    db_session: Session, owner: User
) -> None:
    conversation = ai_service.create_conversation(
        db_session, user_id=owner.id, recipe_id=None, kind=ConversationKind.cookbook_qa
    )
    stub = _tool_use_then_answer({}, "First answer.")
    llm = LLMClient(anthropic_client=stub)

    result = ai_service.cookbook_qa_turn(
        db_session,
        user_id=owner.id,
        content="hi",
        conversation_id=conversation.id,
        llm=llm,
    )

    assert result.conversation_id == conversation.id
    detail = ai_service.get_conversation(
        db_session, user_id=owner.id, conversation_id=conversation.id
    )
    assert len(detail.messages) == 2


def test_cookbook_qa_turn_rejects_recipe_chat_conversation(
    db_session: Session, owner: User
) -> None:
    conversation = _recipe_chat_conversation(db_session, owner.id)
    stub = StubAnthropicClient(create_results=[])
    llm = LLMClient(anthropic_client=stub)

    with pytest.raises(ApiError) as exc_info:
        ai_service.cookbook_qa_turn(
            db_session,
            user_id=owner.id,
            content="hi",
            conversation_id=conversation.id,
            llm=llm,
        )

    assert exc_info.value.status_code == 422
    assert exc_info.value.code == "not_cookbook_qa"
    assert stub.messages.create_calls == []


def test_cookbook_qa_turn_wrong_owner_raises_404(
    db_session: Session, owner: User, other_user: User
) -> None:
    conversation = ai_service.create_conversation(
        db_session, user_id=owner.id, recipe_id=None, kind=ConversationKind.cookbook_qa
    )
    stub = StubAnthropicClient(create_results=[])
    llm = LLMClient(anthropic_client=stub)

    with pytest.raises(ApiError) as exc_info:
        ai_service.cookbook_qa_turn(
            db_session,
            user_id=other_user.id,
            content="hi",
            conversation_id=conversation.id,
            llm=llm,
        )

    assert exc_info.value.status_code == 404
    assert stub.messages.create_calls == []
