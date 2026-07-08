"""Sharing models: `shares`, the copy-on-share audit trail.

More tables (public links, shared cookbooks + memberships) land here in
later tasks of the same phase.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column

from recipe_normalizer.db import Base, new_uuid


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
