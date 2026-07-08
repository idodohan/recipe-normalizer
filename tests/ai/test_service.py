"""Service-level tests for ai.service: conversation + message storage."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from recipe_normalizer.ai import service as ai_service
from recipe_normalizer.ai.models import Conversation, ConversationKind, Message, MessageRole
from recipe_normalizer.cookbook import service as cookbook_service
from recipe_normalizer.cookbook.schemas import (
    IngredientGroupIn,
    IngredientLineIn,
    RecipeIn,
    RecipeOut,
)
from recipe_normalizer.errors import ApiError
from recipe_normalizer.users.models import User

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
