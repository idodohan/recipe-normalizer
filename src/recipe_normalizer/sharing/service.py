"""Sharing service: copy-on-share.

Talks to `cookbook` only via `cookbook.service` (never `cookbook.models`)
and to `users` only via `users.service` (never `users.models`) — see
.importlinter. Transaction convention matches the rest of the codebase:
this module flushes; the HTTP layer (or test) owns commit.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from recipe_normalizer.cookbook import service as cookbook_service
from recipe_normalizer.cookbook.schemas import RecipeOut
from recipe_normalizer.errors import ApiError
from recipe_normalizer.sharing.models import PublicLink, Share
from recipe_normalizer.sharing.schemas import PublicLinkOut, PublicRecipeOut, ShareOut
from recipe_normalizer.users import service as users_service

__all__ = [
    "create_public_link",
    "get_public_recipe",
    "get_public_recipe_for_scaling",
    "list_public_links",
    "revoke_public_link",
    "share_recipe",
]


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


# ---------------------------------------------------------------------------
# Public links
# ---------------------------------------------------------------------------


def _to_public_link_out(link: PublicLink, *, recipe_title: str) -> PublicLinkOut:
    return PublicLinkOut(
        id=link.id,
        token=link.token,
        recipe_id=link.recipe_id,
        recipe_title=recipe_title,
        revoked_at=link.revoked_at,
        created_at=link.created_at,
    )


def create_public_link(db: Session, *, owner_id: uuid.UUID, recipe_id: uuid.UUID) -> PublicLinkOut:
    """Create a public link for *recipe_id*, owner-scoped.

    - The caller must own *recipe_id*: reuses ``cookbook_service.get_recipe``'s
      owner-scoped 404, so a recipe owned by someone else is indistinguishable
      from one that doesn't exist.
    - IDEMPOTENT-ish: if this owner already has an unrevoked link for this
      recipe, that existing link is returned rather than minting a second
      token — callers can safely call this repeatedly (e.g. re-opening the
      share dialog) without accumulating dead links.

    Flushes; caller owns commit.
    """
    recipe = cookbook_service.get_recipe(db, owner_id=owner_id, recipe_id=recipe_id)

    existing = db.scalars(
        select(PublicLink).where(
            PublicLink.recipe_id == recipe_id,
            PublicLink.created_by == owner_id,
            PublicLink.revoked_at.is_(None),
        )
    ).first()
    if existing is not None:
        return _to_public_link_out(existing, recipe_title=recipe.title)

    link = PublicLink(recipe_id=recipe_id, created_by=owner_id)
    db.add(link)
    db.flush()
    return _to_public_link_out(link, recipe_title=recipe.title)


def list_public_links(db: Session, *, owner_id: uuid.UUID) -> list[PublicLinkOut]:
    """List every public link (revoked or not) *owner_id* has ever created."""
    links = list(
        db.scalars(
            select(PublicLink)
            .where(PublicLink.created_by == owner_id)
            .order_by(PublicLink.created_at.desc())
        ).all()
    )
    titles = cookbook_service.recipe_titles_for_ids(db, {link.recipe_id for link in links})
    return [
        _to_public_link_out(link, recipe_title=titles.get(link.recipe_id, "")) for link in links
    ]


def revoke_public_link(db: Session, *, owner_id: uuid.UUID, link_id: uuid.UUID) -> None:
    """Revoke *link_id*, owner-scoped (404 if missing or owned by someone else).

    Idempotent: revoking an already-revoked link is a no-op, not an error.
    Flushes; caller owns commit.
    """
    link = db.scalars(
        select(PublicLink).where(PublicLink.id == link_id, PublicLink.created_by == owner_id)
    ).first()
    if link is None:
        raise ApiError(404, "not_found", f"Public link {link_id} not found.")
    if link.revoked_at is None:
        link.revoked_at = datetime.now(UTC)
        db.flush()


def _resolve_public_token(db: Session, token: str) -> uuid.UUID:
    """Resolve *token* to a recipe id.

    404 for BOTH an unknown token and a revoked one, with the identical error
    body — a caller probing tokens can't distinguish "never existed" from
    "existed but got revoked".
    """
    link = db.scalars(select(PublicLink).where(PublicLink.token == token)).first()
    if link is None or link.revoked_at is not None:
        raise ApiError(404, "not_found", "This link is unavailable.")
    return link.recipe_id


def get_public_recipe(db: Session, *, token: str) -> PublicRecipeOut:
    """Unauthenticated recipe lookup by public token — see PublicRecipeOut for the leak audit."""
    recipe_id = _resolve_public_token(db, token)
    recipe = cookbook_service.get_recipe_unscoped(db, recipe_id)
    return PublicRecipeOut.from_recipe_out(recipe)


def get_public_recipe_for_scaling(db: Session, *, token: str) -> RecipeOut:
    """Resolve *token* to a full ``RecipeOut`` for the scaled endpoint.

    Deliberately returns the full (unfiltered) ``RecipeOut``, unlike
    ``get_public_recipe`` — but this is safe because the router never
    serializes this return value directly. It immediately feeds it to
    ``cookbook.scaling.scale_recipe``, whose *output* type (``ScaledRecipeOut``)
    is a completely separate schema — it only ever carries
    recipe_id/factor/servings/groups/steps and was never capable of
    representing notes/favorites/provenance/extraction_meta, so there is no
    leak path through it. This mirrors exactly what the authenticated scaled
    endpoint does with ``cookbook_service.get_recipe``.
    """
    recipe_id = _resolve_public_token(db, token)
    return cookbook_service.get_recipe_unscoped(db, recipe_id)
