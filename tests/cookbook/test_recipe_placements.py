"""Tests for the recipe↔cookbook join (`cookbook_recipes`) and set-based access.

Covers the boards model's data foundation:

* the join rows themselves — dual-written by every code path that files a
  recipe into a cookbook (`create_recipe`, `copy_recipe`, `move_recipe`) while
  `recipes.cookbook_id` is still NOT NULL,
* `add_recipe_to_cookbook` / `remove_recipe_from_cookbook` /
  `cookbooks_for_recipe`,
* and the reworked, SET-BASED recipe access: a recipe's access level is the
  HIGHEST role its viewer holds across ALL the cookbooks holding it, plus an
  always-`"owner"` path through `recipes.owner_id`.

The transition set is `distinct(cookbook_recipes) ∪ {recipes.cookbook_id}` —
see `cookbook.service._recipe_cookbook_ids`; that union is what keeps rows
predating the (later) data migration readable.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from recipe_normalizer.catalog.seed_loader import load_seed
from recipe_normalizer.cookbook import service as cookbook_service
from recipe_normalizer.cookbook.models import (
    Cookbook,
    CookbookMember,
    CookbookRecipe,
    CookbookRole,
    CookbookVisibility,
    Recipe,
)
from recipe_normalizer.cookbook.schemas import IngredientGroupIn, IngredientLineIn, RecipeIn
from recipe_normalizer.errors import ApiError
from recipe_normalizer.users.models import User

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def make_user(db: Session) -> User:
    suffix = str(uuid.uuid4())[:8]
    user = User(
        email=f"chef{suffix}@example.com",
        password_hash="hash",
        display_name=f"Chef{suffix}",
    )
    db.add(user)
    db.flush()
    return user


def make_cookbook(
    db: Session,
    owner: User,
    *,
    visibility: CookbookVisibility = CookbookVisibility.private,
    name: str = "Cookbook",
) -> Cookbook:
    cookbook = Cookbook(owner_id=owner.id, name=name, visibility=visibility)
    db.add(cookbook)
    db.flush()
    return cookbook


def add_member(db: Session, cookbook: Cookbook, user: User, role: CookbookRole) -> CookbookMember:
    member = CookbookMember(cookbook_id=cookbook.id, user_id=user.id, role=role)
    db.add(member)
    db.flush()
    return member


def _recipe_in(title: str = "Placement Cake") -> RecipeIn:
    return RecipeIn(
        title=title,
        groups=[
            IngredientGroupIn(
                name="Main",
                lines=[
                    IngredientLineIn(
                        original_text="1 cup flour",
                        quantity=1,
                        unit="cup",
                        name="all-purpose flour",
                    )
                ],
            )
        ],
        cuisines=[],
        dish_types=[],
        tags=[],
        steps=[],
    )


def placements(db: Session, recipe_id: uuid.UUID) -> set[uuid.UUID]:
    """The raw join rows for *recipe_id* (NOT the transition set)."""
    return set(
        db.scalars(
            select(CookbookRecipe.cookbook_id).where(CookbookRecipe.recipe_id == recipe_id)
        ).all()
    )


@pytest.fixture()
def seeded(db_session: Session) -> Session:
    load_seed(db_session)
    return db_session


@pytest.fixture()
def owner(seeded: Session) -> User:
    return make_user(seeded)


# ---------------------------------------------------------------------------
# Dual-write: every path that files a recipe into a cookbook writes a join row
# ---------------------------------------------------------------------------


def test_create_recipe_dual_writes_a_join_row(seeded: Session, owner: User) -> None:
    created = cookbook_service.create_recipe(seeded, owner_id=owner.id, data=_recipe_in())

    row = seeded.get(CookbookRecipe, (created.cookbook_id, created.id))
    assert row is not None
    assert row.added_by == owner.id
    assert row.added_at is not None
    assert cookbook_service.cookbooks_for_recipe(seeded, created.id) == [created.cookbook_id]


def test_create_recipe_into_explicit_cookbook_dual_writes(seeded: Session, owner: User) -> None:
    cookbook = make_cookbook(seeded, owner, name="Explicit")
    created = cookbook_service.create_recipe(
        seeded, owner_id=owner.id, data=_recipe_in(), cookbook_id=cookbook.id
    )

    assert placements(seeded, created.id) == {cookbook.id}


def test_copy_recipe_dual_writes_a_join_row(seeded: Session, owner: User) -> None:
    recipient = make_user(seeded)
    created = cookbook_service.create_recipe(seeded, owner_id=owner.id, data=_recipe_in())

    copy = cookbook_service.copy_recipe(
        seeded, created.id, new_owner_id=recipient.id, provenance={}
    )

    assert placements(seeded, copy.id) == {copy.cookbook_id}
    row = seeded.get(CookbookRecipe, (copy.cookbook_id, copy.id))
    assert row is not None and row.added_by == recipient.id


def test_move_recipe_moves_the_join_row(seeded: Session, owner: User) -> None:
    source = make_cookbook(seeded, owner, name="Source")
    dest = make_cookbook(seeded, owner, name="Dest")
    created = cookbook_service.create_recipe(
        seeded, owner_id=owner.id, data=_recipe_in(), cookbook_id=source.id
    )

    cookbook_service.move_recipe(seeded, created.id, owner.id, dest.id)

    assert placements(seeded, created.id) == {dest.id}
    assert cookbook_service.cookbooks_for_recipe(seeded, created.id) == [dest.id]


# ---------------------------------------------------------------------------
# cookbooks_for_recipe — the transition set
# ---------------------------------------------------------------------------


def test_cookbooks_for_recipe_includes_legacy_cookbook_id_without_a_join_row(
    seeded: Session, owner: User
) -> None:
    """Pre-migration rows (no join row at all) are still readable/placeable."""
    created = cookbook_service.create_recipe(seeded, owner_id=owner.id, data=_recipe_in())
    # Simulate a row that predates the dual-write: delete its join row.
    seeded.delete(seeded.get(CookbookRecipe, (created.cookbook_id, created.id)))
    seeded.flush()

    assert placements(seeded, created.id) == set()
    assert cookbook_service.cookbooks_for_recipe(seeded, created.id) == [created.cookbook_id]


def test_cookbooks_for_recipe_is_empty_for_a_nonexistent_recipe(seeded: Session) -> None:
    assert cookbook_service.cookbooks_for_recipe(seeded, uuid.uuid4()) == []


def test_cookbooks_for_recipe_dedupes_the_legacy_placement(seeded: Session, owner: User) -> None:
    second = make_cookbook(seeded, owner, name="Second")
    created = cookbook_service.create_recipe(seeded, owner_id=owner.id, data=_recipe_in())

    cookbook_service.add_recipe_to_cookbook(
        seeded, user_id=owner.id, recipe_id=created.id, cookbook_id=second.id
    )

    assert set(cookbook_service.cookbooks_for_recipe(seeded, created.id)) == {
        created.cookbook_id,
        second.id,
    }
    assert len(cookbook_service.cookbooks_for_recipe(seeded, created.id)) == 2


# ---------------------------------------------------------------------------
# add_recipe_to_cookbook
# ---------------------------------------------------------------------------


def test_add_recipe_to_cookbook_adds_a_placement(seeded: Session, owner: User) -> None:
    second = make_cookbook(seeded, owner, name="Second")
    created = cookbook_service.create_recipe(seeded, owner_id=owner.id, data=_recipe_in())

    cookbook_service.add_recipe_to_cookbook(
        seeded, user_id=owner.id, recipe_id=created.id, cookbook_id=second.id
    )

    assert placements(seeded, created.id) == {created.cookbook_id, second.id}
    row = seeded.get(CookbookRecipe, (second.id, created.id))
    assert row is not None and row.added_by == owner.id
    # ...and the recipe's primary (legacy) placement is untouched.
    assert seeded.get(Recipe, created.id).cookbook_id == created.cookbook_id  # type: ignore[union-attr]


def test_add_recipe_to_cookbook_is_idempotent(seeded: Session, owner: User) -> None:
    second = make_cookbook(seeded, owner, name="Second")
    created = cookbook_service.create_recipe(seeded, owner_id=owner.id, data=_recipe_in())

    for _ in range(3):
        cookbook_service.add_recipe_to_cookbook(
            seeded, user_id=owner.id, recipe_id=created.id, cookbook_id=second.id
        )

    assert placements(seeded, created.id) == {created.cookbook_id, second.id}


def test_add_recipe_to_its_own_legacy_cookbook_is_idempotent(seeded: Session, owner: User) -> None:
    created = cookbook_service.create_recipe(seeded, owner_id=owner.id, data=_recipe_in())

    cookbook_service.add_recipe_to_cookbook(
        seeded, user_id=owner.id, recipe_id=created.id, cookbook_id=created.cookbook_id
    )

    assert placements(seeded, created.id) == {created.cookbook_id}


def test_add_recipe_by_editor_member_ok(seeded: Session, owner: User) -> None:
    editor = make_user(seeded)
    target = make_cookbook(seeded, owner, name="Shared")
    add_member(seeded, target, editor, CookbookRole.editor)
    created = cookbook_service.create_recipe(seeded, owner_id=editor.id, data=_recipe_in())

    cookbook_service.add_recipe_to_cookbook(
        seeded, user_id=editor.id, recipe_id=created.id, cookbook_id=target.id
    )

    assert target.id in placements(seeded, created.id)


@pytest.mark.parametrize("role", [CookbookRole.viewer, None])
def test_add_recipe_requires_editor_on_the_target_cookbook(
    seeded: Session, owner: User, role: CookbookRole | None
) -> None:
    outsider = make_user(seeded)
    target = make_cookbook(seeded, owner, name="Not Yours")
    if role is not None:
        add_member(seeded, target, outsider, role)
    created = cookbook_service.create_recipe(seeded, owner_id=outsider.id, data=_recipe_in())

    with pytest.raises(ApiError) as exc_info:
        cookbook_service.add_recipe_to_cookbook(
            seeded, user_id=outsider.id, recipe_id=created.id, cookbook_id=target.id
        )
    assert exc_info.value.status_code == 404
    assert placements(seeded, created.id) == {created.cookbook_id}


def test_add_recipe_requires_read_access_to_the_recipe(seeded: Session, owner: User) -> None:
    """Editor of the target cookbook, but the recipe itself is invisible to them."""
    stranger = make_user(seeded)
    own_cookbook = make_cookbook(seeded, stranger, name="Stranger's Own")
    created = cookbook_service.create_recipe(seeded, owner_id=owner.id, data=_recipe_in())

    with pytest.raises(ApiError) as exc_info:
        cookbook_service.add_recipe_to_cookbook(
            seeded, user_id=stranger.id, recipe_id=created.id, cookbook_id=own_cookbook.id
        )
    assert exc_info.value.status_code == 404
    assert placements(seeded, created.id) == {created.cookbook_id}


def test_add_nonexistent_recipe_raises_404(seeded: Session, owner: User) -> None:
    target = make_cookbook(seeded, owner, name="Target")

    with pytest.raises(ApiError) as exc_info:
        cookbook_service.add_recipe_to_cookbook(
            seeded, user_id=owner.id, recipe_id=uuid.uuid4(), cookbook_id=target.id
        )
    assert exc_info.value.status_code == 404


def test_add_a_publicly_readable_recipe_to_my_own_cookbook(seeded: Session, owner: User) -> None:
    """Read access is enough on the recipe side — here via a public cookbook."""
    saver = make_user(seeded)
    public = make_cookbook(seeded, owner, visibility=CookbookVisibility.public, name="Public")
    mine = make_cookbook(seeded, saver, name="Mine")
    created = cookbook_service.create_recipe(
        seeded, owner_id=owner.id, data=_recipe_in(), cookbook_id=public.id
    )

    cookbook_service.add_recipe_to_cookbook(
        seeded, user_id=saver.id, recipe_id=created.id, cookbook_id=mine.id
    )

    assert placements(seeded, created.id) == {public.id, mine.id}


# ---------------------------------------------------------------------------
# remove_recipe_from_cookbook
# ---------------------------------------------------------------------------


def test_remove_recipe_from_cookbook_drops_the_placement(seeded: Session, owner: User) -> None:
    second = make_cookbook(seeded, owner, name="Second")
    created = cookbook_service.create_recipe(seeded, owner_id=owner.id, data=_recipe_in())
    cookbook_service.add_recipe_to_cookbook(
        seeded, user_id=owner.id, recipe_id=created.id, cookbook_id=second.id
    )

    cookbook_service.remove_recipe_from_cookbook(
        seeded, user_id=owner.id, recipe_id=created.id, cookbook_id=second.id
    )

    assert cookbook_service.cookbooks_for_recipe(seeded, created.id) == [created.cookbook_id]
    # The recipe itself survives — removal is never a delete.
    assert seeded.get(Recipe, created.id) is not None


def test_remove_last_placement_raises_409_last_placement(seeded: Session, owner: User) -> None:
    created = cookbook_service.create_recipe(seeded, owner_id=owner.id, data=_recipe_in())

    with pytest.raises(ApiError) as exc_info:
        cookbook_service.remove_recipe_from_cookbook(
            seeded, user_id=owner.id, recipe_id=created.id, cookbook_id=created.cookbook_id
        )
    assert exc_info.value.status_code == 409
    assert exc_info.value.code == "last_placement"
    assert cookbook_service.cookbooks_for_recipe(seeded, created.id) == [created.cookbook_id]


def test_remove_the_primary_placement_repoints_cookbook_id(seeded: Session, owner: User) -> None:
    """`recipes.cookbook_id` is still NOT NULL, so it must follow the set."""
    second = make_cookbook(seeded, owner, name="Second")
    created = cookbook_service.create_recipe(seeded, owner_id=owner.id, data=_recipe_in())
    cookbook_service.add_recipe_to_cookbook(
        seeded, user_id=owner.id, recipe_id=created.id, cookbook_id=second.id
    )

    cookbook_service.remove_recipe_from_cookbook(
        seeded, user_id=owner.id, recipe_id=created.id, cookbook_id=created.cookbook_id
    )

    recipe = seeded.get(Recipe, created.id)
    assert recipe is not None
    assert recipe.cookbook_id == second.id
    assert cookbook_service.cookbooks_for_recipe(seeded, created.id) == [second.id]


def test_remove_the_legacy_only_placement_repoints_cookbook_id(
    seeded: Session, owner: User
) -> None:
    """A row predating the migration has no join row for its primary cookbook."""
    second = make_cookbook(seeded, owner, name="Second")
    created = cookbook_service.create_recipe(seeded, owner_id=owner.id, data=_recipe_in())
    legacy_cookbook_id = created.cookbook_id
    seeded.delete(seeded.get(CookbookRecipe, (legacy_cookbook_id, created.id)))
    seeded.flush()
    cookbook_service.add_recipe_to_cookbook(
        seeded, user_id=owner.id, recipe_id=created.id, cookbook_id=second.id
    )

    cookbook_service.remove_recipe_from_cookbook(
        seeded, user_id=owner.id, recipe_id=created.id, cookbook_id=legacy_cookbook_id
    )

    recipe = seeded.get(Recipe, created.id)
    assert recipe is not None
    assert recipe.cookbook_id == second.id
    assert cookbook_service.cookbooks_for_recipe(seeded, created.id) == [second.id]


@pytest.mark.parametrize("role", [CookbookRole.viewer, None])
def test_remove_requires_editor_on_the_cookbook(
    seeded: Session, owner: User, role: CookbookRole | None
) -> None:
    outsider = make_user(seeded)
    shared = make_cookbook(seeded, owner, name="Shared")
    if role is not None:
        add_member(seeded, shared, outsider, role)
    created = cookbook_service.create_recipe(
        seeded, owner_id=owner.id, data=_recipe_in(), cookbook_id=shared.id
    )
    second = make_cookbook(seeded, owner, name="Second")
    cookbook_service.add_recipe_to_cookbook(
        seeded, user_id=owner.id, recipe_id=created.id, cookbook_id=second.id
    )

    with pytest.raises(ApiError) as exc_info:
        cookbook_service.remove_recipe_from_cookbook(
            seeded, user_id=outsider.id, recipe_id=created.id, cookbook_id=shared.id
        )
    assert exc_info.value.status_code == 404
    assert shared.id in placements(seeded, created.id)


def test_remove_from_a_cookbook_the_recipe_is_not_in_is_a_noop(
    seeded: Session, owner: User
) -> None:
    unrelated = make_cookbook(seeded, owner, name="Unrelated")
    created = cookbook_service.create_recipe(seeded, owner_id=owner.id, data=_recipe_in())

    cookbook_service.remove_recipe_from_cookbook(
        seeded, user_id=owner.id, recipe_id=created.id, cookbook_id=unrelated.id
    )

    assert cookbook_service.cookbooks_for_recipe(seeded, created.id) == [created.cookbook_id]


def test_remove_nonexistent_recipe_raises_404(seeded: Session, owner: User) -> None:
    cookbook = make_cookbook(seeded, owner, name="Target")

    with pytest.raises(ApiError) as exc_info:
        cookbook_service.remove_recipe_from_cookbook(
            seeded, user_id=owner.id, recipe_id=uuid.uuid4(), cookbook_id=cookbook.id
        )
    assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# Set-based recipe access — the highest role across ALL placements wins
# ---------------------------------------------------------------------------


def test_access_is_the_highest_role_across_placements(seeded: Session, owner: User) -> None:
    """Viewer in cookbook A + editor in cookbook B ⇒ editor on the recipe."""
    user = make_user(seeded)
    book_a = make_cookbook(seeded, owner, name="A")
    book_b = make_cookbook(seeded, owner, name="B")
    add_member(seeded, book_a, user, CookbookRole.viewer)
    add_member(seeded, book_b, user, CookbookRole.editor)
    created = cookbook_service.create_recipe(
        seeded, owner_id=owner.id, data=_recipe_in(), cookbook_id=book_a.id
    )

    assert (
        cookbook_service.recipe_access(seeded, user_id=user.id, recipe_id=created.id)
        == CookbookRole.viewer
    )

    cookbook_service.add_recipe_to_cookbook(
        seeded, user_id=owner.id, recipe_id=created.id, cookbook_id=book_b.id
    )

    assert (
        cookbook_service.recipe_access(seeded, user_id=user.id, recipe_id=created.id)
        == CookbookRole.editor
    )
    # ...and edit rights follow the set-based level.
    updated = cookbook_service.update_recipe(
        seeded,
        owner_id=user.id,
        recipe_id=created.id,
        data=_recipe_in(title="Edited via cookbook B"),
        editor_id=user.id,
    )
    assert updated.title == "Edited via cookbook B"


def test_access_via_a_second_cookbook_the_user_owns(seeded: Session, owner: User) -> None:
    """Owning ANY cookbook holding the recipe resolves to "owner"-level access."""
    saver = make_user(seeded)
    public = make_cookbook(seeded, owner, visibility=CookbookVisibility.public, name="Public")
    mine = make_cookbook(seeded, saver, name="Mine")
    created = cookbook_service.create_recipe(
        seeded, owner_id=owner.id, data=_recipe_in(), cookbook_id=public.id
    )

    assert (
        cookbook_service.recipe_access(seeded, user_id=saver.id, recipe_id=created.id)
        == CookbookRole.viewer
    )

    cookbook_service.add_recipe_to_cookbook(
        seeded, user_id=saver.id, recipe_id=created.id, cookbook_id=mine.id
    )

    assert cookbook_service.recipe_access(seeded, user_id=saver.id, recipe_id=created.id) == "owner"
    # ...but the RECIPE row's owner is unchanged — owner-only ops stay theirs.
    assert cookbook_service.is_recipe_owner(seeded, saver.id, created.id) is False
    assert cookbook_service.is_recipe_owner(seeded, owner.id, created.id) is True


def test_recipe_owner_always_has_owner_access_even_with_no_readable_cookbook(
    seeded: Session, owner: User
) -> None:
    """The boards model's fix for the Phase-1 removed-contributor hole."""
    contributor = make_user(seeded)
    shared = make_cookbook(seeded, owner, name="Shared")
    add_member(seeded, shared, contributor, CookbookRole.editor)
    created = cookbook_service.create_recipe(
        seeded, owner_id=contributor.id, data=_recipe_in(), cookbook_id=shared.id
    )

    cookbook_service.remove_cookbook_member(
        seeded, cookbook_id=shared.id, owner_id=owner.id, member_user_id=contributor.id
    )

    assert (
        cookbook_service.recipe_access(seeded, user_id=contributor.id, recipe_id=created.id)
        == "owner"
    )
    assert (
        cookbook_service.get_recipe(seeded, owner_id=contributor.id, recipe_id=created.id).id
        == created.id
    )


