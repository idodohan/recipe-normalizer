"""Cookbook service: Recipe CRUD with catalog matching, deduplication, and merge hooks.

Transaction convention: service flushes; HTTP layer (or test) owns commit.
Import-linter enforces that we never import catalog.models or users.models directly —
we only import from catalog.service (which re-exports the conversion API).
"""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import Subquery, and_, exists, func, literal, literal_column, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, selectinload

from recipe_normalizer.catalog import service as catalog_service
from recipe_normalizer.catalog.service import convert_to_normalized

if TYPE_CHECKING:
    from recipe_normalizer.llm.client import LLMClient
from recipe_normalizer.cookbook.models import (
    Collection,
    Cookbook,
    CookbookMember,
    CookbookRecipe,
    CookbookRole,
    CookbookVisibility,
    Cuisine,
    DishType,
    IngredientGroup,
    IngredientLine,
    Recipe,
    SourceType,
    Step,
    Tag,
    collection_recipes,
    recipe_cuisines,
    recipe_dish_types,
    recipe_tags,
)
from recipe_normalizer.cookbook.schemas import (
    CollectionOut,
    CookbookSummary,
    IngredientLineIn,
    RecipeCookbookOut,
    RecipeIn,
    RecipeOut,
    RecipePage,
    RecipeSummary,
)
from recipe_normalizer.errors import ApiError
from recipe_normalizer.filestore import FileStore
from recipe_normalizer.sniff import detect_media_type
from recipe_normalizer.users import service as users_service

__all__ = [
    "MAX_IMAGE_BYTES",
    "UNSET",
    "Access",
    "DuplicateCollectionNameError",
    "DuplicateRecipeError",
    "SourceType",
    "add_recipe_to_cookbook",
    "are_verified",
    "clear_recipe_image",
    "cookbook_access",
    "cookbooks_for_recipe",
    "copy_recipe",
    "create_collection",
    "create_cookbook",
    "create_recipe",
    "delete_collection",
    "delete_cookbook",
    "delete_recipe",
    "ensure_default_cookbook",
    "find_recipe_id_by_fingerprint",
    "get_recipe",
    "get_recipe_image_ref_unscoped",
    "get_recipe_unscoped",
    "invite_cookbook_member",
    "is_recipe_owner",
    "leave_cookbook",
    "list_collections",
    "list_my_cookbooks",
    "list_recipes",
    "readable_cookbooks_for_recipe",
    "recipe_access",
    "recipe_summaries_for_ids",
    "recipe_titles_for_ids",
    "recommendations_for_recipe",
    "register_hooks",
    "remove_cookbook_member",
    "remove_recipe_from_cookbook",
    "rename_collection",
    "rename_cookbook",
    "repoint_ingredient_lines",
    "require_cookbook_access",
    "save_recipe_to_cookbook",
    "set_cookbook_cover",
    "set_cookbook_description",
    "set_cookbook_member_role",
    "set_cookbook_visibility",
    "set_personal",
    "set_recipe_collections",
    "set_recipe_image",
    "update_recipe",
    "verify_recipe",
]


# Public sentinel for "field not provided" in set_personal, so callers can
# explicitly pass notes=None (clearing it) vs. leaving it untouched.
UNSET: Any = object()


# ---------------------------------------------------------------------------
# Recipe image upload — validation constants
# ---------------------------------------------------------------------------

MAX_IMAGE_BYTES = 10 * 1024 * 1024  # 10 MB — the router bounds its read with this

# Images only — no PDF (that's an ingestion source type, not a recipe photo).
_ALLOWED_IMAGE_TYPES: frozenset[str] = frozenset(
    {"image/png", "image/jpeg", "image/webp", "image/gif"}
)

_IMAGE_MEDIA_TYPE_SUFFIX: dict[str, str] = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
}


# ---------------------------------------------------------------------------
# Custom error
# ---------------------------------------------------------------------------


class DuplicateRecipeError(ApiError):
    """Raised when a recipe with the same (owner_id, source_fingerprint) already exists."""

    existing_id: uuid.UUID

    def __init__(self, existing_id: uuid.UUID) -> None:
        super().__init__(
            status_code=409,
            code="duplicate_recipe",
            message=f"A recipe with this fingerprint already exists (id={existing_id}).",
            extra={"existing_id": str(existing_id)},
        )
        self.existing_id = existing_id


class DuplicateCollectionNameError(ApiError):
    """Raised when a collection with the same (owner_id, name) already exists."""

    def __init__(self, name: str) -> None:
        super().__init__(
            status_code=409,
            code="duplicate_name",
            message=f"A collection named {name!r} already exists.",
        )


# ---------------------------------------------------------------------------
# Hook registration (idempotent) — called at bottom of module after
# repoint_ingredient_lines is defined.
# ---------------------------------------------------------------------------

_hooks_registered = False


def register_hooks() -> None:
    """Register cookbook merge-repoint hook with catalog service (idempotent)."""
    global _hooks_registered
    if _hooks_registered:
        return
    catalog_service.register_merge_hook(repoint_ingredient_lines)
    _hooks_registered = True


# ---------------------------------------------------------------------------
# Cookbook access / role resolution — THE authorization choke point for
# cookbook-scoped operations (Task 4+ recipe/cookbook CRUD builds on this).
#
# Resolution order (first match wins):
#   1. cookbook.owner_id == user_id       -> "owner"
#   2. a CookbookMember row exists        -> that row's CookbookRole
#   3. visibility in {unlisted, public}   -> CookbookRole.viewer (anonymous
#                                             or any authenticated user)
#   4. otherwise                          -> None
#
# Owner always wins even if a (pathological) member row also exists for the
# same (cookbook_id, owner_id) pair. A None user_id (anonymous) can only ever
# resolve to viewer (via public/unlisted) or None — steps 1 and 2 both
# require a real user_id.
# ---------------------------------------------------------------------------

#: An access level: the literal "owner" (outranks every CookbookRole) or a
#: resolved CookbookRole (editor/viewer). Ordered owner > editor > viewer —
#: see `_rank`.
Access = Literal["owner"] | CookbookRole

_ACCESS_RANK: dict[Access, int] = {
    CookbookRole.viewer: 1,
    CookbookRole.editor: 2,
    "owner": 3,
}


def _rank(level: Access) -> int:
    """Map an Access level to its ordinal rank (higher = more privileged)."""
    return _ACCESS_RANK[level]


def _access_from_parts(
    *,
    user_id: uuid.UUID | None,
    cookbook_owner_id: uuid.UUID,
    visibility: CookbookVisibility,
    member_role: CookbookRole | None,
) -> Access | None:
    """THE cookbook access rule, over already-fetched facts about one cookbook.

    Pure (no DB): the two callers differ only in how they obtain the three
    facts — ``_resolve_cookbook_access`` from a loaded row plus a member lookup,
    ``_resolve_recipe_access`` from one joined query over a recipe's whole
    cookbook set. Keeping the rule itself in one place is what guarantees the
    set-based recipe path can never drift from the single-cookbook path.
    """
    if user_id is not None and cookbook_owner_id == user_id:
        return "owner"
    if member_role is not None:
        return member_role
    if visibility in (CookbookVisibility.unlisted, CookbookVisibility.public):
        return CookbookRole.viewer
    return None


def _resolve_cookbook_access(
    db: Session, *, user_id: uuid.UUID | None, cookbook: Cookbook
) -> Access | None:
    """Resolve *user_id*'s access to an already-loaded *cookbook* row."""
    member_role = (
        db.scalar(
            select(CookbookMember.role).where(
                CookbookMember.cookbook_id == cookbook.id,
                CookbookMember.user_id == user_id,
            )
        )
        if user_id is not None
        else None
    )
    return _access_from_parts(
        user_id=user_id,
        cookbook_owner_id=cookbook.owner_id,
        visibility=cookbook.visibility,
        member_role=member_role,
    )


def cookbook_access(
    db: Session, *, user_id: uuid.UUID | None, cookbook_id: uuid.UUID
) -> Access | None:
    """Resolve *user_id*'s access level to *cookbook_id* (None if no claim / doesn't exist).

    See the module-level comment above for the exact resolution order.
    *user_id* may be None (anonymous) — the only levels reachable then are
    ``CookbookRole.viewer`` (via a public/unlisted cookbook) or ``None``.
    """
    cookbook = db.get(Cookbook, cookbook_id)
    if cookbook is None:
        return None
    return _resolve_cookbook_access(db, user_id=user_id, cookbook=cookbook)


def require_cookbook_access(
    db: Session,
    *,
    user_id: uuid.UUID | None,
    cookbook_id: uuid.UUID,
    need: Access,
) -> Cookbook:
    """Return the Cookbook if *user_id* has at least *need* access, else raise 404.

    Raises ApiError(404) — not 403 — both when the cookbook doesn't exist and
    when it exists but the resolved access level is insufficient, so callers
    never leak whether a cookbook merely belongs to someone else (matches
    this module's existing not-found discipline, e.g. ``get_recipe``).
    """
    cookbook = db.get(Cookbook, cookbook_id)
    resolved = (
        None
        if cookbook is None
        else _resolve_cookbook_access(db, user_id=user_id, cookbook=cookbook)
    )
    if cookbook is None or resolved is None or _rank(resolved) < _rank(need):
        raise ApiError(404, "not_found", f"Cookbook {cookbook_id} not found.")
    return cookbook


# ---------------------------------------------------------------------------
# Recipe access derived from ALL the cookbooks holding it (the boards rule).
#
# A recipe may live in MANY cookbooks (`cookbook_recipes`), so "who may
# read/edit this recipe" is answered by the HIGHEST access level the caller
# holds across that whole set — one readable placement is enough to read,
# one editor+ placement is enough to edit. There is no per-recipe grant.
#
# The one non-cookbook path: `recipes.owner_id`. The recipe's creator always
# resolves to "owner", even when they can reach none of the cookbooks holding
# it (e.g. they contributed it to a shared cookbook and were later removed as
# a member). Phase 1 deliberately denied that case — the boards model
# deliberately allows it (see the design spec's "Access model"), which is what
# dissolves the parked removed-contributor hole. Owner-only operations
# (delete the recipe, mint a public link, be a copy-on-share source) still go
# through `is_recipe_owner`, which is about the ROW's owner and nothing else.
#
# TRANSITION: `recipes.cookbook_id` is still NOT NULL and still dual-written,
# and rows predating the backfill migration have no join row yet. So the
# cookbook SET is `distinct(cookbook_recipes) ∪ {recipes.cookbook_id}` — see
# `_recipe_cookbook_ids`. Reads therefore behave identically before and after
# the migration; once `cookbook_id` is dropped, the union collapses to the
# join table alone and nothing else here changes.
# ---------------------------------------------------------------------------


def _recipe_cookbook_ids(recipe_id: uuid.UUID) -> Subquery:
    """A subquery of every cookbook id holding *recipe_id*, de-duplicated.

    The UNION (not UNION ALL — the dedupe is the point) of the recipe's join
    rows and its transitional primary placement ``recipes.cookbook_id``. Empty
    for a nonexistent recipe. Used both as a join target (``_resolve_recipe_access``)
    and selected from directly (``cookbooks_for_recipe``).
    """
    from_join = select(CookbookRecipe.cookbook_id.label("cookbook_id")).where(
        CookbookRecipe.recipe_id == recipe_id
    )
    from_column = select(Recipe.cookbook_id.label("cookbook_id")).where(Recipe.id == recipe_id)
    return from_join.union(from_column).subquery()


def cookbooks_for_recipe(db: Session, recipe_id: uuid.UUID) -> list[uuid.UUID]:
    """Every cookbook id *recipe_id* is placed in — the "which boards is this on" read.

    UNSCOPED: no access check, and it does not filter to cookbooks the caller
    can see. Callers that expose placements to a user must intersect with what
    that user may read themselves. Returns ``[]`` for a nonexistent recipe.
    """
    ids = _recipe_cookbook_ids(recipe_id)
    return list(db.scalars(select(ids.c.cookbook_id)).all())


