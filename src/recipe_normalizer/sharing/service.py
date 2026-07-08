"""Sharing service: copy-on-share.

Talks to `cookbook` only via `cookbook.service` (never `cookbook.models`)
and to `users` only via `users.service` (never `users.models`) — see
.importlinter. Transaction convention matches the rest of the codebase:
this module flushes; the HTTP layer (or test) owns commit.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from recipe_normalizer.cookbook import service as cookbook_service
from recipe_normalizer.errors import ApiError
from recipe_normalizer.sharing.models import Share
from recipe_normalizer.sharing.schemas import ShareOut
from recipe_normalizer.users import service as users_service

__all__ = ["share_recipe"]


def share_recipe(
    db: Session,
    *,
    from_user_id: uuid.UUID,
    from_email: str,
    recipe_id: uuid.UUID,
    to_email: str,
) -> ShareOut:
    """Copy-on-share: deep-copy *recipe_id* into the recipient's cookbook.

    - The sharer must own *recipe_id*: this reuses
      ``cookbook.service.get_recipe``'s owner-scoped 404, so a recipe that
      exists but belongs to someone else is indistinguishable from one that
      doesn't exist at all.
    - The recipient is looked up by case-insensitive email; 404
      ``recipient_not_found`` if no account has that email.
    - Sharing to yourself is rejected with 422 — checked by user id (not by
      comparing email strings), so it also catches sharing to a second
      account registered under a differently-cased email.
    - The copy's ``provenance`` records *from_email* (not display name) for
      ``shared_by``, per spec.

    Flushes; caller owns commit.
    """
    # Ownership check — raises ApiError 404 if not found / not the sharer's.
    cookbook_service.get_recipe(db, owner_id=from_user_id, recipe_id=recipe_id)

    recipient = users_service.get_user_by_email(db, to_email)
    if recipient is None:
        raise ApiError(404, "recipient_not_found", "No account with that email.")

    if recipient.id == from_user_id:
        raise ApiError(422, "validation_error", "You can't share a recipe with yourself.")

    provenance = {
        "shared_by": from_email,
        "shared_at": datetime.now(UTC).isoformat(),
        "origin_recipe_id": str(recipe_id),
    }
    copied = cookbook_service.copy_recipe(
        db,
        recipe_id,
        new_owner_id=recipient.id,
        provenance=provenance,
    )

    share = Share(
        origin_recipe_id=recipe_id,
        from_user_id=from_user_id,
        to_user_id=recipient.id,
        copied_recipe_id=copied.id,
    )
    db.add(share)
    db.flush()

    return ShareOut(
        id=share.id,
        copied_recipe_id=copied.id,
        to_email=recipient.email,
        created_at=share.created_at,
    )