def test_no_placement_the_user_can_read_and_not_the_owner_is_none(
    seeded: Session, owner: User
) -> None:
    stranger = make_user(seeded)
    private = make_cookbook(seeded, owner, name="Private")
    created = cookbook_service.create_recipe(
        seeded, owner_id=owner.id, data=_recipe_in(), cookbook_id=private.id
    )

    assert cookbook_service.recipe_access(seeded, user_id=stranger.id, recipe_id=created.id) is None
    assert cookbook_service.recipe_access(seeded, user_id=None, recipe_id=created.id) is None
    with pytest.raises(ApiError) as exc_info:
        cookbook_service.get_recipe(seeded, owner_id=stranger.id, recipe_id=created.id)
    assert exc_info.value.status_code == 404


def test_a_single_public_placement_grants_anonymous_viewer(seeded: Session, owner: User) -> None:
    """One readable cookbook in the set is enough — the others stay private."""
    private = make_cookbook(seeded, owner, name="Private")
    public = make_cookbook(seeded, owner, visibility=CookbookVisibility.public, name="Public")
    created = cookbook_service.create_recipe(
        seeded, owner_id=owner.id, data=_recipe_in(), cookbook_id=private.id
    )

    assert cookbook_service.recipe_access(seeded, user_id=None, recipe_id=created.id) is None

    cookbook_service.add_recipe_to_cookbook(
        seeded, user_id=owner.id, recipe_id=created.id, cookbook_id=public.id
    )

    assert (
        cookbook_service.recipe_access(seeded, user_id=None, recipe_id=created.id)
        == CookbookRole.viewer
    )