def _resolve_recipe_access(
    db: Session, *, user_id: uuid.UUID | None, recipe_id: uuid.UUID, recipe_owner_id: uuid.UUID
) -> Access | None:
    """The set-based rule: highest level across the recipe's cookbooks, or owner.

    One query over the recipe's cookbook set, left-joined to the caller's
    member row in each, then ``_access_from_parts`` per cookbook and ``max`` by
    rank — so a caller who is a viewer of cookbook A and an editor of cookbook
    B, both holding the recipe, gets editor. ``user_id=None`` (anonymous)
    left-joins to nothing (``cookbook_members.user_id`` is NOT NULL, so the
    ``IS NULL`` comparison never matches), leaving only the visibility branch
    reachable — exactly as in the single-cookbook path.
    """
    if user_id is not None and recipe_owner_id == user_id:
        return "owner"
    holding_cookbooks = _recipe_cookbook_ids(recipe_id)
    rows = db.execute(
        select(Cookbook.owner_id, Cookbook.visibility, CookbookMember.role)
        .join(holding_cookbooks, holding_cookbooks.c.cookbook_id == Cookbook.id)
        .outerjoin(
            CookbookMember,
            and_(
                CookbookMember.cookbook_id == Cookbook.id,
                CookbookMember.user_id == user_id,
            ),
        )
    ).all()
    best: Access | None = None
    for cookbook_owner_id, visibility, member_role in rows:
        level = _access_from_parts(
            user_id=user_id,
            cookbook_owner_id=cookbook_owner_id,
            visibility=visibility,
            member_role=member_role,
        )
        if level is not None and (best is None or _rank(level) > _rank(best)):
            best = level
    return best


def _recipe_access(db: Session, *, user_id: uuid.UUID | None, recipe: Recipe) -> Access | None:
    """Resolve *user_id*'s access level to an already-loaded *recipe*."""
    return _resolve_recipe_access(
        db, user_id=user_id, recipe_id=recipe.id, recipe_owner_id=recipe.owner_id
    )


def recipe_access(db: Session, *, user_id: uuid.UUID | None, recipe_id: uuid.UUID) -> Access | None:
    """Resolve *user_id*'s access level to *recipe_id* by id (None if no claim).

    The by-id sibling of ``_recipe_access``, for callers that hold only a
    recipe id and want to gate on access without fetching the whole recipe
    (``ai.service``'s conversation/transform entry points,
    ``recommendations_for_recipe``). Returns ``None`` both for a nonexistent
    recipe and for one the caller has no claim on — callers treat both as
    their 404 case, never revealing which it was.
    """
    owner_id = db.scalar(select(Recipe.owner_id).where(Recipe.id == recipe_id))
    if owner_id is None:
        return None
    return _resolve_recipe_access(
        db, user_id=user_id, recipe_id=recipe_id, recipe_owner_id=owner_id
    )


def is_recipe_owner(db: Session, user_id: uuid.UUID, recipe_id: uuid.UUID) -> bool:
    """True iff *recipe_id* exists and ``recipes.owner_id`` is *user_id*.

    Deliberately about the RECIPE ROW's owner, not cookbook-derived access:
    ``sharing`` uses this for the two operations that must be reserved to the
    person whose recipe it is, no matter which cookbook it currently sits in
    (copy-on-share, and minting a public link). Those two must stay available
    to a recipe's own owner even when their recipe lives in a cookbook someone
    else owns (they contributed it as an editor member), and must stay
    unavailable to a cookbook owner/editor who merely has access to it.
    """
    owner_id = db.scalar(select(Recipe.owner_id).where(Recipe.id == recipe_id))
    return owner_id is not None and owner_id == user_id


def _require_recipe_access(
    db: Session, *, user_id: uuid.UUID | None, recipe: Recipe, need: Access
) -> None:
    """Raise ApiError 404 unless *user_id* has at least *need* access to *recipe*.

    Mirrors ``require_cookbook_access``'s not-found discipline: a recipe that
    doesn't exist and one that exists but the caller can't reach are
    indistinguishable to the caller.
    """
    resolved = _recipe_access(db, user_id=user_id, recipe=recipe)
    if resolved is None or _rank(resolved) < _rank(need):
        raise ApiError(404, "not_found", f"Recipe {recipe.id} not found.")


# ---------------------------------------------------------------------------
# Cookbook CRUD, visibility, and membership (Task 4)
#
# All MANAGE operations (rename/description/cover/delete/visibility/every
# membership mutation) gate through require_cookbook_access(..., need="owner")
# above — the single choke point, never a hand-rolled owner_id== check here.
# ---------------------------------------------------------------------------

#: Name given to the auto-created default cookbook (see ensure_default_cookbook).
DEFAULT_COOKBOOK_NAME = "My Cookbook"


def _select_default_cookbook(db: Session, user_id: uuid.UUID) -> Cookbook | None:
    """Return *user_id*'s ``is_default`` cookbook row, or None if they have none yet."""
    return db.scalars(
        select(Cookbook).where(Cookbook.owner_id == user_id, Cookbook.is_default.is_(True))
    ).first()


def ensure_default_cookbook(db: Session, user_id: uuid.UUID) -> Cookbook:
    """Return *user_id*'s default cookbook, creating it if none exists yet.

    Idempotent: a user has exactly one ``is_default`` cookbook, and repeated
    calls return the same row rather than creating duplicates. The created
    cookbook is named "My Cookbook", private, and marked is_default=True.
    Flushes; caller owns commit.

    Race-safe. This is read-then-insert, and ``GET /api/cookbooks`` calls it on
    every request, so two concurrent first-ever requests for the same user both
    miss the SELECT and then collide on the ``uq_cookbooks_default_per_owner``
    partial unique index (``cookbooks (owner_id) WHERE is_default``, created in
    migration 4e1b7c9a52d8 and deliberately absent from the ORM model). The
    INSERT therefore carries ``ON CONFLICT DO NOTHING``, and the loser of the
    race re-reads the winner's row instead of raising IntegrityError (a 500).
    No conflict target is named on purpose: inferring the partial index would
    mean repeating its predicate here, and the unqualified form covers it —
    the table's only other unique keys are the freshly generated ``id`` and
    ``public_token``, which is NULL on a cookbook this function creates.
    """
    existing = _select_default_cookbook(db, user_id)
    if existing is not None:
        return existing

    db.execute(
        pg_insert(Cookbook)
        .values(
            owner_id=user_id,
            name=DEFAULT_COOKBOOK_NAME,
            visibility=CookbookVisibility.private,
            is_default=True,
        )
        .on_conflict_do_nothing()
    )
    created = _select_default_cookbook(db, user_id)
    if created is not None:
        return created

    # Unreachable at the READ COMMITTED isolation this app runs at: the INSERT
    # is only skipped when a conflicting row is already committed, and the
    # re-SELECT above takes a fresh snapshot that sees it. Under a stricter
    # isolation level (a repeatable-read snapshot taken before the winner
    # committed) it can be reached — fall back to the plain ORM insert, which
    # surfaces the conflict as an error rather than returning None.
    cookbook = Cookbook(
        owner_id=user_id,
        name=DEFAULT_COOKBOOK_NAME,
        visibility=CookbookVisibility.private,
        is_default=True,
    )
    db.add(cookbook)
    db.flush()
    return cookbook


