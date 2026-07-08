"""Sharing service: copy-on-share, public links, and shared cookbooks.

Talks to `cookbook` only via `cookbook.service` (never `cookbook.models`)
and to `users` only via `users.service` (never `users.models`) — see
.importlinter. Transaction convention matches the rest of the codebase:
this module flushes; the HTTP layer (or test) owns commit.

Shared-cookbook member lists and "last edited by" render DISPLAY NAMES
ONLY, never email addresses — a design decision, not an oversight. Friends
sharing a house cookbook already know each other; there's no reason to hand
every member the email address of every other member just to show who's in
the cookbook or who touched a recipe last.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from recipe_normalizer.cookbook import service as cookbook_service
from recipe_normalizer.cookbook.schemas import RecipeOut
from recipe_normalizer.errors import ApiError
from recipe_normalizer.sharing.models import (
    PublicLink,
    Share,
    SharedCookbook,
    SharedCookbookMember,
    SharedCookbookRecipe,
)
from recipe_normalizer.sharing.schemas import (
    PublicLinkOut,
    PublicRecipeOut,
    SharedCookbookDetailOut,
    SharedCookbookMemberOut,
    SharedCookbookOut,
    SharedCookbookRecipeOut,
    ShareOut,
)
from recipe_normalizer.users import service as users_service

__all__ = [
    "add_recipe_to_shared_cookbook",
    "create_public_link",
    "create_shared_cookbook",
    "get_public_recipe",
    "get_public_recipe_for_scaling",
    "get_shared_cookbook",
    "invite_member",
    "list_my_shared_cookbooks",
    "list_public_links",
    "register_hooks",
    "remove_member",
    "remove_recipe_from_shared_cookbook",
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


# ---------------------------------------------------------------------------
# Shared cookbooks
# ---------------------------------------------------------------------------


def _get_membership(
    db: Session, cookbook_id: uuid.UUID, user_id: uuid.UUID
) -> SharedCookbookMember | None:
    return db.scalars(
        select(SharedCookbookMember).where(
            SharedCookbookMember.cookbook_id == cookbook_id,
            SharedCookbookMember.user_id == user_id,
        )
    ).first()


def _require_member(db: Session, cookbook_id: uuid.UUID, user_id: uuid.UUID) -> SharedCookbook:
    """Fetch a shared cookbook, requiring *user_id* to be a member.

    404 for BOTH a nonexistent cookbook id AND one that exists but *user_id*
    isn't a member of — identical body, so a non-member can't tell the two
    apart (the phase plan's "member-gated endpoints 404, not 403, for
    non-members" rule).
    """
    cookbook = db.get(SharedCookbook, cookbook_id)
    if cookbook is None or _get_membership(db, cookbook_id, user_id) is None:
        raise ApiError(404, "not_found", f"Shared cookbook {cookbook_id} not found.")
    return cookbook


def _member_count(db: Session, cookbook_id: uuid.UUID) -> int:
    return (
        db.scalar(
            select(func.count())
            .select_from(SharedCookbookMember)
            .where(SharedCookbookMember.cookbook_id == cookbook_id)
        )
        or 0
    )


def _recipe_count(db: Session, cookbook_id: uuid.UUID) -> int:
    return (
        db.scalar(
            select(func.count())
            .select_from(SharedCookbookRecipe)
            .where(SharedCookbookRecipe.cookbook_id == cookbook_id)
        )
        or 0
    )


def _to_cookbook_out(db: Session, cookbook: SharedCookbook) -> SharedCookbookOut:
    return SharedCookbookOut(
        id=cookbook.id,
        name=cookbook.name,
        created_by=cookbook.created_by,
        created_at=cookbook.created_at,
        member_count=_member_count(db, cookbook.id),
        recipe_count=_recipe_count(db, cookbook.id),
    )


def create_shared_cookbook(db: Session, *, creator_id: uuid.UUID, name: str) -> SharedCookbookOut:
    """Create a shared cookbook; the creator is auto-inserted as its first member.

    No uniqueness constraint on name (unlike personal collections) —
    shared cookbooks aren't a per-owner namespace, so "Sunday Dinners" and
    "Sunday Dinners" created by two different friend groups are simply two
    unrelated cookbooks. Flushes; caller owns commit.
    """
    stripped = name.strip()
    cookbook = SharedCookbook(name=stripped, created_by=creator_id)
    db.add(cookbook)
    db.flush()  # get cookbook.id

    db.add(SharedCookbookMember(cookbook_id=cookbook.id, user_id=creator_id, added_by=creator_id))
    db.flush()

    return _to_cookbook_out(db, cookbook)


def list_my_shared_cookbooks(db: Session, *, user_id: uuid.UUID) -> list[SharedCookbookOut]:
    """List shared cookbooks *user_id* is a member of, newest first, with counts."""
    my_ids = select(SharedCookbookMember.cookbook_id).where(SharedCookbookMember.user_id == user_id)
    cookbooks = db.scalars(
        select(SharedCookbook)
        .where(SharedCookbook.id.in_(my_ids))
        .order_by(SharedCookbook.created_at.desc())
    ).all()
    return [_to_cookbook_out(db, c) for c in cookbooks]


def get_shared_cookbook(
    db: Session, *, user_id: uuid.UUID, cookbook_id: uuid.UUID
) -> SharedCookbookDetailOut:
    """Full detail view: members + recipes. 404 if *user_id* isn't a member.

    Members are listed creator-first, then alphabetically by display name.
    Recipes are listed most-recently-added-to-this-cookbook first. Both
    lists resolve display names via ``users_service.display_names_for_ids``
    in one batched call each — no per-row N+1.
    """
    cookbook = _require_member(db, cookbook_id, user_id)

    memberships = db.scalars(
        select(SharedCookbookMember).where(SharedCookbookMember.cookbook_id == cookbook_id)
    ).all()
    links = db.scalars(
        select(SharedCookbookRecipe)
        .where(SharedCookbookRecipe.cookbook_id == cookbook_id)
        .order_by(SharedCookbookRecipe.created_at.desc())
    ).all()

    summaries = cookbook_service.recipe_summaries_for_ids(db, [link.recipe_id for link in links])

    name_ids = {m.user_id for m in memberships} | {
        s.last_edited_by for s in summaries.values() if s.last_edited_by is not None
    }
    names = users_service.display_names_for_ids(db, name_ids)

    members = [
        SharedCookbookMemberOut(
            user_id=m.user_id,
            display_name=names.get(m.user_id, ""),
            is_creator=(m.user_id == cookbook.created_by),
        )
        for m in memberships
    ]
    members.sort(key=lambda m: (not m.is_creator, m.display_name.lower()))

    recipes = []
    for link in links:
        summary = summaries.get(link.recipe_id)
        if summary is None:
            continue  # recipe was deleted after being added — skip, don't raise
        recipes.append(
            SharedCookbookRecipeOut(
                id=summary.id,
                title=summary.title,
                image_ref=summary.image_ref,
                dish_types=summary.dish_types,
                total_min=summary.total_min,
                is_verified=summary.is_verified,
                created_at=summary.created_at,
                last_edited_by=summary.last_edited_by,
                last_edited_by_name=(
                    names.get(summary.last_edited_by) if summary.last_edited_by else None
                ),
            )
        )

    return SharedCookbookDetailOut(
        id=cookbook.id,
        name=cookbook.name,
        created_by=cookbook.created_by,
        created_at=cookbook.created_at,
        members=members,
        recipes=recipes,
    )


def invite_member(
    db: Session, *, user_id: uuid.UUID, cookbook_id: uuid.UUID, email: str
) -> SharedCookbookMemberOut:
    """Invite a user by email to a shared cookbook. Any existing member may invite.

    - 404 if *user_id* (the inviter) isn't a member — same indistinguishable
      404 as every other member-gated endpoint here.
    - 404 ``recipient_not_found`` if no account has that email.
    - 409 ``already_member`` if the recipient is already in the cookbook
      (this also covers "you invited yourself" and "you re-invited the
      creator" — both are already members).
    Flushes; caller owns commit.
    """
    _require_member(db, cookbook_id, user_id)

    recipient = users_service.get_user_by_email(db, email)
    if recipient is None:
        raise ApiError(404, "recipient_not_found", "No account with that email.")

    if _get_membership(db, cookbook_id, recipient.id) is not None:
        raise ApiError(409, "already_member", "That person is already in this shared cookbook.")

    db.add(SharedCookbookMember(cookbook_id=cookbook_id, user_id=recipient.id, added_by=user_id))
    db.flush()

    return SharedCookbookMemberOut(
        user_id=recipient.id, display_name=recipient.display_name, is_creator=False
    )


def remove_member(
    db: Session, *, user_id: uuid.UUID, cookbook_id: uuid.UUID, target_user_id: uuid.UUID
) -> None:
    """Remove a member from a shared cookbook.

    - Self-leave is always allowed (unless the "creator can't leave while
      others remain" rule below applies).
    - The creator may remove anyone else.
    - A non-creator member may NOT remove anyone but themselves — attempting
      to do so 404s, matching the codebase-wide idiom (see
      cookbook.service's owner-scoped functions) of using 404 rather than
      403 for "you're authenticated but this action isn't yours to take",
      not just for genuine non-existence.
    - 422 ``creator_cannot_leave`` if the creator tries to self-leave while
      other members remain — they'd otherwise orphan a cookbook with active
      members and no one able to remove anyone else.
    - DECISION: the last member leaving deletes the cookbook entirely
      (cascading to its membership/recipe-link rows) rather than leaving an
      empty husk around forever — there is no way to "recreate" it, so
      leaving as the sole remaining member is effectively "delete this
      shared cookbook".
    Flushes; caller owns commit.
    """
    cookbook = _require_member(db, cookbook_id, user_id)

    target_membership = _get_membership(db, cookbook_id, target_user_id)
    if target_membership is None:
        raise ApiError(404, "not_found", f"Member {target_user_id} not found.")

    is_self = target_user_id == user_id
    is_actor_creator = user_id == cookbook.created_by

    if not is_self and not is_actor_creator:
        raise ApiError(404, "not_found", f"Member {target_user_id} not found.")

    if is_self and is_actor_creator and _member_count(db, cookbook_id) > 1:
        raise ApiError(
            422,
            "creator_cannot_leave",
            "As the creator, remove every other member before you can leave this "
            "shared cookbook yourself.",
        )

    remaining_before_delete = _member_count(db, cookbook_id)
    db.delete(target_membership)
    db.flush()

    if remaining_before_delete - 1 <= 0:
        db.delete(cookbook)
        db.flush()


def add_recipe_to_shared_cookbook(
    db: Session, *, user_id: uuid.UUID, cookbook_id: uuid.UUID, recipe_id: uuid.UUID
) -> SharedCookbookRecipeOut:
    """Add a recipe to a shared cookbook. Any existing member may add.

    - 404 if *user_id* isn't a member of the cookbook.
    - The recipe must be one *user_id* already has access to — owner OR
      member of some (possibly different) shared cookbook containing it, via
      ``cookbook_service.user_recipe_access`` — 404 otherwise. This is
      deliberately the SAME check the widened get_recipe/update_recipe use,
      so "recipes I can add to a shared cookbook" is exactly "recipes I can
      already see and edit", no new authorization surface.
    - 409 ``already_added`` if the recipe is already linked to this cookbook.
    Flushes; caller owns commit.
    """
    _require_member(db, cookbook_id, user_id)

    if cookbook_service.user_recipe_access(db, user_id, recipe_id) is None:
        raise ApiError(404, "not_found", f"Recipe {recipe_id} not found.")

    existing = db.scalars(
        select(SharedCookbookRecipe).where(
            SharedCookbookRecipe.cookbook_id == cookbook_id,
            SharedCookbookRecipe.recipe_id == recipe_id,
        )
    ).first()
    if existing is not None:
        raise ApiError(409, "already_added", "That recipe is already in this shared cookbook.")

    db.add(SharedCookbookRecipe(cookbook_id=cookbook_id, recipe_id=recipe_id, added_by=user_id))
    db.flush()

    summary = cookbook_service.recipe_summaries_for_ids(db, [recipe_id])[recipe_id]
    last_edited_name = None
    if summary.last_edited_by is not None:
        names = users_service.display_names_for_ids(db, {summary.last_edited_by})
        last_edited_name = names.get(summary.last_edited_by)

    return SharedCookbookRecipeOut(
        id=summary.id,
        title=summary.title,
        image_ref=summary.image_ref,
        dish_types=summary.dish_types,
        total_min=summary.total_min,
        is_verified=summary.is_verified,
        created_at=summary.created_at,
        last_edited_by=summary.last_edited_by,
        last_edited_by_name=last_edited_name,
    )


def remove_recipe_from_shared_cookbook(
    db: Session, *, user_id: uuid.UUID, cookbook_id: uuid.UUID, recipe_id: uuid.UUID
) -> None:
    """Remove a recipe from a shared cookbook. Any member may remove.

    404 if *user_id* isn't a member, or if the recipe isn't currently linked
    to this cookbook. If the recipe is also linked to a different shared
    cookbook, removing it here does NOT affect access via that other
    cookbook (the membership checker matches ANY linking row — see
    ``_shared_cookbook_membership_checker`` below). Flushes; caller owns
    commit.
    """
    _require_member(db, cookbook_id, user_id)

    link = db.scalars(
        select(SharedCookbookRecipe).where(
            SharedCookbookRecipe.cookbook_id == cookbook_id,
            SharedCookbookRecipe.recipe_id == recipe_id,
        )
    ).first()
    if link is None:
        raise ApiError(404, "not_found", f"Recipe {recipe_id} not found in this cookbook.")

    db.delete(link)
    db.flush()


# ---------------------------------------------------------------------------
# Access-widening hook: the membership checker registered into cookbook
# ---------------------------------------------------------------------------


def _shared_cookbook_membership_checker(
    db: Session, user_id: uuid.UUID, recipe_id: uuid.UUID
) -> bool:
    """True iff *user_id* is a member of ANY shared cookbook containing *recipe_id*.

    This is the callback ``cookbook.service.user_recipe_access`` calls (via
    ``register_membership_checker``) when a caller doesn't own the recipe
    outright — it never cares WHICH cookbook or who added the recipe, only
    whether at least one membership+link pair exists.
    """
    stmt = (
        select(SharedCookbookRecipe.cookbook_id)
        .join(
            SharedCookbookMember,
            SharedCookbookMember.cookbook_id == SharedCookbookRecipe.cookbook_id,
        )
        .where(SharedCookbookRecipe.recipe_id == recipe_id, SharedCookbookMember.user_id == user_id)
        .limit(1)
    )
    return db.scalars(stmt).first() is not None


_hooks_registered = False


def register_hooks() -> None:
    """Register the shared-cookbook membership checker with cookbook.service (idempotent).

    Mirrors ``cookbook.service.register_hooks()``'s own pattern for the
    catalog merge hook, but in the opposite direction: THIS module (the one
    doing the widening) registers itself into the narrower module
    (cookbook), which never imports sharing (see .importlinter's
    cookbook-cannot-import-sharing contract). Called both at import time
    (bottom of this module — so any process whose import graph reaches
    sharing.service gets it for free, exactly like cookbook.service's own
    bottom-of-module self-registration) and explicitly from main.py's app
    factory / worker.py's startup imports, mirroring how
    cookbook.service.register_hooks() is wired in both entry points.
    """
    global _hooks_registered
    if _hooks_registered:
        return
    cookbook_service.register_membership_checker(_shared_cookbook_membership_checker)
    _hooks_registered = True


# ---------------------------------------------------------------------------
# Register hooks at import time (after the checker function is defined)
# ---------------------------------------------------------------------------

register_hooks()