def test_removing_the_readable_placement_revokes_access(seeded: Session, owner: User) -> None:
    """Access is derived, not granted — dropping the join row drops the access."""
    reader = make_user(seeded)
    private = make_cookbook(seeded, owner, name="Private")
    shared = make_cookbook(seeded, owner, name="Shared")
    add_member(seeded, shared, reader, CookbookRole.viewer)
    created = cookbook_service.create_recipe(
        seeded, owner_id=owner.id, data=_recipe_in(), cookbook_id=private.id
    )
    cookbook_service.add_recipe_to_cookbook(
        seeded, user_id=owner.id, recipe_id=created.id, cookbook_id=shared.id
    )
    assert (
        cookbook_service.recipe_access(seeded, user_id=reader.id, recipe_id=created.id)
        == CookbookRole.viewer
    )

    cookbook_service.remove_recipe_from_cookbook(
        seeded, user_id=owner.id, recipe_id=created.id, cookbook_id=shared.id
    )

    assert cookbook_service.recipe_access(seeded, user_id=reader.id, recipe_id=created.id) is None


def test_nonexistent_recipe_access_is_none(seeded: Session, owner: User) -> None:
    assert cookbook_service.recipe_access(seeded, user_id=owner.id, recipe_id=uuid.uuid4()) is None


