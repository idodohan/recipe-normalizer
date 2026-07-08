"""Service-level tests for ai.service: conversation + message storage."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from recipe_normalizer.ai import service as ai_service
from recipe_normalizer.ai.models import Conversation, ConversationKind, Message, MessageRole
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
from tests.llm.stubs import StubAnthropicClient, text_response

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
    assert usage_rows[0].feature == "ai.recipe_chat"
