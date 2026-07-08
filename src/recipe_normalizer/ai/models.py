"""AI models: `Conversation` and `Message`.

A conversation is either:
  - per-recipe chat (``kind=recipe_chat``, ``recipe_id`` set), or
  - cookbook-wide Q&A (``kind=cookbook_qa``, ``recipe_id`` NULL).

Both ``Conversation.recipe_id`` and ``Message.conversation_id`` cascade on
delete: a conversation about a recipe has no reason to outlive that recipe
(there's nothing left to chat about), and a message has no reason to
outlive its conversation.
"""

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from recipe_normalizer.db import Base, TimestampMixin, new_uuid


class ConversationKind(enum.StrEnum):
    recipe_chat = "recipe_chat"
    cookbook_qa = "cookbook_qa"


class MessageRole(enum.StrEnum):
    user = "user"
    assistant = "assistant"


class Conversation(TimestampMixin, Base):
    """One chat/Q&A thread, owned by exactly one user.

    ``recipe_id`` is nullable — NULL means a cookbook-wide Q&A conversation
    (``kind=cookbook_qa``) that isn't scoped to any single recipe. When set,
    it's a real foreign key with ``ON DELETE CASCADE``: deleting the recipe
    a chat is grounded on leaves nothing left to chat about, so the
    conversation (and its messages, via ``Message.conversation_id``'s own
    cascade) disappears with it.

    ``updated_at`` (from ``TimestampMixin``) is bumped by
    ``service.append_message`` on every new message — without that, it
    would sit frozen at ``created_at`` forever, since nothing else on this
    row ever changes after creation. That makes "most recently active
    conversation" a sortable, meaningful column for ``list_conversations``.
    """

    __tablename__ = "conversations"
    __table_args__ = (Index("ix_conversations_user_recipe_kind", "user_id", "recipe_id", "kind"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    recipe_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("recipes.id", ondelete="CASCADE"), index=True, nullable=True
    )
    kind: Mapped[ConversationKind] = mapped_column(Enum(ConversationKind), nullable=False)
    title: Mapped[str | None] = mapped_column(String(200), nullable=True)


class Message(Base):
    """One turn in a conversation — user prompt or assistant reply.

    No ``updated_at``: a message is immutable once written (unlike a
    conversation, whose title/updated_at can change over its life), so this
    mirrors ``sharing.models.Share``'s pattern of a bare ``created_at``
    rather than pulling in ``TimestampMixin``.
    """

    __tablename__ = "messages"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True, nullable=False
    )
    role: Mapped[MessageRole] = mapped_column(Enum(MessageRole), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), server_default=func.now()
    )