# ---------------------------------------------------------------------------
# delete_recipe is owner-only now; "remove from cookbook" is the editor action
# ---------------------------------------------------------------------------


def test_editor_member_cannot_delete_the_recipe_but_can_remove_the_placement(
    seeded: Session, owner: User
) -> None:
    editor = make_user(seeded)
    shared = make_cookbook(seeded, owner, name="Shared")
    add_member(seeded, shared, editor, CookbookRole.editor)
    created = cookbook_service.create_recipe(
        seeded, owner_id=owner.id, data=_recipe_in(), cookbook_id=shared.id
    )
    second = make_cookbook(seeded, owner, name="Owner's Second")
    cookbook_service.add_recipe_to_cookbook(
        seeded, user_id=owner.id, recipe_id=created.id, cookbook_id=second.id
    )

    with pytest.raises(ApiError) as exc_info:
        cookbook_service.delete_recipe(seeded, owner_id=editor.id, recipe_id=created.id)
    assert exc_info.value.status_code == 404
    assert seeded.get(Recipe, created.id) is not None

    cookbook_service.remove_recipe_from_cookbook(
        seeded, user_id=editor.id, recipe_id=created.id, cookbook_id=shared.id
    )
    assert cookbook_service.cookbooks_for_recipe(seeded, created.id) == [second.id]


