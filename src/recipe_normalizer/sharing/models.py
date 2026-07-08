"""Sharing models: `shares` (copy-on-share audit trail), `public_links`,
and shared cookbooks (`shared_cookbooks` + `shared_cookbook_members` +
`shared_cookbook_recipes`).
"""

import secrets
import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from recipe_normalizer.db import Base, new_uuid


def _new_token() -> str:
    """Unguessable public-link token.

    ``secrets.token_urlsafe(24)`` yields 24 random bytes (~192 bits of
    entropy) as a 32-character urlsafe-base64 string — comfortably over the
    >=128-bit bar from the phase plan's global constraints.
    """
    return secrets.token_urlsafe(24)


class Share(Base):
    """One copy-on-share event: who sent what to whom, and what copy resulted.

    ``origin_recipe_id`` is deliberately a *plain* uuid column — not a
    foreign key, and specifically not a ``SET NULL`` foreign key either. The
    copy (``copied_recipe_id``) is meant to be an independent snapshot that
    survives deletion of the original: if it were an FK with ``CASCADE``,
    deleting the origin recipe would also delete this share record (and, if
    cascaded further, could be mistaken for a reason to touch the copy); if
    it were ``SET NULL``, deleting the origin would silently erase
    provenance data ("this recipe traces back to origin X") we still want to
    keep visible on the share record after the fact. A bare, non-FK uuid
    column keeps the share row — and the provenance it documents — entirely
    decoupled from the origin recipe's lifecycle.

    ``copied_recipe_id``, by contrast, IS a real foreign key with
    ``ON DELETE CASCADE``: once the recipient's copy is gone, the share
    record documenting that send-event no longer refers to anything and is
    cleaned up with it.
    """

    __tablename__ = "shares"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    origin_recipe_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    from_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    to_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    copied_recipe_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("recipes.id", ondelete="CASCADE"), index=True, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), server_default=func.now()
    )


class PublicLink(Base):
    """A tokenized, revocable, read-only link to one recipe — no account needed.

    ``recipe_id`` IS a real foreign key with ``ON DELETE CASCADE`` (unlike
    ``Share.origin_recipe_id`` above): a public link has no reason to outlive
    the recipe it points at — once the recipe is gone there is nothing left
    to serve, so the link should disappear with it rather than start
    resolving to a ghost / 404 forever.

    Revocation is soft-delete (``revoked_at`` timestamp) rather than row
    deletion so the owner's management list (``GET /api/share/public``) can
    still show past links; the public GET endpoint treats a revoked link
    exactly like a nonexistent token (both 404, same body — see
    sharing/service.py).
    """

    __tablename__ = "public_links"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    token: Mapped[str] = mapped_column(
        String(64), unique=True, index=True, nullable=False, default=_new_token
    )
    recipe_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("recipes.id", ondelete="CASCADE"), index=True, nullable=False
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), server_default=func.now()
    )


# ---------------------------------------------------------------------------
# Shared cookbooks — live, co-owned collections of recipes (LWW editing)
# ---------------------------------------------------------------------------


class SharedCookbook(Base):
    """A named, co-owned cookbook: a group of users who all get read/edit
    access (via cookbook.service.user_recipe_access's membership-checker
    hook — see sharing/service.py) to every recipe in it, regardless of who
    originally owns that recipe.

    The creator is auto-inserted as a ``SharedCookbookMember`` row at
    creation time (see ``sharing.service.create_shared_cookbook``) — there
    is no special-cased "owner" column on this table beyond ``created_by``,
    which exists purely to answer "who may remove other members / is
    blocked from leaving while others remain" (see
    ``sharing.service.remove_member``).
    """

    __tablename__ = "shared_cookbooks"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), server_default=func.now()
    )


class SharedCookbookMember(Base):
    """Membership row: one (cookbook, user) pair. Composite PK — a user is
    either a member of a given cookbook or not, no duplicate rows possible.

    ``added_by`` is an audit trail (who invited this member — the creator
    for the auto-inserted row, or whichever member sent the invite; "any
    member may invite" per the phase plan) and is not currently surfaced by
    any API response.
    """

    __tablename__ = "shared_cookbook_members"

    cookbook_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("shared_cookbooks.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    added_by: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), server_default=func.now()
    )


class SharedCookbookRecipe(Base):
    """Link row: one recipe added to one shared cookbook. Composite PK — a
    recipe can only be added to a given cookbook once (re-adding is a 409
    ``already_added``, see sharing.service.add_recipe_to_shared_cookbook).

    A recipe can belong to more than one shared cookbook simultaneously
    (different rows, different cookbook_id) — removing it from one leaves
    membership-derived access via any other intact, by design (see
    sharing.service's membership-checker callback, which checks ANY
    matching row, not a specific cookbook).

    ``recipe_id`` cascades on delete: once the recipe itself is gone there's
    nothing left to link.
    """

    __tablename__ = "shared_cookbook_recipes"

    cookbook_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("shared_cookbooks.id", ondelete="CASCADE"), primary_key=True
    )
    recipe_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("recipes.id", ondelete="CASCADE"), primary_key=True
    )
    added_by: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), server_default=func.now()
    )
