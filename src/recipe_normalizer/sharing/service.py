"""Sharing service: copy-on-share and per-recipe public links.

Talks to `cookbook` only via `cookbook.service` (never `cookbook.models`)
and to `users` only via `users.service` (never `users.models`) — see
.importlinter. Transaction convention matches the rest of the codebase:
this module flushes; the HTTP layer (or test) owns commit.

This module used to also own "shared cookbooks" — a parallel, role-less
co-ownership model whose membership widened recipe access through a callback
registered into `cookbook.service`. That concept is now the first-class
`Cookbook` + `CookbookMember` model in `cookbook`, so the tables, the
endpoints and the access-widening hook are gone; what remains here is the two
genuinely sharing-shaped features that have no cookbook equivalent:
copy-on-share (deep-copy a recipe into someone else's cookbook) and
tokenized, revocable public links to a single recipe.
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
    "get_public_recipe_image_ref",
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

    - The sharer must own *recipe_id* OUTRIGHT — checked explicitly via
      ``cookbook_service.is_recipe_owner``, NOT via
      ``cookbook.service.get_recipe`` (whose access derives from the recipe's
      COOKBOOK, so it would let any member of the cookbook holding the recipe
      copy-on-share a recipe they don't own). A recipe that exists but isn't
      the sharer's own — including one they can merely see as a cookbook
      member — gets the same 404 as one that doesn't exist at all.
    - The recipient is looked up by case-insensitive email; 404
      ``recipient_not_found`` if no account has that email.
    - Sharing to yourself is rejected with 422 — checked by user id (not by
      comparing email strings), so it also catches sharing to a second
      account registered under a differently-cased email.
    - The copy's ``provenance`` records *from_email* (not display name) for
      ``shared_by``, per spec.

    Flushes; caller owns commit.
    """
    # Ownership check — explicit recipe-owner gate, not cookbook-derived access.
    if not cookbook_service.is_recipe_owner(db, from_user_id, recipe_id):
        raise ApiError(404, "not_found", f"Recipe {recipe_id} not found.")

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

    - The caller must own *recipe_id* OUTRIGHT — checked explicitly via
      ``cookbook_service.is_recipe_owner``, NOT via
      ``cookbook_service.get_recipe`` (whose access derives from the recipe's
      cookbook). A public link is an unauthenticated, unrevocable-by-others
      window into a recipe; a cookbook member merely having *access*
      to view/edit the recipe must never be enough to mint one for the
      owner's recipe — that link wouldn't even show up in the owner's own
      ``list_public_links``/be revocable via ``revoke_public_link`` (both
      scoped to ``created_by``). A recipe that exists but isn't the caller's
      own gets the same 404 as one that doesn't exist at all.
    - IDEMPOTENT-ish: if this owner already has an unrevoked link for this
      recipe, that existing link is returned rather than minting a second
      token — callers can safely call this repeatedly (e.g. re-opening the
      share dialog) without accumulating dead links.

    Flushes; caller owns commit.
    """
    if not cookbook_service.is_recipe_owner(db, owner_id, recipe_id):
        raise ApiError(404, "not_found", f"Recipe {recipe_id} not found.")
    recipe = cookbook_service.get_recipe_unscoped(db, recipe_id)

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
    return PublicRecipeOut.from_recipe_out(recipe, token=token)


def get_public_recipe_image_ref(db: Session, *, token: str) -> str:
    """Resolve *token* to its OWN recipe's raw image ref, for the public image route.

    404 (identical body to ``_resolve_public_token``'s) for an unknown or
    revoked token, and 404 if the recipe currently has no image at all. The
    ref returned always belongs to the token's own recipe — this function
    never accepts (and the router never passes) a client-supplied ref, so a
    valid token can't be used to fetch a DIFFERENT recipe's image bytes.
    """
    recipe_id = _resolve_public_token(db, token)
    ref = cookbook_service.get_recipe_image_ref_unscoped(db, recipe_id)
    if ref is None:
        raise ApiError(404, "not_found", "This recipe has no image.")
    return ref


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