def test_cookbook_owner_cannot_delete_someone_elses_recipe(seeded: Session, owner: User) -> None:
    contributor = make_user(seeded)
    shared = make_cookbook(seeded, owner, name="Shared")
    add_member(seeded, shared, contributor, CookbookRole.editor)
    created = cookbook_service.create_recipe(
        seeded, owner_id=contributor.id, data=_recipe_in(), cookbook_id=shared.id
    )

    with pytest.raises(ApiError) as exc_info:
        cookbook_service.delete_recipe(seeded, owner_id=owner.id, recipe_id=created.id)
    assert exc_info.value.status_code == 404
    assert seeded.get(Recipe, created.id) is not None


def test_recipe_owner_can_delete_and_join_rows_cascade(seeded: Session, owner: User) -> None:
    second = make_cookbook(seeded, owner, name="Second")
    created = cookbook_service.create_recipe(seeded, owner_id=owner.id, data=_recipe_in())
    cookbook_service.add_recipe_to_cookbook(
        seeded, user_id=owner.id, recipe_id=created.id, cookbook_id=second.id
    )

    cookbook_service.delete_recipe(seeded, owner_id=owner.id, recipe_id=created.id)

    assert seeded.get(Recipe, created.id) is None
    assert placements(seeded, created.id) == set()