def _cookbook_recipe_counts(db: Session, cookbook_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    """Map cookbook id -> number of recipes PLACED in it, for the given ids only.

    A recipe placed into a cookbook via ``add_recipe_to_cookbook`` (not just
    created there) must count too, matching what ``GET /api/cookbooks/{id}``'s
    own recipe list now shows (see cookbook_router._recipe_count) — so this
    counts the UNION (not union all — the dedupe is the point, same reasoning
    as ``_recipe_cookbook_ids``) of ``cookbook_recipes`` placements and each
    recipe's still-NOT-NULL legacy ``recipes.cookbook_id``, per (cookbook_id,
    recipe_id) pair, so a dual-written recipe isn't counted twice and a
    recipe predating the dual-write (no join row at all) still counts once.
    """
    if not cookbook_ids:
        return {}
    pairs = (
        select(
            CookbookRecipe.cookbook_id.label("cookbook_id"),
            CookbookRecipe.recipe_id.label("recipe_id"),
        )
        .where(CookbookRecipe.cookbook_id.in_(cookbook_ids))
        .union(
            select(
                Recipe.cookbook_id.label("cookbook_id"),
                Recipe.id.label("recipe_id"),
            ).where(Recipe.cookbook_id.in_(cookbook_ids))
        )
        .subquery()
    )
    rows = db.execute(select(pairs.c.cookbook_id, func.count()).group_by(pairs.c.cookbook_id)).all()
    return {cookbook_id: count for cookbook_id, count in rows}


def list_my_cookbooks(db: Session, user_id: uuid.UUID) -> list[CookbookSummary]:
    """List every cookbook *user_id* has a claim on: owned outright, plus member-of.

    Owned cookbooks (``Cookbook.owner_id == user_id``) get role "owner";
    cookbooks reached via a ``CookbookMember`` row get that row's
    ``CookbookRole``. A user is never both (owners never get a member row —
    see cookbook/models.py), so there is no risk of the same cookbook
    appearing twice. Each summary carries a ``recipe_count`` computed via one
    grouped aggregate query (no N+1 across the returned cookbooks).
    """
    owned = db.scalars(select(Cookbook).where(Cookbook.owner_id == user_id)).all()
    member_rows = db.execute(
        select(Cookbook, CookbookMember.role)
        .join(CookbookMember, CookbookMember.cookbook_id == Cookbook.id)
        .where(CookbookMember.user_id == user_id)
    ).all()

    entries: list[tuple[Cookbook, str]] = [(cookbook, "owner") for cookbook in owned]
    entries.extend((cookbook, str(role)) for cookbook, role in member_rows)

    counts = _cookbook_recipe_counts(db, [cookbook.id for cookbook, _ in entries])

    return [
        CookbookSummary(
            id=cookbook.id,
            name=cookbook.name,
            description=cookbook.description,
            visibility=str(cookbook.visibility),
            role=role,
            recipe_count=counts.get(cookbook.id, 0),
            is_default=cookbook.is_default,
            cover_image_ref=cookbook.cover_image_ref,
        )
        for cookbook, role in entries
    ]


def create_cookbook(
    db: Session,
    owner_id: uuid.UUID,
    name: str,
    description: str | None = None,
) -> Cookbook:
    """Create a new (non-default, private) cookbook owned by owner_id.

    Flushes; caller owns commit.
    """
    cookbook = Cookbook(owner_id=owner_id, name=name.strip(), description=description)
    db.add(cookbook)
    db.flush()
    return cookbook


def rename_cookbook(
    db: Session,
    cookbook_id: uuid.UUID,
    user_id: uuid.UUID,
    name: str,
) -> Cookbook:
    """Rename a cookbook. Owner-only (404 via require_cookbook_access otherwise)."""
    cookbook = require_cookbook_access(db, user_id=user_id, cookbook_id=cookbook_id, need="owner")
    cookbook.name = name.strip()
    db.flush()
    return cookbook


def set_cookbook_description(
    db: Session,
    cookbook_id: uuid.UUID,
    user_id: uuid.UUID,
    description: str | None,
) -> Cookbook:
    """Set a cookbook's description. Owner-only (404 otherwise)."""
    cookbook = require_cookbook_access(db, user_id=user_id, cookbook_id=cookbook_id, need="owner")
    cookbook.description = description
    db.flush()
    return cookbook


def set_cookbook_cover(
    db: Session,
    cookbook_id: uuid.UUID,
    user_id: uuid.UUID,
    cover_image_ref: str | None,
) -> Cookbook:
    """Set a cookbook's cover image ref. Owner-only (404 otherwise)."""
    cookbook = require_cookbook_access(db, user_id=user_id, cookbook_id=cookbook_id, need="owner")
    cookbook.cover_image_ref = cover_image_ref
    db.flush()
    return cookbook


def _rehome_multi_placed_recipes(db: Session, cookbook_id: uuid.UUID) -> None:
    """Repoint ``recipes.cookbook_id`` off *cookbook_id* for recipes placed elsewhere too.

    Called just before a cookbook is deleted. ``recipes.cookbook_id`` carries
    ON DELETE CASCADE, so a recipe whose PRIMARY placement is the doomed
    cookbook would be destroyed even when it also lives in other cookbooks —
    silent data loss now that a recipe can be in several. Each such recipe is
    re-homed to its oldest remaining placement instead; recipes with no other
    placement are left to cascade away exactly as before.

    The doomed cookbook's own ``cookbook_recipes`` rows always cascade (they
    are placements IN it, not of it), so nothing here has to delete them.
    """
    replacement = (
        select(CookbookRecipe.cookbook_id)
        .where(
            CookbookRecipe.recipe_id == Recipe.id,
            CookbookRecipe.cookbook_id != cookbook_id,
        )
        .order_by(CookbookRecipe.added_at, CookbookRecipe.cookbook_id)
        .limit(1)
        .correlate(Recipe)
        .scalar_subquery()
    )
    rehomed = db.scalars(
        update(Recipe)
        .where(Recipe.cookbook_id == cookbook_id, replacement.is_not(None))
        .values(cookbook_id=replacement)
        .returning(Recipe.id)
    ).all()
    if not rehomed:
        return  # nothing multi-placed here — this delete is the pre-boards one
    db.flush()
    # The ORM may hold a loaded `cookbook.recipes` collection whose
    # delete-orphan cascade would still delete the rows we just re-homed;
    # expiring drops it back to unloaded, where `passive_deletes` leaves every
    # remaining child to the database's own CASCADE. Gated on rowcount so the
    # overwhelmingly common delete (nothing multi-placed) doesn't invalidate the
    # caller's whole identity map for nothing.
    db.expire_all()


def delete_cookbook(db: Session, cookbook_id: uuid.UUID, user_id: uuid.UUID) -> None:
    """Delete a cookbook. Owner-only (404 otherwise).

    Recipes whose ONLY placement is this cookbook are deleted with it (FK
    CASCADE, as before); recipes that also live in another cookbook survive,
    re-homed to their oldest remaining placement — see
    ``_rehome_multi_placed_recipes``. Membership rows and this cookbook's
    ``cookbook_recipes`` placements always cascade away.

    Raises ApiError(409, "cannot_delete_default", ...) if this is the user's
    default cookbook — every user must always have exactly one. Flushes;
    caller owns commit.
    """
    cookbook = require_cookbook_access(db, user_id=user_id, cookbook_id=cookbook_id, need="owner")
    if cookbook.is_default:
        raise ApiError(
            409,
            "cannot_delete_default",
            "The default cookbook cannot be deleted.",
        )
    _rehome_multi_placed_recipes(db, cookbook_id)
    db.delete(cookbook)
    db.flush()


def set_cookbook_visibility(
    db: Session,
    cookbook_id: uuid.UUID,
    user_id: uuid.UUID,
    visibility: CookbookVisibility,
) -> Cookbook:
    """Set a cookbook's visibility. Owner-only (404 otherwise).

    Moving to unlisted/public mints ``public_token`` via
    ``secrets.token_urlsafe(24)`` — but only if one isn't already set, so
    toggling back and forth doesn't invalidate a previously shared link.
    Moving to private clears the token (None) so a stale link stops working.
    Flushes; caller owns commit.
    """
    cookbook = require_cookbook_access(db, user_id=user_id, cookbook_id=cookbook_id, need="owner")
    cookbook.visibility = visibility
    if visibility in (CookbookVisibility.unlisted, CookbookVisibility.public):
        if cookbook.public_token is None:
            cookbook.public_token = secrets.token_urlsafe(24)
    else:
        cookbook.public_token = None
    db.flush()
    return cookbook


def invite_cookbook_member(
    db: Session,
    cookbook_id: uuid.UUID,
    owner_id: uuid.UUID,
    email: str,
    role: CookbookRole,
) -> CookbookMember:
    """Invite a user by email to a cookbook. Owner-only (404 otherwise).

    Looks up the invitee via ``users.service.get_user_by_email`` (never
    reaching into users.models directly — see .importlinter) — 404
    ``recipient_not_found`` if no account has that email, matching this
    codebase's existing user-enumeration discipline (sharing.service does
    the same). Re-inviting an existing member updates their role in place
    rather than erroring. The owner is authoritative via ``Cookbook.owner_id``
    and is never given a member row — inviting the owner's own email raises
    a 422 rather than creating a nonsensical member row.
    Flushes; caller owns commit.
    """
    cookbook = require_cookbook_access(db, user_id=owner_id, cookbook_id=cookbook_id, need="owner")

    invitee = users_service.get_user_by_email(db, email)
    if invitee is None:
        raise ApiError(404, "recipient_not_found", "No account with that email.")

    if invitee.id == cookbook.owner_id:
        raise ApiError(
            422,
            "validation_error",
            "The owner is already implicitly a member of their own cookbook.",
        )

    existing = db.get(CookbookMember, (cookbook_id, invitee.id))
    if existing is not None:
        existing.role = role
        db.flush()
        return existing

    member = CookbookMember(
        cookbook_id=cookbook_id,
        user_id=invitee.id,
        role=role,
        added_by=owner_id,
    )
    db.add(member)
    db.flush()
    return member


def set_cookbook_member_role(
    db: Session,
    cookbook_id: uuid.UUID,
    owner_id: uuid.UUID,
    member_user_id: uuid.UUID,
    role: CookbookRole,
) -> CookbookMember:
    """Change a member's role. Owner-only (404 otherwise).

    Raises ApiError 404 if member_user_id has no membership row for this
    cookbook. Flushes; caller owns commit.
    """
    require_cookbook_access(db, user_id=owner_id, cookbook_id=cookbook_id, need="owner")
    member = db.get(CookbookMember, (cookbook_id, member_user_id))
    if member is None:
        raise ApiError(
            404, "not_found", f"Member {member_user_id} not found in cookbook {cookbook_id}."
        )
    member.role = role
    db.flush()
    return member


def remove_cookbook_member(
    db: Session,
    cookbook_id: uuid.UUID,
    owner_id: uuid.UUID,
    member_user_id: uuid.UUID,
) -> None:
    """Remove a member from a cookbook. Owner-only (404 otherwise).

    Idempotent — removing a user who isn't (or is no longer) a member is a
    no-op, not an error. Flushes; caller owns commit.
    """
    require_cookbook_access(db, user_id=owner_id, cookbook_id=cookbook_id, need="owner")
    member = db.get(CookbookMember, (cookbook_id, member_user_id))
    if member is not None:
        db.delete(member)
        db.flush()


def leave_cookbook(db: Session, cookbook_id: uuid.UUID, user_id: uuid.UUID) -> None:
    """Remove the caller's own membership row from a cookbook.

    No access-level check — any caller may always remove their own
    membership row. Idempotent — a no-op if user_id was never a member (or
    is the owner, who has no member row to remove). Flushes; caller owns
    commit.
    """
    member = db.get(CookbookMember, (cookbook_id, user_id))
    if member is not None:
        db.delete(member)
        db.flush()


# ---------------------------------------------------------------------------
# Recipe placements (`cookbook_recipes`) — adding/removing a recipe to/from a
# cookbook, the per-cookbook actions of the boards model.
#
# Gating, per the design spec:
#   - add:    editor+ on the TARGET cookbook, AND read access to the recipe
#             ("you can add a recipe you can see"). Idempotent.
#   - remove: editor+ on the cookbook the placement is being dropped from.
#             Never deletes the recipe; refuses (409) when it would leave the
#             recipe in zero cookbooks — "delete the recipe" is the separate,
#             owner-only action (`delete_recipe`).
# ---------------------------------------------------------------------------


def _placement_insert(*, cookbook_id: uuid.UUID, recipe_id: uuid.UUID, added_by: uuid.UUID) -> Any:
    """An idempotent INSERT of one join row (no-op when the placement exists).

    ``ON CONFLICT DO NOTHING`` on the composite PK rather than a
    read-then-insert, so two concurrent "save to cookbook" clicks can't race
    into a duplicate-key error.
    """
    return (
        pg_insert(CookbookRecipe)
        .values(cookbook_id=cookbook_id, recipe_id=recipe_id, added_by=added_by)
        .on_conflict_do_nothing(index_elements=["cookbook_id", "recipe_id"])
    )


def add_recipe_to_cookbook(
    db: Session,
    *,
    user_id: uuid.UUID,
    recipe_id: uuid.UUID,
    cookbook_id: uuid.UUID,
) -> None:
    """Place *recipe_id* in *cookbook_id* (idempotent).

    Requires editor+ access to *cookbook_id* AND read (viewer+) access to the
    recipe — both raise ApiError 404 on failure, the module's usual not-found
    discipline (never leaking that the cookbook/recipe exists but belongs to
    someone else). Note the recipe-side check is the SET-based one, so a recipe
    is addable as soon as ANY cookbook holding it is readable by the caller
    (e.g. a public cookbook) — that is exactly the "save someone's public
    recipe" path, which for a *reference* (rather than copy-on-save) placement
    is the caller's decision to make, not this function's.

    Leaves ``recipes.cookbook_id`` (the transitional primary placement) alone —
    adding a placement never re-homes the recipe. Flushes; caller owns commit.
    """
    require_cookbook_access(db, user_id=user_id, cookbook_id=cookbook_id, need=CookbookRole.editor)
    recipe = db.get(Recipe, recipe_id)
    if recipe is None:
        raise ApiError(404, "not_found", f"Recipe {recipe_id} not found.")
    _require_recipe_access(db, user_id=user_id, recipe=recipe, need=CookbookRole.viewer)
    db.execute(_placement_insert(cookbook_id=cookbook_id, recipe_id=recipe_id, added_by=user_id))
    db.flush()


def remove_recipe_from_cookbook(
    db: Session,
    *,
    user_id: uuid.UUID,
    recipe_id: uuid.UUID,
    cookbook_id: uuid.UUID,
) -> None:
    """Drop *recipe_id*'s placement in *cookbook_id*. NEVER deletes the recipe.

    Requires editor+ access to *cookbook_id* (404 otherwise).

    Raises ApiError 404 when the recipe is not in *cookbook_id* — including
    when it doesn't exist at all, and including a second removal of the same
    placement. The two cases are deliberately INDISTINGUISHABLE: 404-ing only
    the nonexistent one would hand any editor of any cookbook an existence
    oracle for arbitrary recipe ids ("is this uuid a real recipe?"). Same
    not-found discipline as ``add_recipe_to_cookbook`` and the rest of the
    module — the cost is that removal is not idempotent, which a DELETE
    endpoint can absorb by mapping 404 to "already gone".

    Raises ApiError 409 ``last_placement`` when *cookbook_id* is the recipe's
    ONLY placement — every recipe must stay in at least one cookbook, and
    "delete the recipe entirely" is the separate owner-only action
    (``delete_recipe``). This invariant lives here, not in the database. (No
    oracle here: reaching it already required editor+ on a cookbook the recipe
    is in, so the caller can see the recipe anyway.)

    TRANSITION: when the dropped placement is also the recipe's primary
    (``recipes.cookbook_id``, still NOT NULL), that column is repointed at one
    of the remaining placements — otherwise the transitional
    ``join ∪ {cookbook_id}`` set would keep reporting the cookbook the recipe
    was just removed from, and access would survive the removal. The remaining
    placement is picked deterministically (oldest ``added_at``, then id), and
    exists by construction: this line is only reached when the set had ≥ 2
    entries. Flushes; caller owns commit.
    """
    require_cookbook_access(db, user_id=user_id, cookbook_id=cookbook_id, need=CookbookRole.editor)

    # One check for both the nonexistent-recipe and the not-in-this-cookbook
    # cases: a nonexistent recipe simply has an empty placement set.
    placements = cookbooks_for_recipe(db, recipe_id)
    if cookbook_id not in placements:
        raise ApiError(404, "not_found", f"Recipe {recipe_id} not found in this cookbook.")
    recipe = db.get(Recipe, recipe_id)
    assert recipe is not None  # it has a placement, so the row exists
    if len(placements) == 1:
        raise ApiError(
            409,
            "last_placement",
            f"Recipe {recipe_id} is only in this cookbook; delete the recipe instead.",
        )

    join_row = db.get(CookbookRecipe, (cookbook_id, recipe_id))
    if join_row is not None:
        db.delete(join_row)
        db.flush()

    if recipe.cookbook_id == cookbook_id:
        replacement = db.scalar(
            select(CookbookRecipe.cookbook_id)
            .where(
                CookbookRecipe.recipe_id == recipe_id,
                CookbookRecipe.cookbook_id != cookbook_id,
            )
            .order_by(CookbookRecipe.added_at, CookbookRecipe.cookbook_id)
            .limit(1)
        )
        assert replacement is not None  # guaranteed by the len(placements) > 1 check above
        recipe.cookbook_id = replacement
    db.flush()


def save_recipe_to_cookbook(
    db: Session, *, user_id: uuid.UUID, recipe_id: uuid.UUID, cookbook_id: uuid.UUID
) -> tuple[uuid.UUID, bool]:
    """Save/pin *recipe_id* into *cookbook_id* — the social "Save" action.

    The design spec's "Save / pin semantics" branch:

    - ``recipe.owner_id == user_id`` (the caller's OWN recipe): a plain
      REFERENCE placement via ``add_recipe_to_cookbook`` — editor+ on
      *cookbook_id* required (404 otherwise), idempotent, no copy. The
      caller already owns the row, so nothing about ownership widens.
    - otherwise (someone else's recipe): the caller must still be able to
      READ *recipe_id* via the set-based rule (any cookbook currently
      holding it, e.g. a public/shared one) — 404 if not. The recipe is then
      DEEP-COPIED via ``copy_recipe`` straight into *cookbook_id* (editor+
      required there too, enforced by ``copy_recipe`` itself), so the caller
      ends up owning an independent copy rather than gaining edit rights on
      someone else's row. Cross-owner *reference* is a deliberate non-goal
      of this phase — see the spec.

    Returns ``(resulting_recipe_id, copied)``: the id actually placed (the
    source's own id for a reference, the new copy's id when copied) and
    whether a copy was made — the router surfaces both so a client can
    navigate to the copy it just created.
    """
    recipe = db.get(Recipe, recipe_id)
    if recipe is None:
        raise ApiError(404, "not_found", f"Recipe {recipe_id} not found.")

    if recipe.owner_id == user_id:
        add_recipe_to_cookbook(db, user_id=user_id, recipe_id=recipe_id, cookbook_id=cookbook_id)
        return recipe_id, False

    _require_recipe_access(db, user_id=user_id, recipe=recipe, need=CookbookRole.viewer)
    provenance = {
        "saved_from_owner_id": str(recipe.owner_id),
        "saved_at": datetime.now(UTC).isoformat(),
        "origin_recipe_id": str(recipe_id),
    }
    copied = copy_recipe(
        db,
        recipe_id,
        new_owner_id=user_id,
        cookbook_id=cookbook_id,
        provenance=provenance,
    )
    return copied.id, True


def readable_cookbooks_for_recipe(
    db: Session, *, user_id: uuid.UUID, recipe_id: uuid.UUID
) -> list[RecipeCookbookOut]:
    """Cookbooks *recipe_id* is placed in that *user_id* may THEMSELVES read.

    ``cookbooks_for_recipe`` is unscoped (every placement, regardless of
    caller); each candidate cookbook here is filtered through this caller's
    OWN ``cookbook_access`` (owner/member/public-or-unlisted-viewer) before
    being returned — a stranger's private cookbook that also holds this
    recipe (e.g. the removed-contributor scenario, where the recipe's owner
    can still read the recipe itself but not that cookbook) must never leak
    its id, name, or visibility to a caller who cannot see it.

    Raises ApiError 404 if *recipe_id* doesn't exist, or the caller has no
    read access to the recipe AT ALL (the module's usual not-found
    discipline). Returns ``[]`` (not 404) when the recipe IS readable by the
    caller (e.g. via ``recipes.owner_id``) but none of its current
    placements individually are.
    """
    recipe = db.get(Recipe, recipe_id)
    if recipe is None:
        raise ApiError(404, "not_found", f"Recipe {recipe_id} not found.")
    _require_recipe_access(db, user_id=user_id, recipe=recipe, need=CookbookRole.viewer)

    cookbook_ids = cookbooks_for_recipe(db, recipe_id)
    if not cookbook_ids:
        return []
    cookbooks = db.scalars(
        select(Cookbook)
        .where(Cookbook.id.in_(cookbook_ids))
        .order_by(func.lower(Cookbook.name), Cookbook.id)
    ).all()
    out: list[RecipeCookbookOut] = []
    for cookbook in cookbooks:
        access = _resolve_cookbook_access(db, user_id=user_id, cookbook=cookbook)
        if access is not None:
            out.append(
                RecipeCookbookOut(
                    id=cookbook.id,
                    name=cookbook.name,
                    visibility=str(cookbook.visibility),
                    role=str(access),
                )
            )
    return out


# ---------------------------------------------------------------------------
# Vocab helpers
# ---------------------------------------------------------------------------


def _get_or_create_vocab(db: Session, model: Any, name: str) -> Any:
    """Get or create a vocab row (Cuisine/DishType/Tag) by case-insensitive name.

    Lookup is case-insensitive (stripped + lowered); on insert the first-seen
    casing is preserved as given (stripped only).
    """
    stripped = name.strip()
    row = db.scalars(select(model).where(func.lower(model.name) == stripped.lower())).first()
    if row is None:
        row = model(name=stripped)
        db.add(row)
        db.flush()
    return row


# ---------------------------------------------------------------------------
# Eager-load option builders
# ---------------------------------------------------------------------------

_RECIPE_FULL_OPTIONS = [
    selectinload(Recipe.ingredient_groups).selectinload(IngredientGroup.ingredient_lines),
    selectinload(Recipe.steps),
    selectinload(Recipe.cuisines),
    selectinload(Recipe.dish_types),
    selectinload(Recipe.tags),
    selectinload(Recipe.collections),
]

_RECIPE_SUMMARY_OPTIONS = [
    selectinload(Recipe.dish_types),
]


def _load_recipe_full(db: Session, recipe_id: uuid.UUID) -> Recipe | None:
    """Load a recipe with all relationships eagerly.

    Expires any cached instance first so selectinload re-fetches from the DB.
    """
    # Expire any stale identity-map entry so selectinload re-runs its queries
    existing = db.get(Recipe, recipe_id)
    if existing is not None:
        db.expire(existing)
    stmt = select(Recipe).where(Recipe.id == recipe_id).options(*_RECIPE_FULL_OPTIONS)
    recipe = db.scalars(stmt).first()
    if recipe is not None:
        _attach_canonical_names(db, recipe)
    return recipe


def _attach_canonical_names(db: Session, recipe: Recipe) -> None:
    """Stamp each line with its linked ingredient's name (transient attribute).

    IngredientLineOut reads this via its ``canonical_name`` validation alias, so
    the review editor can round-trip the catalog link on save. Done in one batch
    query (no cross-module relationship — boundary stays clean).
    """
    lines = [line for group in recipe.ingredient_groups for line in group.ingredient_lines]
    ids = {line.canonical_ingredient_id for line in lines if line.canonical_ingredient_id}
    names = catalog_service.names_for_ids(db, ids)
    for line in lines:
        line.canonical_name = (  # type: ignore[attr-defined]
            names.get(line.canonical_ingredient_id) if line.canonical_ingredient_id else None
        )


# ---------------------------------------------------------------------------
# Line / group helpers
# ---------------------------------------------------------------------------


def _process_line(
    db: Session,
    line_in: IngredientLineIn,
    *,
    llm: LLMClient | None = None,
    corpus: catalog_service.FuzzyCorpus | None = None,
) -> dict[str, Any]:
    """Resolve catalog match + normalization for a single ingredient line.

    Returns a dict of column values (excluding group_id and order_index).
    Uses match_or_create for fuzzy + LLM-assisted matching when *llm* is provided.
    *corpus* is the caller's shared fuzzy corpus, so a recipe's lines share one
    catalog snapshot instead of each rebuilding it (see FuzzyCorpus).
    """
    canonical_ingredient_id: uuid.UUID | None = None
    normalized_amount: float | None = None
    normalized_unit: str | None = None
    is_approx: bool = False

    if line_in.name:
        ingredient = catalog_service.match_or_create(db, line_in.name, llm=llm, corpus=corpus)
        canonical_ingredient_id = ingredient.id

        # Attempt unit conversion when quantity + unit are present
        if line_in.quantity is not None and line_in.unit is not None:
            converted = convert_to_normalized(float(line_in.quantity), line_in.unit, ingredient)
            if converted is not None:
                normalized_amount = converted.amount
                normalized_unit = converted.unit
                is_approx = converted.is_approx

    return {
        "original_text": line_in.original_text,
        "quantity": line_in.quantity,
        "unit": line_in.unit,
        "note": line_in.note,
        "is_optional": line_in.is_optional,
        "canonical_ingredient_id": canonical_ingredient_id,
        "normalized_amount": normalized_amount,
        "normalized_unit": normalized_unit,
        "is_approx": is_approx,
    }


def _apply_groups(
    db: Session,
    recipe: Recipe,
    data: RecipeIn,
    *,
    llm: LLMClient | None = None,
) -> None:
    """Build/replace ingredient groups and lines from RecipeIn.

    Existing groups/lines are not touched here — caller must ensure a clean state
    (either new recipe or after removing old groups via delete-orphan cascade).
    Passes *llm* to _process_line for fuzzy + LLM-assisted catalog matching.

    All lines share one ``FuzzyCorpus``: catalog matching only builds it when a
    line misses the exact/alias index, and building it reads the whole alias +
    canonical corpus, so per-line instances turned one recipe save into dozens of
    full-catalog reads. It is created here (per call, never cached across
    requests) so it stays a snapshot of this transaction.
    """
    corpus = catalog_service.FuzzyCorpus()

    for g_idx, group_in in enumerate(data.groups):
        group = IngredientGroup(
            recipe_id=recipe.id,
            name=group_in.name,
            order_index=g_idx,
        )
        db.add(group)
        db.flush()  # get group.id

        for l_idx, line_in in enumerate(group_in.lines):
            line_data = _process_line(db, line_in, llm=llm, corpus=corpus)
            line = IngredientLine(
                group_id=group.id,
                order_index=l_idx,
                **line_data,
            )
            db.add(line)

    # Flush all lines at once
    db.flush()


def _dedupe_names(names: list[str]) -> list[str]:
    """Dedupe names case-insensitively, preserving order and first-seen casing."""
    seen: dict[str, str] = {}
    for n in names:
        stripped = n.strip()
        seen.setdefault(stripped.lower(), stripped)
    return list(seen.values())


def _apply_vocab(db: Session, recipe: Recipe, data: RecipeIn) -> None:
    """Sync vocab many-to-many relationships from RecipeIn (deduped per request)."""
    recipe.cuisines = [_get_or_create_vocab(db, Cuisine, n) for n in _dedupe_names(data.cuisines)]
    recipe.dish_types = [
        _get_or_create_vocab(db, DishType, n) for n in _dedupe_names(data.dish_types)
    ]
    recipe.tags = [_get_or_create_vocab(db, Tag, n) for n in _dedupe_names(data.tags)]


def _apply_steps(db: Session, recipe: Recipe, data: RecipeIn) -> None:
    """Build/replace steps from RecipeIn."""
    for s_idx, step_in in enumerate(data.steps):
        step = Step(
            recipe_id=recipe.id,
            order_index=s_idx,
            original_text=step_in.original_text,
        )
        db.add(step)
    db.flush()


# ---------------------------------------------------------------------------
# Public CRUD operations
# ---------------------------------------------------------------------------


def create_recipe(
    db: Session,
    *,
    owner_id: uuid.UUID,
    data: RecipeIn,
    cookbook_id: uuid.UUID | None = None,
    source_fingerprint: str | None = None,
    source: str | None = None,
    source_type: SourceType = SourceType.manual,
    llm: LLMClient | None = None,
    extraction_meta: dict[str, Any] | None = None,
    image_ref: str | None = None,
    derived_from: uuid.UUID | None = None,
    provenance: dict[str, Any] | None = None,
) -> RecipeOut:
    """Create a new recipe and return a fully-populated RecipeOut.

    - ``cookbook_id``: when given, *owner_id* must have editor+ access to that
      cookbook (``require_cookbook_access(..., need=CookbookRole.editor)`` —
      404 if the cookbook doesn't exist, or the caller has only viewer access,
      or no claim on it at all). When omitted, the recipe lands in
      *owner_id*'s own default cookbook (``ensure_default_cookbook``), created
      on first use — this is what keeps every existing caller that doesn't
      pass a cookbook_id working unchanged.
    - Fingerprint check first → DuplicateRecipeError if already exists for this owner.
    - Vocab rows get-or-created.
    - Ingredient lines: catalog-matched (or unreviewed created), normalized when possible.
    - extraction_meta / image_ref: provenance from the extraction pipeline (None for manual).
    - derived_from / provenance: set when this recipe was produced FROM another recipe rather
      than an external source — currently only `ai.service.transform_recipe` (a recipe transform
      draft, e.g. "make it vegan"). None/None for every other caller (extraction, manual create).
      Mirrors `copy_recipe`'s `provenance` convention but as an optional pass-through here rather
      than always-set, since most `create_recipe` callers have no such lineage to record.
    - Flushes; caller owns commit.
    """
    # Fingerprint uniqueness check
    if source_fingerprint is not None:
        existing = db.scalars(
            select(Recipe).where(
                Recipe.owner_id == owner_id,
                Recipe.source_fingerprint == source_fingerprint,
            )
        ).first()
        if existing is not None:
            raise DuplicateRecipeError(existing_id=existing.id)

    target_cookbook = (
        require_cookbook_access(
            db, user_id=owner_id, cookbook_id=cookbook_id, need=CookbookRole.editor
        )
        if cookbook_id is not None
        else ensure_default_cookbook(db, owner_id)
    )

    recipe = Recipe(
        owner_id=owner_id,
        cookbook_id=target_cookbook.id,
        title=data.title,
        description=data.description,
        language=data.language,
        source=source,
        source_type=source_type,
        source_fingerprint=source_fingerprint,
        extraction_meta=extraction_meta,
        image_ref=image_ref,
        servings_amount=data.servings.amount if data.servings else None,
        servings_unit_text=data.servings.unit_text if data.servings else None,
        prep_min=data.prep_min,
        cook_min=data.cook_min,
        total_min=data.total_min,
        derived_from=derived_from,
        provenance=provenance,
    )
    db.add(recipe)
    db.flush()  # get recipe.id

    # DUAL-WRITE (transition): the recipe's placement lives both in
    # `recipes.cookbook_id` above and as a `cookbook_recipes` join row, so
    # set-based reads work for new rows before the backfill migration runs.
    db.execute(
        _placement_insert(cookbook_id=target_cookbook.id, recipe_id=recipe.id, added_by=owner_id)
    )

    _apply_groups(db, recipe, data, llm=llm)
    _apply_steps(db, recipe, data)
    _apply_vocab(db, recipe, data)
    db.flush()

    # Re-fetch with all eager-loaded relationships
    loaded = _load_recipe_full(db, recipe.id)
    assert loaded is not None
    return RecipeOut.model_validate(loaded)


def get_recipe(
    db: Session,
    *,
    owner_id: uuid.UUID,
    recipe_id: uuid.UUID,
) -> RecipeOut:
    """Fetch a recipe by id. Raises ApiError 404 if not found or the caller has no access.

    Access is the SET-based rule (see ``_recipe_access``): despite the
    parameter name (kept as ``owner_id`` for backward compatibility with every
    existing call site — ingestion, sharing, the router's GET and /scaled
    endpoints), any caller with at least viewer access to ANY cookbook holding
    the recipe — owner, editor, or viewer member, or a public/unlisted
    viewer — can read it, as can the recipe's own owner (``recipes.owner_id``),
    and nobody else. A caller with no claim gets the same 404 as a nonexistent
    id — this function never reveals whether a recipe merely belongs to
    someone else.
    """
    loaded = _load_recipe_full(db, recipe_id)
    if loaded is None:
        raise ApiError(404, "not_found", f"Recipe {recipe_id} not found.")
    _require_recipe_access(db, user_id=owner_id, recipe=loaded, need=CookbookRole.viewer)
    return RecipeOut.model_validate(loaded)


def get_recipe_unscoped(db: Session, recipe_id: uuid.UUID) -> RecipeOut:
    """Fetch a recipe by id only — NO owner check.

    For trusted internal callers that have already established the caller's
    right to view this *specific* recipe through an out-of-band mechanism
    (e.g. `sharing.service` resolving a valid, unrevoked public-link token).
    Never wire this to an authenticated user-facing route directly — that
    would reintroduce the exact "recipe visible ⇔ owner" hole sharing is
    meant to punch through deliberately, not accidentally.

    Raises ApiError 404 if *recipe_id* doesn't exist.
    """
    loaded = _load_recipe_full(db, recipe_id)
    if loaded is None:
        raise ApiError(404, "not_found", f"Recipe {recipe_id} not found.")
    return RecipeOut.model_validate(loaded)


def get_recipe_image_ref_unscoped(db: Session, recipe_id: uuid.UUID) -> str | None:
    """Fetch the RAW (unprefixed) ``image_ref`` column for *recipe_id* — NO owner check.

    Trusted internal helper, mirroring ``get_recipe_unscoped``'s trust
    boundary: callers must have already established the caller's right to
    view this specific recipe through an out-of-band mechanism (e.g.
    `sharing.service` resolving a valid, unrevoked public-link token).

    Deliberately returns the bare content-addressed ref (e.g.
    ``"sha16/sha.jpg"``), NOT the ``/api/files/{ref}`` URL that
    ``RecipeOut.image_ref`` computes — callers here want to hand the ref
    straight to a ``FileStore`` to serve the bytes themselves, not a URL for
    a browser to follow (which would require auth).

    Raises ApiError 404 if *recipe_id* doesn't exist. Returns None (not an
    error) if the recipe exists but has no image.
    """
    recipe = db.get(Recipe, recipe_id)
    if recipe is None:
        raise ApiError(404, "not_found", f"Recipe {recipe_id} not found.")
    return recipe.image_ref


def recipe_titles_for_ids(db: Session, ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    """Map recipe ids to their titles (for callers rendering a list of refs)."""
    if not ids:
        return {}
    rows = db.execute(select(Recipe.id, Recipe.title).where(Recipe.id.in_(ids))).all()
    return {row.id: row.title for row in rows}


def recipe_summaries_for_ids(
    db: Session, ids: list[uuid.UUID], *, viewer_id: uuid.UUID | None
) -> dict[uuid.UUID, RecipeSummary]:
    """Return RecipeSummary rows for the given ids, UNSCOPED by owner.

    Trusted internal helper, mirroring ``get_recipe_unscoped``'s trust
    boundary: callers (cookbook_router, for a cookbook's recipe list) must
    have already established the caller's right to see each of these
    specific recipes through their own access-control layer (cookbook
    membership/visibility) before calling this — it does no authorization
    itself. Ids that don't resolve to a recipe (e.g. deleted meanwhile) are
    silently omitted, not raised.

    *viewer_id* is who the rows are being rendered FOR (None = anonymous). It
    grants nothing; it only decides whose ``is_favorite`` is truthful — see
    ``_summary_for``. Required (no default) so a caller can't accidentally
    hand one user another's personal flag by forgetting it, and so this and
    ``list_recipes`` can never disagree about the same recipe.
    """
    if not ids:
        return {}
    stmt = select(Recipe).where(Recipe.id.in_(ids)).options(*_RECIPE_SUMMARY_OPTIONS)
    recipes = db.scalars(stmt).all()
    return {r.id: _summary_for(r, viewer_id=viewer_id) for r in recipes}


def copy_recipe(
    db: Session,
    recipe_id: uuid.UUID,
    *,
    new_owner_id: uuid.UUID,
    provenance: dict[str, Any],
    cookbook_id: uuid.UUID | None = None,
) -> Recipe:
    """Deep-copy a recipe into a new owner's cookbook (the copy-on-share primitive).

    No ownership check here — this is service-internal; the caller (the
    sharing service, or ``save_recipe_to_cookbook`` below) is responsible for
    verifying the caller may read *recipe_id* before calling this. Raises
    ApiError 404 if *recipe_id* doesn't exist at all.

    The copy is filed in *cookbook_id* when given — the caller must have
    editor+ access to it (404 otherwise), the same gate
    ``add_recipe_to_cookbook`` uses for a reference placement. This is what
    lets ``save_recipe_to_cookbook`` copy someone else's recipe straight into
    the SPECIFIC cookbook the caller chose to save it into, rather than
    always landing in their default one.

    When *cookbook_id* is omitted (every pre-existing caller —
    ``sharing.service.share_recipe``), the copy is filed in *new_owner_id*'s
    own default cookbook (``ensure_default_cookbook``, created on first use)
    instead — NEVER the source recipe's cookbook, which belongs to the sharer
    and whose members must not silently gain access to the recipient's copy.
    This is also what keeps copy-on-share from minting cookbook-less recipes
    (``recipes.cookbook_id`` is NOT NULL as of the finalize migration).

    Copied verbatim: title/description/servings/prep_min/cook_min/total_min/
    language/source/source_type, image_ref (shared BY REFERENCE — the
    content-addressed file itself is not duplicated, both recipes just point
    at the same store key), ingredient groups + lines (every column,
    including canonical_ingredient_id and the already-computed
    normalized_amount/normalized_unit — re-running catalog matching would be
    wasteful and could drift from what the sharer actually saw), steps (with
    ingredient_line_refs remapped, see below), cuisines/dish_types/tags (the
    same vocab rows are simply re-linked — vocab is a shared, deduped table,
    not owned per-recipe), and is_verified (the copy inherits the sharer's
    verification state).

    Deliberately NOT copied — the copy is an independent snapshot, not a
    live-linked twin:
      - favorites/notes: left at their model defaults (False/None) —
        personal metadata belongs to whoever owns the row, not the original.
      - collections: left empty — collections are the new owner's own
        organizational scheme, unrelated to the sharer's.
      - source_fingerprint: forced to None, so the recipient can still
        independently import the same original source (e.g. re-scrape the
        same URL) later without tripping the (owner_id, source_fingerprint)
        uniqueness constraint against a fingerprint that was really the
        SHARER's, not theirs.
      - extraction_meta: dropped — it references the sharer's ingestion job
        (tier used, LLM cost, etc.), which is meaningless to the recipient.
      - derived_from / last_edited_by / last_edited_at: left at defaults —
        the copy hasn't been edited by anyone yet.

    ``provenance`` is stored as-is on the new row; the sharing service builds
    it as ``{"shared_by": ..., "shared_at": ..., "origin_recipe_id": ...}``.

    Ingredient-line-ref remapping: ``Step.ingredient_line_refs`` is a loose
    (non-FK) JSONB list of ``IngredientLine.id`` strings (see
    cookbook/models.py's Step docstring). Every ingredient line gets a brand
    new id in the copy, so a ref that pointed at an original line would
    dangle (or, worse, coincidentally collide with an unrelated line) if
    left as-is. While copying lines we build an old-line-id -> new-line-id
    string map, then rewrite each step's refs through it; any ref that
    doesn't resolve (refs are only ever meant to be line ids from the same
    recipe, so this shouldn't happen, but a stale/malformed ref is possible)
    is dropped rather than left pointing at a line in someone else's
    cookbook.

    Flushes; caller owns commit.
    """
    source = _load_recipe_full(db, recipe_id)
    if source is None:
        raise ApiError(404, "not_found", f"Recipe {recipe_id} not found.")

    target_cookbook = (
        require_cookbook_access(
            db, user_id=new_owner_id, cookbook_id=cookbook_id, need=CookbookRole.editor
        )
        if cookbook_id is not None
        else ensure_default_cookbook(db, new_owner_id)
    )

    new_recipe = Recipe(
        owner_id=new_owner_id,
        cookbook_id=target_cookbook.id,
        title=source.title,
        description=source.description,
        image_ref=source.image_ref,
        source=source.source,
        source_type=source.source_type,
        language=source.language,
        servings_amount=source.servings_amount,
        servings_unit_text=source.servings_unit_text,
        prep_min=source.prep_min,
        cook_min=source.cook_min,
        total_min=source.total_min,
        is_verified=source.is_verified,
        provenance=provenance,
        source_fingerprint=None,
        extraction_meta=None,
    )
    db.add(new_recipe)
    db.flush()  # get new_recipe.id

    # DUAL-WRITE (transition) — same reasoning as create_recipe: the copy is
    # placed in the new owner's default cookbook both ways. `added_by` is the
    # RECIPIENT (they are the one placing this copy in their own cookbook), not
    # the sharer.
    db.execute(
        _placement_insert(
            cookbook_id=target_cookbook.id, recipe_id=new_recipe.id, added_by=new_owner_id
        )
    )

    line_id_map: dict[str, str] = {}
    for group in source.ingredient_groups:
        new_group = IngredientGroup(
            recipe_id=new_recipe.id,
            name=group.name,
            order_index=group.order_index,
        )
        db.add(new_group)
        db.flush()  # get new_group.id

        for line in group.ingredient_lines:
            new_line = IngredientLine(
                group_id=new_group.id,
                order_index=line.order_index,
                original_text=line.original_text,
                quantity=line.quantity,
                unit=line.unit,
                canonical_ingredient_id=line.canonical_ingredient_id,
                normalized_amount=line.normalized_amount,
                normalized_unit=line.normalized_unit,
                is_approx=line.is_approx,
                note=line.note,
                is_optional=line.is_optional,
            )
            db.add(new_line)
            db.flush()  # get new_line.id for the ref map
            line_id_map[str(line.id)] = str(new_line.id)

    for step in source.steps:
        remapped_refs = [
            line_id_map[ref] for ref in step.ingredient_line_refs if ref in line_id_map
        ]
        new_step = Step(
            recipe_id=new_recipe.id,
            order_index=step.order_index,
            original_text=step.original_text,
            ingredient_line_refs=remapped_refs,
        )
        db.add(new_step)

    new_recipe.cuisines = list(source.cuisines)
    new_recipe.dish_types = list(source.dish_types)
    new_recipe.tags = list(source.tags)

    db.flush()
    return new_recipe


# Trigram similarity threshold for ingredient-line matching only. Title search
# uses the `%` operator instead (see list_recipes), whose threshold is governed
# by the session GUC `pg_trgm.similarity_threshold` (default 0.3) so that it
# can be satisfied by the `gin_trgm_ops` index; ingredient lines have no such
# index, so `similarity() > threshold` (not index-accelerated) is fine there
# and free to keep its own, more permissive, value.
_INGREDIENT_TRIGRAM_THRESHOLD = 0.25

_VALID_DIETARY_FILTERS = frozenset({"vegan", "vegetarian", "gluten_free"})


def _readable_cookbook_ids(user_id: uuid.UUID) -> Any:
    """A SELECT of every cookbook id *user_id* owns or is a member of.

    Used as an ``IN (...)`` subquery by ``list_recipes``; Postgres plans it as
    a semi-join, so no ids are materialized in Python. Owned and member-of
    only — see ``list_recipes``'s docstring for why public/unlisted cookbooks
    the caller isn't a member of are excluded.
    """
    return select(Cookbook.id).where(
        or_(
            Cookbook.owner_id == user_id,
            exists(
                select(1).where(
                    CookbookMember.cookbook_id == Cookbook.id,
                    CookbookMember.user_id == user_id,
                )
            ),
        )
    )


def list_recipes(
    db: Session,
    *,
    owner_id: uuid.UUID,
    q: str | None = None,
    cuisine: str | None = None,
    dish_type: str | None = None,
    tag: str | None = None,
    dietary: str | None = None,
    max_total_min: int | None = None,
    source_type: SourceType | str | None = None,
    favorites: bool | None = None,
    collection: uuid.UUID | None = None,
    limit: int = 1000,
    offset: int = 0,
) -> RecipePage:
    """Return a page of recipes *owner_id* can READ, newest first, filters ANDed.

    SCOPE (the flat compat list, boards-aware): every recipe that appears —
    via the ``cookbook_recipes`` join, OR its still-NOT-NULL legacy
    ``recipes.cookbook_id`` (the same transitional union used everywhere else
    in this module, see ``_recipe_cookbook_ids``) — in ANY cookbook
    *owner_id* owns or is a member of (any role), PLUS every recipe
    *owner_id* owns outright even if its only current placement became
    unreadable to them (e.g. removed as a member of the cookbook holding
    it) — "my stuff + shared-with-me". A recipe placed in SEVERAL cookbooks
    *owner_id* can read appears exactly ONCE: the scope is expressed as a
    per-recipe EXISTS/boolean condition over the ``Recipe`` table itself
    (never a JOIN against ``cookbook_recipes``), so there is nothing to
    de-duplicate — each recipe row is considered exactly once regardless of
    how many of its placements are readable.

    Deliberately NOT a discovery feed: a public/unlisted cookbook the caller
    is not a member of is readable via its own cookbook page but does NOT
    pour its recipes into this list, which would otherwise grow without
    bound as strangers publish cookbooks. ``owner_id`` is kept as the
    parameter name for call-site compatibility; it means "the calling user",
    and a recipe someone else owns can now appear (shared to the caller as
    viewer or editor, or still their own even without a readable placement).

    Search (*q*): a recipe matches when ANY of the following hold —
      1. title/description full-text search: ``to_tsvector('simple', title ||
         ' ' || coalesce(description, '')) @@ websearch_to_tsquery('simple',
         q)``. The query builds this with the same ``||`` (textcat) operator
         used by the ``ix_recipes_title_description_fts`` expression index
         (rather than ``concat()``, a different function that Postgres will
         NOT match to a ``||``-based index) so the GIN index is used.
      2. title trigram similarity via the ``%`` operator:
         ``title % q``, i.e. ``similarity(title, q) >
         pg_trgm.similarity_threshold`` (session GUC, default 0.3). This is
         what lets the planner use the ``ix_recipes_title_trgm`` GIN index;
         the equivalent ``similarity(title, q) > 0.25`` function-call form is
         NOT index-accelerated.
      3. an ingredient line's ``original_text`` has trigram
         ``similarity(original_text, q) > 0.25`` (no index backs this one, so
         it keeps the explicit, more permissive threshold).

    Dietary rule (*dietary* — one of ``"vegan"``, ``"vegetarian"``,
    ``"gluten_free"``): a recipe qualifies when EVERY ingredient line that HAS
    a canonical-ingredient match is compatible with the diet, per
    ``catalog.service.dietary_incompatible_ingredient_ids``. Ingredient lines
    with no canonical match (free-text lines, or lines with no ``name``
    given) do NOT disqualify — they are simply not considered. Note that
    *optional* ingredient lines DO still disqualify when incompatible: an
    optional non-vegan ingredient still appears in the recipe, so it is not
    exempted from the diet check. See catalog/service.py for how the raw
    ``dietary_flags`` tags map to each diet.

    ``limit`` defaults to 1000 (offset 0) — high enough that, for realistic
    cookbook sizes, omitting both params reproduces the pre-pagination
    behavior of returning every recipe; the search/filter UI (next task)
    passes explicit page sizes.

    ``collection`` filters to recipes that are a member of the given
    collection id (correlated EXISTS against ``collection_recipes``,
    ANDed with the rest); an id that isn't the caller's own collection
    simply matches zero recipes rather than raising.

    Raises ``ApiError`` 422 for an unrecognised *dietary* value.
    """
    placed_in_a_readable_cookbook = exists(
        select(1).where(
            CookbookRecipe.recipe_id == Recipe.id,
            CookbookRecipe.cookbook_id.in_(_readable_cookbook_ids(owner_id)),
        )
    )
    conditions: list[Any] = [
        or_(
            placed_in_a_readable_cookbook,
            # TRANSITION: a recipe predating the backfill migration has no
            # join row at all — its legacy primary placement alone decides.
            Recipe.cookbook_id.in_(_readable_cookbook_ids(owner_id)),
            # The caller's own recipes always show up here, even when their
            # only placement(s) became unreadable to them (e.g. removed as a
            # cookbook member) — see the design spec's access model.
            Recipe.owner_id == owner_id,
        )
    ]

    if q and q.strip():
        q_stripped = q.strip()
        # Must be built with the literal `||` (textcat) operator, not
        # func.concat(...) — Postgres matches expression indexes by comparing
        # parsed expression trees, and concat() is a different function than
        # ||, so a concat()-based query would never hit
        # ix_recipes_title_description_fts. The " " / "" literals are forced
        # to render as SQL literals (not bind params) via literal_column so
        # the parsed tree is byte-for-byte the same shape as the index's
        # `title || ' ' || coalesce(description, '')` expression.
        title_desc_concat = Recipe.title.op("||")(literal_column("' '")).op("||")(
            func.coalesce(Recipe.description, literal_column("''"))
        )
        title_desc_tsvector = func.to_tsvector("simple", title_desc_concat)
        fts_match = title_desc_tsvector.op("@@")(func.websearch_to_tsquery("simple", q_stripped))
        # `%` (not `similarity() > threshold`) is what lets the planner use
        # the ix_recipes_title_trgm GIN index; the threshold is then governed
        # by the session GUC pg_trgm.similarity_threshold (default 0.3).
        title_trgm_match = Recipe.title.op("%")(q_stripped)
        ingredient_trgm_match = exists(
            select(1)
            .select_from(IngredientLine)
            .join(IngredientGroup, IngredientLine.group_id == IngredientGroup.id)
            .where(
                IngredientGroup.recipe_id == Recipe.id,
                func.similarity(IngredientLine.original_text, q_stripped)
                > _INGREDIENT_TRIGRAM_THRESHOLD,
            )
        )
        conditions.append(or_(fts_match, title_trgm_match, ingredient_trgm_match))

    if cuisine and cuisine.strip():
        conditions.append(Recipe.cuisines.any(func.lower(Cuisine.name) == cuisine.strip().lower()))

    if dish_type and dish_type.strip():
        conditions.append(
            Recipe.dish_types.any(func.lower(DishType.name) == dish_type.strip().lower())
        )

    if tag and tag.strip():
        conditions.append(Recipe.tags.any(func.lower(Tag.name) == tag.strip().lower()))

    if dietary is not None:
        if dietary not in _VALID_DIETARY_FILTERS:
            raise ApiError(
                422,
                "validation_error",
                f"Unknown dietary filter {dietary!r}; expected one of "
                f"{sorted(_VALID_DIETARY_FILTERS)}.",
            )
        disqualifying_ids = catalog_service.dietary_incompatible_ingredient_ids(dietary)
        conditions.append(
            ~exists(
                select(1)
                .select_from(IngredientLine)
                .join(IngredientGroup, IngredientLine.group_id == IngredientGroup.id)
                .where(
                    IngredientGroup.recipe_id == Recipe.id,
                    IngredientLine.canonical_ingredient_id.in_(disqualifying_ids),
                )
            )
        )

    if max_total_min is not None:
        conditions.append(Recipe.total_min <= max_total_min)

    if source_type is not None:
        conditions.append(Recipe.source_type == source_type)

    if favorites is not None:
        # `is_favorite` is the OWNER's personal flag (set_personal is
        # owner-only), so it is only ever meaningful about one's own recipes.
        # Now that this list spans shared cookbooks, the filter is pinned to
        # the caller's own rows as well — otherwise "show my favorites" would
        # hand back recipes a co-member starred, which the caller never
        # favorited and cannot un-favorite.
        conditions.append(Recipe.owner_id == owner_id)
        conditions.append(Recipe.is_favorite == favorites)

    if collection is not None:
        # Correlated EXISTS against the association table, like the vocab
        # filters above. No separate ownership check is needed: a recipe can
        # only ever be linked to its own owner's collections (enforced by
        # set_recipe_collections), so an id belonging to another user's
        # collection simply matches nothing here.
        conditions.append(
            exists(
                select(1).where(
                    collection_recipes.c.recipe_id == Recipe.id,
                    collection_recipes.c.collection_id == collection,
                )
            )
        )

    total = db.scalar(select(func.count()).select_from(Recipe).where(*conditions)) or 0

    stmt = (
        select(Recipe)
        .where(*conditions)
        .options(*_RECIPE_SUMMARY_OPTIONS)
        # `Recipe.id` breaks created_at ties so the sort is TOTAL. Required
        # for correct pagination: with a non-deterministic order, two rows
        # sharing a created_at can swap between the page-N and page-N+1
        # queries and be returned twice or skipped entirely. Newly load-
        # bearing since this list started spanning cookbooks — one user's own
        # recipes rarely tie, but rows minted by several users (a bulk import
        # into a shared cookbook, say) frequently do.
        .order_by(Recipe.created_at.desc(), Recipe.id.desc())
        .limit(limit)
        .offset(offset)
    )
    recipes = db.scalars(stmt).all()
    return RecipePage(
        items=[_summary_for(r, viewer_id=owner_id) for r in recipes],
        total=total,
        limit=limit,
        offset=offset,
    )


def _summary_for(recipe: Recipe, *, viewer_id: uuid.UUID | None) -> RecipeSummary:
    """RecipeSummary as seen BY *viewer_id* (None = anonymous), owner flag masked.

    ``is_favorite`` belongs to the recipe's OWNER — someone browsing a recipe
    shared with them never set it and (``set_personal`` being owner-only)
    could not clear it, so surfacing it as if it were theirs would render a
    filled heart whose toggle 404s. Reported as False for anyone but the
    owner, which is every path that renders a summary: the flat
    ``list_recipes`` and ``recipe_summaries_for_ids`` (a cookbook's own recipe
    list) must never disagree about the same recipe.
    """
    summary = RecipeSummary.model_validate(recipe)
    if recipe.owner_id != viewer_id and summary.is_favorite:
        summary = summary.model_copy(update={"is_favorite": False})
    return summary


# ---------------------------------------------------------------------------
# Content-based recommendations (Phase 3 Task 8) — deterministic, NO LLM.
#
# Module placement: this lives here, not in `ai.service`, because the
# weighted-overlap query needs to join directly against the m2m association
# tables (`recipe_cuisines`/`recipe_dish_types`/`recipe_tags`) and
# `IngredientLine.canonical_ingredient_id` — models `ai` is explicitly
# forbidden from importing (see .importlinter's `ai-cannot-import-sibling-
# models` contract, which lists `recipe_normalizer.cookbook.models`).
# `list_recipes`'s public filters (`cuisine=`/`dish_type=`/`tag=`/`dietary=`)
# are yes/no predicates, not "how many things overlap" counts, so they can't
# be reused as-is for scoring; putting this next to `list_recipes` keeps the
# recipe-relationship query logic in one module and lets `ai.router` (or
# `cookbook.router`) call a single well-typed function instead of ai reaching
# past cookbook.service into cookbook.models. Collaborative filtering
# (recommendations informed by OTHER users' cookbooks) is an explicit
# non-goal — every candidate is scored purely against content already in
# *user_id*'s own cookbook.
# ---------------------------------------------------------------------------

#: Weighted-overlap scoring formula (see `recommendations_for_recipe`):
#:
#:     score = WEIGHT_CUISINE    * |shared cuisines|
#:           + WEIGHT_DISH_TYPE  * |shared dish types|
#:           + WEIGHT_TAG        * |shared tags|
#:           + WEIGHT_INGREDIENT * |shared canonical ingredients|
#:
#: Cuisine/dish-type overlap is the strongest "this is a similar recipe"
#: signal (sharing a whole genre, e.g. both "Italian" or both "dessert"), a
#: shared tag a medium signal, and a single shared canonical ingredient the
#: weakest (nearly any two recipes share SOME ingredient, e.g. salt) — hence
#: cuisine/dish_type >> tag > ingredient. These are deliberately plain
#: integers (not normalized/fractional) so the score stays simple to reason
#: about and to test.
WEIGHT_CUISINE = 4
WEIGHT_DISH_TYPE = 4
WEIGHT_TAG = 2
WEIGHT_INGREDIENT = 1

#: Default "More like this" row size (see `recommendations_for_recipe`).
DEFAULT_RECOMMENDATION_LIMIT = 6


def _recipe_assoc_ids(db: Session, recipe_id: uuid.UUID, assoc_table: Any, id_col: str) -> set[Any]:
    """Return the distinct set of vocab ids *recipe_id* is linked to in *assoc_table*.

    One cheap indexed query against a two-column association table (composite
    PK on (recipe_id, <vocab>_id)) — used to resolve the "target" recipe's
    cuisine/dish_type/tag ids before scoring every candidate against them.
    """
    col = assoc_table.c[id_col]
    return set(db.scalars(select(col).where(assoc_table.c.recipe_id == recipe_id)))


def recommendations_for_recipe(
    db: Session,
    *,
    user_id: uuid.UUID,
    recipe_id: uuid.UUID,
    limit: int = DEFAULT_RECOMMENDATION_LIMIT,
) -> list[RecipeSummary]:
    """ "More like this": top *limit* recipes in *user_id*'s OWN cookbook, by content overlap.

    NOT collaborative filtering (an explicit non-goal) and uses NO LLM —
    purely deterministic content similarity, scored per the WEIGHT_* formula
    documented above.

    Access to *recipe_id* itself uses the SAME cookbook-derived check every
    other per-recipe read in this module uses (`recipe_access`) — 404 if no
    claim. Since the cookbooks pivot that means owner of the recipe's
    cookbook, a member of it, OR any caller at all when it is public or
    unlisted (`cookbook_access` resolves visibility to viewer); the pre-pivot
    check was owner ∪ shared-cookbook-member and admitted nobody on
    visibility alone. Wider, but harmless here specifically — this endpoint
    reads no LLM and, per the next paragraph, hands back nothing the caller
    didn't already own.

    The CANDIDATE POOL is unchanged and stays pinned to
    ALWAYS *user_id*'s own recipes (`Recipe.owner_id == user_id`), regardless
    of whether *recipe_id* belongs to *user_id* or was reached through a
    cookbook shared with (or published to) them: someone browsing another
    user's recipe gets recommendations drawn from THEIR OWN cookbook, never
    the other user's. This is both the intended UX (never recommend a recipe
    the viewer can't open) and the safe default — the widened access above
    therefore cannot leak the existence of anyone else's recipes, because no
    one else's recipes can ever appear in the result.

    Excludes *recipe_id* itself. Recipes scoring 0 (no overlap at all) are
    excluded too — an empty list means genuinely nothing in the cookbook
    overlaps, not "give me something anyway." Ties are broken by recency
    (`Recipe.created_at` descending, newest first).

    Query shape: FOUR small upfront queries resolve *recipe_id*'s own
    cuisine/dish_type/tag/canonical-ingredient id sets (each a single indexed
    lookup against a two-column association table), then ONE query scores
    every candidate recipe against those fixed id sets via correlated scalar
    subqueries and returns the top *limit* — never N+1 over the whole
    cookbook regardless of how many recipes *user_id* owns.
    """
    if recipe_access(db, user_id=user_id, recipe_id=recipe_id) is None:
        raise ApiError(404, "not_found", f"Recipe {recipe_id} not found.")

    cuisine_ids = _recipe_assoc_ids(db, recipe_id, recipe_cuisines, "cuisine_id")
    dish_type_ids = _recipe_assoc_ids(db, recipe_id, recipe_dish_types, "dish_type_id")
    tag_ids = _recipe_assoc_ids(db, recipe_id, recipe_tags, "tag_id")
    ingredient_ids: set[uuid.UUID] = set(
        db.scalars(
            select(IngredientLine.canonical_ingredient_id)
            .join(IngredientGroup, IngredientLine.group_id == IngredientGroup.id)
            .where(
                IngredientGroup.recipe_id == recipe_id,
                IngredientLine.canonical_ingredient_id.is_not(None),
            )
            .distinct()
        )
    )

    if not (cuisine_ids or dish_type_ids or tag_ids or ingredient_ids):
        # Nothing to score against — every candidate would score 0 anyway;
        # skip the query entirely rather than running a guaranteed-empty one.
        return []

    def _shared_vocab_count(assoc_table: Any, id_col: str, ids: set[Any]) -> Any:
        if not ids:
            return literal(0)
        col = assoc_table.c[id_col]
        return (
            select(func.count(func.distinct(col)))
            .where(assoc_table.c.recipe_id == Recipe.id, col.in_(ids))
            .correlate(Recipe)
            .scalar_subquery()
        )

    def _shared_ingredient_count(ids: set[uuid.UUID]) -> Any:
        if not ids:
            return literal(0)
        return (
            select(func.count(func.distinct(IngredientLine.canonical_ingredient_id)))
            .select_from(IngredientLine)
            .join(IngredientGroup, IngredientLine.group_id == IngredientGroup.id)
            .where(
                IngredientGroup.recipe_id == Recipe.id,
                IngredientLine.canonical_ingredient_id.in_(ids),
            )
            .correlate(Recipe)
            .scalar_subquery()
        )

    score_expr = (
        _shared_vocab_count(recipe_cuisines, "cuisine_id", cuisine_ids) * WEIGHT_CUISINE
        + _shared_vocab_count(recipe_dish_types, "dish_type_id", dish_type_ids) * WEIGHT_DISH_TYPE
        + _shared_vocab_count(recipe_tags, "tag_id", tag_ids) * WEIGHT_TAG
        + _shared_ingredient_count(ingredient_ids) * WEIGHT_INGREDIENT
    )

    scored = (
        select(Recipe.id, score_expr.label("score"))
        .where(Recipe.owner_id == user_id, Recipe.id != recipe_id)
        .subquery()
    )
    stmt = (
        select(Recipe)
        .join(scored, scored.c.id == Recipe.id)
        .where(scored.c.score > 0)
        .options(*_RECIPE_SUMMARY_OPTIONS)
        .order_by(scored.c.score.desc(), Recipe.created_at.desc())
        .limit(limit)
    )
    recipes = db.scalars(stmt).all()
    return [RecipeSummary.model_validate(r) for r in recipes]


def update_recipe(
    db: Session,
    *,
    owner_id: uuid.UUID,
    recipe_id: uuid.UUID,
    data: RecipeIn,
    editor_id: uuid.UUID,
    llm: LLMClient | None = None,
) -> RecipeOut:
    """Replace a recipe's content wholesale; returns updated RecipeOut.

    Access is the SET-based rule (see ``_recipe_access``): ``owner_id`` (again,
    kept as the param name for compatibility) needs editor+ access to ANY
    cookbook holding the recipe — owner or editor member of at least one — or
    to be the recipe's own owner (``recipes.owner_id``); 404 otherwise (viewer
    members and non-members alike).
    - Replaces groups/lines/steps (delete-orphan cascade handles cleanup).
    - Re-runs catalog matching + normalization.
    - Sets last_edited_by and last_edited_at to *editor_id* — for a member
      edit this is the *editing member's* id (the router always passes
      ``editor_id=current_user.id``), NOT the recipe's owner, so co-owners
      can see who last touched a shared recipe.
    """
    recipe = db.scalars(select(Recipe).where(Recipe.id == recipe_id)).first()
    if recipe is None:
        raise ApiError(404, "not_found", f"Recipe {recipe_id} not found.")
    _require_recipe_access(db, user_id=owner_id, recipe=recipe, need=CookbookRole.editor)

    # Clear existing groups/steps — delete-orphan cascade removes children
    recipe.ingredient_groups.clear()
    recipe.steps.clear()
    db.flush()

    # Update scalar fields
    recipe.title = data.title
    recipe.description = data.description
    recipe.language = data.language
    recipe.servings_amount = data.servings.amount if data.servings else None
    recipe.servings_unit_text = data.servings.unit_text if data.servings else None
    recipe.prep_min = data.prep_min
    recipe.cook_min = data.cook_min
    recipe.total_min = data.total_min
    recipe.last_edited_by = editor_id
    recipe.last_edited_at = datetime.now(UTC)

    db.flush()

    _apply_groups(db, recipe, data, llm=llm)
    _apply_steps(db, recipe, data)
    _apply_vocab(db, recipe, data)
    db.flush()

    loaded = _load_recipe_full(db, recipe_id)
    assert loaded is not None
    return RecipeOut.model_validate(loaded)


def delete_recipe(
    db: Session,
    *,
    owner_id: uuid.UUID,
    recipe_id: uuid.UUID,
) -> None:
    """Delete a recipe (and its children + placements via cascade).

    OWNER-ONLY, on ``recipes.owner_id`` — NOT cookbook-derived access (this is
    the one recipe mutation that doesn't go through ``_require_recipe_access``).
    A recipe now lives in many cookbooks, so deleting the row destroys it for
    every cookbook holding it, including ones the caller has nothing to do
    with; only the recipe's own owner may do that. Anyone else with editor+
    access on a particular cookbook gets the per-cookbook action instead —
    ``remove_recipe_from_cookbook``, which drops that one placement and leaves
    the recipe alone. This TIGHTENS Phase 1's editor+ delete.

    Raises ApiError 404 both for a nonexistent recipe and for one the caller
    doesn't own (same not-found discipline as everywhere else here).
    ``cookbook_recipes`` rows go with the recipe via ON DELETE CASCADE.
    """
    recipe = db.get(Recipe, recipe_id)
    if recipe is None or recipe.owner_id != owner_id:
        raise ApiError(404, "not_found", f"Recipe {recipe_id} not found.")
    db.delete(recipe)
    db.flush()


# ---------------------------------------------------------------------------
# Collections
# ---------------------------------------------------------------------------


def _get_owned_collection(db: Session, owner_id: uuid.UUID, collection_id: uuid.UUID) -> Collection:
    """Fetch a collection by id, owner-scoped. Raises ApiError 404 if not found or wrong owner."""
    collection = db.scalars(
        select(Collection).where(Collection.id == collection_id, Collection.owner_id == owner_id)
    ).first()
    if collection is None:
        raise ApiError(404, "not_found", f"Collection {collection_id} not found.")
    return collection


def _recipe_count(db: Session, collection_id: uuid.UUID) -> int:
    return (
        db.scalar(
            select(func.count())
            .select_from(collection_recipes)
            .where(collection_recipes.c.collection_id == collection_id)
        )
        or 0
    )


def create_collection(db: Session, *, owner_id: uuid.UUID, name: str) -> CollectionOut:
    """Create a new (empty) collection for owner_id.

    Raises DuplicateCollectionNameError (409 duplicate_name) if the owner
    already has a collection with this exact name (stripped, case-sensitive
    — matches the DB-level unique constraint on (owner_id, name); this is a
    check-then-insert like DuplicateRecipeError's fingerprint check, so it
    carries the same narrow race window under concurrent identical requests).
    Flushes; caller owns commit.
    """
    stripped = name.strip()
    existing = db.scalars(
        select(Collection).where(Collection.owner_id == owner_id, Collection.name == stripped)
    ).first()
    if existing is not None:
        raise DuplicateCollectionNameError(stripped)

    collection = Collection(owner_id=owner_id, name=stripped)
    db.add(collection)
    db.flush()
    return CollectionOut(id=collection.id, name=collection.name, recipe_count=0)


def rename_collection(
    db: Session,
    *,
    owner_id: uuid.UUID,
    collection_id: uuid.UUID,
    name: str,
) -> CollectionOut:
    """Rename a collection. Owner-scoped: 404 if not found or wrong owner.

    Raises DuplicateCollectionNameError (409) if another of the owner's
    collections already has the new name. Renaming to the current name (a
    no-op) is always allowed. Flushes; caller owns commit.
    """
    collection = _get_owned_collection(db, owner_id, collection_id)
    stripped = name.strip()
    if stripped != collection.name:
        existing = db.scalars(
            select(Collection).where(
                Collection.owner_id == owner_id,
                Collection.name == stripped,
                Collection.id != collection_id,
            )
        ).first()
        if existing is not None:
            raise DuplicateCollectionNameError(stripped)
        collection.name = stripped
        db.flush()

    return CollectionOut(
        id=collection.id, name=collection.name, recipe_count=_recipe_count(db, collection_id)
    )


def delete_collection(db: Session, *, owner_id: uuid.UUID, collection_id: uuid.UUID) -> None:
    """Delete a collection. Owner-scoped: 404 if not found or wrong owner.

    Only the `collection_recipes` association rows are removed (cascade);
    the recipes themselves are left entirely untouched. Flushes; caller owns
    commit.
    """
    collection = _get_owned_collection(db, owner_id, collection_id)
    db.delete(collection)
    db.flush()


def list_collections(db: Session, *, owner_id: uuid.UUID) -> list[CollectionOut]:
    """List owner_id's collections, alphabetically, each with its recipe count.

    Single aggregate query (LEFT JOIN + COUNT + GROUP BY) — no N+1.
    """
    stmt = (
        select(Collection, func.count(collection_recipes.c.recipe_id))
        .outerjoin(collection_recipes, collection_recipes.c.collection_id == Collection.id)
        .where(Collection.owner_id == owner_id)
        .group_by(Collection.id)
        .order_by(func.lower(Collection.name))
    )
    rows = db.execute(stmt).all()
    return [CollectionOut(id=c.id, name=c.name, recipe_count=count) for c, count in rows]


def set_recipe_collections(
    db: Session,
    *,
    owner_id: uuid.UUID,
    recipe_id: uuid.UUID,
    collection_ids: list[uuid.UUID],
) -> RecipeOut:
    """Replace the full set of collections a recipe belongs to (full-set semantics).

    - The recipe must be owner_id's own (404 otherwise).
    - Every id in collection_ids must be one of owner_id's own collections
      (404 on the first one that isn't — mirrors the other ownership checks
      in this module rather than silently dropping/ignoring foreign ids).
    - Duplicate ids in the input are deduped; the previous membership set is
      entirely replaced (recipe.collections = [...]) rather than diffed.
    Flushes; caller owns commit.
    """
    recipe = db.scalars(
        select(Recipe).where(Recipe.id == recipe_id, Recipe.owner_id == owner_id)
    ).first()
    if recipe is None:
        raise ApiError(404, "not_found", f"Recipe {recipe_id} not found.")

    unique_ids = list(dict.fromkeys(collection_ids))
    collections: list[Collection] = []
    if unique_ids:
        owned = db.scalars(
            select(Collection).where(Collection.owner_id == owner_id, Collection.id.in_(unique_ids))
        ).all()
        owned_by_id = {c.id: c for c in owned}
        missing = [cid for cid in unique_ids if cid not in owned_by_id]
        if missing:
            raise ApiError(404, "not_found", f"Collection {missing[0]} not found.")
        collections = [owned_by_id[cid] for cid in unique_ids]

    recipe.collections = collections
    db.flush()

    loaded = _load_recipe_full(db, recipe_id)
    assert loaded is not None
    return RecipeOut.model_validate(loaded)


# ---------------------------------------------------------------------------
# Fingerprint helpers (used by ingestion dedupe)
# ---------------------------------------------------------------------------


def find_recipe_id_by_fingerprint(
    db: Session,
    owner_id: uuid.UUID,
    fingerprint: str,
) -> uuid.UUID | None:
    """Return the id of the owner's recipe with *fingerprint*, or None.

    Owner-scoped single-query lookup.
    """
    row = db.scalars(
        select(Recipe.id).where(
            Recipe.owner_id == owner_id,
            Recipe.source_fingerprint == fingerprint,
        )
    ).first()
    return row


# ---------------------------------------------------------------------------
# Verification helpers
# ---------------------------------------------------------------------------


def verify_recipe(
    db: Session,
    owner_id: uuid.UUID,
    recipe_id: uuid.UUID,
) -> None:
    """Mark a recipe as verified (is_verified = True).

    Owner-scoped. Raises ApiError 404 if not found or wrong owner.
    Flushes; caller owns commit.
    """
    recipe = db.scalars(
        select(Recipe).where(Recipe.id == recipe_id, Recipe.owner_id == owner_id)
    ).first()
    if recipe is None:
        raise ApiError(404, "not_found", f"Recipe {recipe_id} not found.")
    recipe.is_verified = True
    db.flush()


def set_personal(
    db: Session,
    *,
    owner_id: uuid.UUID,
    recipe_id: uuid.UUID,
    is_favorite: bool | Any = UNSET,
    notes: str | None | Any = UNSET,
) -> RecipeOut:
    """Patch personal metadata (is_favorite/notes) without touching recipe content.

    Owner-scoped: raises ApiError 404 if not found or wrong owner.
    Both fields use the ``UNSET`` sentinel so callers can explicitly pass
    ``notes=None`` (clearing it) vs. leaving it untouched.
    Flushes; caller owns commit.
    """
    recipe = db.scalars(
        select(Recipe).where(Recipe.id == recipe_id, Recipe.owner_id == owner_id)
    ).first()
    if recipe is None:
        raise ApiError(404, "not_found", f"Recipe {recipe_id} not found.")

    if is_favorite is not UNSET:
        recipe.is_favorite = is_favorite

    if notes is not UNSET:
        recipe.notes = notes

    db.flush()

    loaded = _load_recipe_full(db, recipe_id)
    assert loaded is not None
    return RecipeOut.model_validate(loaded)


# ---------------------------------------------------------------------------
# Recipe image upload
# ---------------------------------------------------------------------------


def set_recipe_image(
    db: Session,
    *,
    owner_id: uuid.UUID,
    recipe_id: uuid.UUID,
    data: bytes,
    store: FileStore,
) -> RecipeOut:
    """Upload (or replace) the recipe's photo.

    Owner-scoped: raises ApiError 404 if not found or wrong owner.
    Validates size, then sniffs the actual content from magic bytes — the
    client-supplied Content-Type is only a hint and is never trusted (mirrors
    ingestion.service.submit_file). Images only (png/jpeg/webp/gif); a PDF or
    any other content is rejected even though sniff.py recognizes it.
    Saves to the FileStore and points recipe.image_ref at the new ref
    (the old blob, if any, is left in place — content-addressed store, same
    convention as everywhere else in the codebase).
    Flushes; caller owns commit.
    """
    recipe = db.scalars(
        select(Recipe).where(Recipe.id == recipe_id, Recipe.owner_id == owner_id)
    ).first()
    if recipe is None:
        raise ApiError(404, "not_found", f"Recipe {recipe_id} not found.")

    if len(data) > MAX_IMAGE_BYTES:
        raise ApiError(422, "file_too_large", "Image must be ≤ 10 MB.")

    sniffed_media_type = detect_media_type(data)
    if sniffed_media_type is None or sniffed_media_type not in _ALLOWED_IMAGE_TYPES:
        raise ApiError(
            422,
            "unsupported_file_type",
            f"File content is not a supported image type (detected: "
            f"{sniffed_media_type or 'unknown'}). "
            f"Allowed: {', '.join(sorted(_ALLOWED_IMAGE_TYPES))}",
        )

    suffix = _IMAGE_MEDIA_TYPE_SUFFIX[sniffed_media_type]
    recipe.image_ref = store.save(data, suffix=suffix)
    db.flush()

    loaded = _load_recipe_full(db, recipe_id)
    assert loaded is not None
    return RecipeOut.model_validate(loaded)


def clear_recipe_image(
    db: Session,
    *,
    owner_id: uuid.UUID,
    recipe_id: uuid.UUID,
) -> RecipeOut:
    """Clear the recipe's photo (sets image_ref to None).

    Owner-scoped: raises ApiError 404 if not found or wrong owner.
    Idempotent — clearing an already-imageless recipe is a no-op.
    Flushes; caller owns commit.
    """
    recipe = db.scalars(
        select(Recipe).where(Recipe.id == recipe_id, Recipe.owner_id == owner_id)
    ).first()
    if recipe is None:
        raise ApiError(404, "not_found", f"Recipe {recipe_id} not found.")

    recipe.image_ref = None
    db.flush()

    loaded = _load_recipe_full(db, recipe_id)
    assert loaded is not None
    return RecipeOut.model_validate(loaded)


def are_verified(
    db: Session,
    owner_id: uuid.UUID,
    recipe_ids: list[uuid.UUID],
) -> dict[uuid.UUID, bool]:
    """Return a mapping of recipe_id → is_verified for the given owner-scoped ids.

    Missing (deleted) recipes are omitted from the result.
    """
    if not recipe_ids:
        return {}
    rows = db.execute(
        select(Recipe.id, Recipe.is_verified).where(
            Recipe.owner_id == owner_id,
            Recipe.id.in_(recipe_ids),
        )
    ).all()
    return {row.id: row.is_verified for row in rows}


# ---------------------------------------------------------------------------
# Merge hook: repoint ingredient_lines after catalog merge
# ---------------------------------------------------------------------------


def repoint_ingredient_lines(db: Session, source_id: uuid.UUID, target_id: uuid.UUID) -> None:
    """Bulk UPDATE ingredient_lines.canonical_ingredient_id from source to target.

    Called by catalog_service.merge via the registered hook.
    """
    db.execute(
        update(IngredientLine)
        .where(IngredientLine.canonical_ingredient_id == source_id)
        .values(canonical_ingredient_id=target_id)
    )
    db.flush()


# ---------------------------------------------------------------------------
# Register hooks at import time (after repoint_ingredient_lines is defined)
# ---------------------------------------------------------------------------

register_hooks()