def test_deleting_a_cookbook_cascades_its_join_rows(seeded: Session, owner: User) -> None:
    """A cookbook delete takes its placements with it (and its own recipes)."""
    second = make_cookbook(seeded, owner, name="Second")
    created = cookbook_service.create_recipe(seeded, owner_id=owner.id, data=_recipe_in())
    cookbook_service.add_recipe_to_cookbook(
        seeded, user_id=owner.id, recipe_id=created.id, cookbook_id=second.id
    )

    cookbook_service.delete_cookbook(seeded, second.id, owner.id)

    assert placements(seeded, created.id) == {created.cookbook_id}
    assert seeded.get(Recipe, created.id) is not None


def test_deleting_a_cookbook_rehomes_recipes_that_live_elsewhere_too(
    seeded: Session, owner: User
) -> None:
    """Multi-placed recipes survive their PRIMARY cookbook being deleted.

    ``recipes.cookbook_id`` cascades on cookbook delete, so without re-homing,
    deleting cookbook A would destroy a recipe that also sits in cookbook B —
    silent data loss the moment a recipe can be in two places.
    """
    book_a = make_cookbook(seeded, owner, name="A")
    book_b = make_cookbook(seeded, owner, name="B")
    shared_recipe = cookbook_service.create_recipe(
        seeded, owner_id=owner.id, data=_recipe_in(title="Lives in both"), cookbook_id=book_a.id
    )
    only_in_a = cookbook_service.create_recipe(
        seeded, owner_id=owner.id, data=_recipe_in(title="Only in A"), cookbook_id=book_a.id
    )
    cookbook_service.add_recipe_to_cookbook(
        seeded, user_id=owner.id, recipe_id=shared_recipe.id, cookbook_id=book_b.id
    )

    cookbook_service.delete_cookbook(seeded, book_a.id, owner.id)

    survivor = seeded.get(Recipe, shared_recipe.id)
    assert survivor is not None
    assert survivor.cookbook_id == book_b.id
    assert placements(seeded, shared_recipe.id) == {book_b.id}
    # ...while a recipe that lived ONLY in the deleted cookbook is still gone.
    assert seeded.get(Recipe, only_in_a.id) is None
