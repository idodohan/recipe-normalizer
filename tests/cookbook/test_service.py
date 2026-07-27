"""Service-level tests for cookbook CRUD, catalog matching, and dedupe."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from recipe_normalizer.catalog import service as catalog_service
from recipe_normalizer.catalog.seed_loader import load_seed
from recipe_normalizer.cookbook import service as cookbook_service
from recipe_normalizer.cookbook.models import (
    Cookbook,
    CookbookMember,
    CookbookRole,
    CookbookVisibility,
    IngredientLine,
    Recipe,
    SourceType,
    Step,
)
from recipe_normalizer.cookbook.schemas import (
    IngredientGroupIn,
    IngredientLineIn,
    RecipeIn,
    StepIn,
)
from recipe_normalizer.cookbook.service import DuplicateCollectionNameError, DuplicateRecipeError
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


def _simple_recipe_in(
    *,
    title: str = "Test Cake",
    lines: list[IngredientLineIn] | None = None,
    cuisines: list[str] | None = None,
    dish_types: list[str] | None = None,
    tags: list[str] | None = None,
    steps: list[StepIn] | None = None,
) -> RecipeIn:
    if lines is None:
        lines = [
            IngredientLineIn(
                original_text="1 cup flour", quantity=1, unit="cup", name="all-purpose flour"
            )
        ]
    return RecipeIn(
        title=title,
        groups=[IngredientGroupIn(name="Main", lines=lines)],
        cuisines=cuisines or [],
        dish_types=dish_types or [],
        tags=tags or [],
        steps=steps or [],
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def seeded(db_session: Session) -> Session:
    """Load catalog seed data and return the session."""
    load_seed(db_session)
    return db_session


@pytest.fixture()
def owner(seeded: Session) -> User:
    return make_user(seeded, suffix=str(uuid.uuid4())[:8])


# ---------------------------------------------------------------------------
# create_recipe — happy path
# ---------------------------------------------------------------------------


def test_create_recipe_happy_path(seeded: Session, owner: User) -> None:
    """1 cup flour: catalog-matched, normalized, correct display string."""
    data = _simple_recipe_in(
        lines=[
            IngredientLineIn(
                original_text="1 cup flour",
                quantity=1,
                unit="cup",
                name="all-purpose flour",
            )
        ],
        cuisines=["Italian"],
        dish_types=["main"],
        tags=["quick"],
        steps=[StepIn(original_text="Mix everything.")],
    )
    out = cookbook_service.create_recipe(seeded, owner_id=owner.id, data=data)

    # Basic fields
    assert out.owner_id == owner.id
    assert out.title == "Test Cake"

    # Vocab get-or-created
    assert out.cuisines == ["Italian"]
    assert out.dish_types == ["main"]
    assert out.tags == ["quick"]

    # Groups / lines
    assert len(out.groups) == 1
    group = out.groups[0]
    assert group.name == "Main"
    assert len(group.lines) == 1

    line = group.lines[0]
    assert line.canonical_ingredient_id is not None
    # 1 cup all-purpose flour: 120 g (cup gram_weight), preferred=mass → g, approx=True
    assert line.normalized_amount == pytest.approx(120.0, abs=0.1)
    assert line.normalized_unit == "g"
    assert line.is_approx is True
    # Exact dual-quantity display string (spec §5 format)
    assert line.display == "1 cup flour → ~120 g"

    # Steps
    assert len(out.steps) == 1
    assert out.steps[0].original_text == "Mix everything."


def test_create_recipe_unmatched_ingredient_creates_unreviewed(
    seeded: Session, owner: User
) -> None:
    """Unmatched name → create_unreviewed + link; conversion skipped (no density/gram_weights)."""
    data = _simple_recipe_in(
        lines=[
            IngredientLineIn(
                original_text="1 cup gremlin dust",
                quantity=1,
                unit="cup",
                name="gremlin dust",
            )
        ]
    )
    out = cookbook_service.create_recipe(seeded, owner_id=owner.id, data=data)
    line = out.groups[0].lines[0]

    # Canonical link created (unreviewed)
    assert line.canonical_ingredient_id is not None
    # No density/gram_weights for gremlin dust → no conversion → normalized_amount None
    assert line.normalized_amount is None
    assert line.normalized_unit is None
    # display falls back to original_text
    assert line.display == "1 cup gremlin dust"


def test_create_recipe_free_text_line_no_name(seeded: Session, owner: User) -> None:
    """Free-text line with no name field → no catalog match attempt, no normalization."""
    data = _simple_recipe_in(
        lines=[
            IngredientLineIn(
                original_text="salt to taste",
                # no quantity, no unit, no name
            )
        ]
    )
    out = cookbook_service.create_recipe(seeded, owner_id=owner.id, data=data)
    line = out.groups[0].lines[0]

    assert line.canonical_ingredient_id is None
    assert line.normalized_amount is None
    assert line.normalized_unit is None
    assert line.display == "salt to taste"


def test_create_recipe_name_but_no_quantity(seeded: Session, owner: User) -> None:
    """Name provided (match succeeds) but no quantity → canonical linked, no normalization."""
    data = _simple_recipe_in(
        lines=[
            IngredientLineIn(
                original_text="flour, to taste",
                name="all-purpose flour",
            )
        ]
    )
    out = cookbook_service.create_recipe(seeded, owner_id=owner.id, data=data)
    line = out.groups[0].lines[0]

    assert line.canonical_ingredient_id is not None
    assert line.normalized_amount is None
    assert line.display == "flour, to taste"


def test_create_recipe_servings_persisted(seeded: Session, owner: User) -> None:
    from recipe_normalizer.cookbook.schemas import ServingsIn

    data = RecipeIn(
        title="Soup",
        groups=[
            IngredientGroupIn(
                name="Base",
                lines=[IngredientLineIn(original_text="water")],
            )
        ],
        servings=ServingsIn(amount=4.0, unit_text="servings"),
    )
    out = cookbook_service.create_recipe(seeded, owner_id=owner.id, data=data)
    assert out.servings is not None
    assert out.servings.amount == 4.0
    assert out.servings.unit_text == "servings"


def test_create_recipe_vocab_get_or_create_idempotent(seeded: Session, owner: User) -> None:
    """Creating two recipes with the same cuisine name doesn't duplicate the vocab row."""
    from sqlalchemy import func, select

    from recipe_normalizer.cookbook.models import Cuisine

    for i in range(2):
        data = _simple_recipe_in(
            title=f"Recipe {i}",
            cuisines=["TestCuisine"],
            lines=[IngredientLineIn(original_text="water")],
        )
        cookbook_service.create_recipe(seeded, owner_id=owner.id, data=data)

    count = seeded.execute(
        select(func.count()).select_from(Cuisine).where(Cuisine.name == "TestCuisine")
    ).scalar_one()
    assert count == 1


def test_create_recipe_vocab_case_insensitive(seeded: Session, owner: User) -> None:
    """ "Italian" and "italian" resolve to a single cuisine row (first-seen casing kept)."""
    from sqlalchemy import func, select

    from recipe_normalizer.cookbook.models import Cuisine

    for i, name in enumerate(["Italian", "italian"]):
        data = _simple_recipe_in(
            title=f"Recipe {i}",
            cuisines=[name],
            lines=[IngredientLineIn(original_text="water")],
        )
        out = cookbook_service.create_recipe(seeded, owner_id=owner.id, data=data)
        # First-seen casing is preserved on both recipes
        assert out.cuisines == ["Italian"]

    count = seeded.execute(
        select(func.count()).select_from(Cuisine).where(func.lower(Cuisine.name) == "italian")
    ).scalar_one()
    assert count == 1


def test_create_recipe_duplicate_vocab_in_request_deduped(seeded: Session, owner: User) -> None:
    """Duplicate vocab names within one request are deduped (no association PK violation)."""
    data = _simple_recipe_in(
        cuisines=["Italian", "Italian", "italian"],
        tags=["quick", "Quick"],
        lines=[IngredientLineIn(original_text="water")],
    )
    out = cookbook_service.create_recipe(seeded, owner_id=owner.id, data=data)
    assert out.cuisines == ["Italian"]
    assert out.tags == ["quick"]


def test_create_recipe_extraction_meta_and_image_ref_persisted(
    seeded: Session, owner: User
) -> None:
    """extraction_meta and image_ref thread through to the persisted recipe."""
    data = _simple_recipe_in(lines=[IngredientLineIn(original_text="water")])
    out = cookbook_service.create_recipe(
        seeded,
        owner_id=owner.id,
        data=data,
        extraction_meta={"tier_used": 2, "actions_log": ["scrolled"]},
        image_ref="ab/cd.png",
    )
    assert out.extraction_meta == {"tier_used": 2, "actions_log": ["scrolled"]}
    # API exposes the stored ref as a servable /api/files URL...
    assert out.image_ref == "/api/files/ab/cd.png"

    row = seeded.get(Recipe, out.id)
    assert row is not None
    assert row.extraction_meta == {"tier_used": 2, "actions_log": ["scrolled"]}
    # ...while the DB keeps the bare store ref.
    assert row.image_ref == "ab/cd.png"


def test_create_recipe_extraction_meta_and_image_ref_default_none(
    seeded: Session, owner: User
) -> None:
    data = _simple_recipe_in(lines=[IngredientLineIn(original_text="water")])
    out = cookbook_service.create_recipe(seeded, owner_id=owner.id, data=data)
    assert out.extraction_meta is None
    assert out.image_ref is None


# ---------------------------------------------------------------------------
# get_recipe
# ---------------------------------------------------------------------------


def test_get_recipe_returns_recipe_out(seeded: Session, owner: User) -> None:
    data = _simple_recipe_in()
    created = cookbook_service.create_recipe(seeded, owner_id=owner.id, data=data)
    fetched = cookbook_service.get_recipe(seeded, owner_id=owner.id, recipe_id=created.id)
    assert fetched.id == created.id
    assert fetched.title == "Test Cake"


def test_get_recipe_wrong_owner_raises_404(seeded: Session, owner: User) -> None:
    """Other user's recipe → 404 (don't leak existence)."""
    other = make_user(seeded, suffix=str(uuid.uuid4())[:8])
    data = _simple_recipe_in()
    created = cookbook_service.create_recipe(seeded, owner_id=owner.id, data=data)

    with pytest.raises(ApiError) as exc_info:
        cookbook_service.get_recipe(seeded, owner_id=other.id, recipe_id=created.id)
    assert exc_info.value.status_code == 404


def test_get_recipe_missing_raises_404(seeded: Session, owner: User) -> None:
    with pytest.raises(ApiError) as exc_info:
        cookbook_service.get_recipe(seeded, owner_id=owner.id, recipe_id=uuid.uuid4())
    assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# list_recipes
# ---------------------------------------------------------------------------


def test_list_recipes_returns_own_newest_first(seeded: Session, owner: User) -> None:
    for i in range(3):
        cookbook_service.create_recipe(
            seeded,
            owner_id=owner.id,
            data=_simple_recipe_in(
                title=f"Recipe {i}",
                lines=[IngredientLineIn(original_text="water")],
            ),
        )
    page = cookbook_service.list_recipes(seeded, owner_id=owner.id)
    assert len(page.items) == 3
    assert page.total == 3
    # newest first — created_at descending
    titles = [s.title for s in page.items]
    assert titles == ["Recipe 2", "Recipe 1", "Recipe 0"]


def test_list_recipes_excludes_other_owners(seeded: Session, owner: User) -> None:
    other = make_user(seeded, suffix=str(uuid.uuid4())[:8])
    cookbook_service.create_recipe(
        seeded,
        owner_id=owner.id,
        data=_simple_recipe_in(lines=[IngredientLineIn(original_text="water")]),
    )
    cookbook_service.create_recipe(
        seeded,
        owner_id=other.id,
        data=_simple_recipe_in(lines=[IngredientLineIn(original_text="water")]),
    )
    page = cookbook_service.list_recipes(seeded, owner_id=owner.id)
    assert len(page.items) == 1
    assert page.total == 1


# ---------------------------------------------------------------------------
# update_recipe
# ---------------------------------------------------------------------------


def test_update_recipe_replaces_groups_and_reruns_match(seeded: Session, owner: User) -> None:
    """update_recipe wholesale-replaces groups/lines, re-runs matching."""
    data = _simple_recipe_in(
        lines=[
            IngredientLineIn(
                original_text="1 cup flour", quantity=1, unit="cup", name="all-purpose flour"
            )
        ]
    )
    created = cookbook_service.create_recipe(seeded, owner_id=owner.id, data=data)

    # Now update with different ingredient
    editor = make_user(seeded, suffix=str(uuid.uuid4())[:8])
    new_data = _simple_recipe_in(
        title="Updated Cake",
        lines=[IngredientLineIn(original_text="water")],
    )
    updated = cookbook_service.update_recipe(
        seeded,
        owner_id=owner.id,
        recipe_id=created.id,
        data=new_data,
        editor_id=editor.id,
    )

    assert updated.title == "Updated Cake"
    assert updated.last_edited_by == editor.id
    assert updated.last_edited_at is not None
    # Only one group with one line (the old group is gone)
    assert len(updated.groups) == 1
    assert len(updated.groups[0].lines) == 1
    assert updated.groups[0].lines[0].original_text == "water"
    # No name on the new line → no match → no canonical
    assert updated.groups[0].lines[0].canonical_ingredient_id is None


def test_recipe_out_exposes_canonical_name_for_round_trip(seeded: Session, owner: User) -> None:
    """A linked line exposes the canonical name so the review editor round-trips it."""
    created = cookbook_service.create_recipe(
        seeded,
        owner_id=owner.id,
        data=_simple_recipe_in(
            lines=[
                IngredientLineIn(
                    original_text="1 cup flour", quantity=1, unit="cup", name="all-purpose flour"
                )
            ]
        ),
    )
    line = created.groups[0].lines[0]
    assert line.canonical_ingredient_id is not None
    assert line.name == "all-purpose flour"  # the canonical name, not None

    # Re-saving with that exposed name preserves the same catalog link.
    fetched = cookbook_service.get_recipe(seeded, owner_id=owner.id, recipe_id=created.id)
    resaved = cookbook_service.update_recipe(
        seeded,
        owner_id=owner.id,
        recipe_id=created.id,
        data=_simple_recipe_in(
            lines=[
                IngredientLineIn(
                    original_text="1 cup flour",
                    quantity=1,
                    unit="cup",
                    name=fetched.groups[0].lines[0].name,
                )
            ]
        ),
        editor_id=owner.id,
    )
    assert resaved.groups[0].lines[0].canonical_ingredient_id == line.canonical_ingredient_id


def test_recipe_out_line_name_none_when_unlinked(seeded: Session, owner: User) -> None:
    created = cookbook_service.create_recipe(
        seeded,
        owner_id=owner.id,
        data=_simple_recipe_in(lines=[IngredientLineIn(original_text="a pinch of magic")]),
    )
    assert created.groups[0].lines[0].name is None


def test_update_recipe_wrong_owner_raises_404(seeded: Session, owner: User) -> None:
    other = make_user(seeded, suffix=str(uuid.uuid4())[:8])
    created = cookbook_service.create_recipe(
        seeded,
        owner_id=owner.id,
        data=_simple_recipe_in(lines=[IngredientLineIn(original_text="water")]),
    )
    with pytest.raises(ApiError) as exc_info:
        cookbook_service.update_recipe(
            seeded,
            owner_id=other.id,
            recipe_id=created.id,
            data=_simple_recipe_in(lines=[IngredientLineIn(original_text="water")]),
            editor_id=other.id,
        )
    assert exc_info.value.status_code == 404


def test_update_recipe_full_replace_preserves_favorite_and_notes(
    seeded: Session, owner: User
) -> None:
    """PATCH /api/recipes/{id} is a full destructive replace of content, but
    is_favorite/notes are personal metadata outside that replace and must
    survive it untouched."""
    created = cookbook_service.create_recipe(
        seeded,
        owner_id=owner.id,
        data=_simple_recipe_in(lines=[IngredientLineIn(original_text="water")]),
    )
    cookbook_service.set_personal(
        seeded, owner_id=owner.id, recipe_id=created.id, is_favorite=True, notes="great recipe"
    )

    updated = cookbook_service.update_recipe(
        seeded,
        owner_id=owner.id,
        recipe_id=created.id,
        data=_simple_recipe_in(title="Renamed", lines=[IngredientLineIn(original_text="milk")]),
        editor_id=owner.id,
    )

    assert updated.title == "Renamed"
    assert updated.is_favorite is True
    assert updated.notes == "great recipe"


# ---------------------------------------------------------------------------
# set_personal
# ---------------------------------------------------------------------------


def test_set_personal_favorite_toggle_persists(seeded: Session, owner: User) -> None:
    created = cookbook_service.create_recipe(
        seeded,
        owner_id=owner.id,
        data=_simple_recipe_in(lines=[IngredientLineIn(original_text="water")]),
    )
    assert created.is_favorite is False

    favorited = cookbook_service.set_personal(
        seeded, owner_id=owner.id, recipe_id=created.id, is_favorite=True
    )
    assert favorited.is_favorite is True

    unfavorited = cookbook_service.set_personal(
        seeded, owner_id=owner.id, recipe_id=created.id, is_favorite=False
    )
    assert unfavorited.is_favorite is False


def test_set_personal_notes_set_and_explicit_null_clears(seeded: Session, owner: User) -> None:
    created = cookbook_service.create_recipe(
        seeded,
        owner_id=owner.id,
        data=_simple_recipe_in(lines=[IngredientLineIn(original_text="water")]),
    )
    assert created.notes is None

    set_result = cookbook_service.set_personal(
        seeded, owner_id=owner.id, recipe_id=created.id, notes="delicious"
    )
    assert set_result.notes == "delicious"

    cleared = cookbook_service.set_personal(
        seeded, owner_id=owner.id, recipe_id=created.id, notes=None
    )
    assert cleared.notes is None


def test_set_personal_absent_field_leaves_value_unchanged(seeded: Session, owner: User) -> None:
    created = cookbook_service.create_recipe(
        seeded,
        owner_id=owner.id,
        data=_simple_recipe_in(lines=[IngredientLineIn(original_text="water")]),
    )
    cookbook_service.set_personal(
        seeded, owner_id=owner.id, recipe_id=created.id, is_favorite=True, notes="keep me"
    )

    # Calling with neither field set (both UNSET by default) leaves both untouched.
    unchanged = cookbook_service.set_personal(seeded, owner_id=owner.id, recipe_id=created.id)
    assert unchanged.is_favorite is True
    assert unchanged.notes == "keep me"


def test_set_personal_wrong_owner_raises_404(seeded: Session, owner: User) -> None:
    other = make_user(seeded, suffix=str(uuid.uuid4())[:8])
    created = cookbook_service.create_recipe(
        seeded,
        owner_id=owner.id,
        data=_simple_recipe_in(lines=[IngredientLineIn(original_text="water")]),
    )
    with pytest.raises(ApiError) as exc_info:
        cookbook_service.set_personal(
            seeded, owner_id=other.id, recipe_id=created.id, is_favorite=True
        )
    assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# delete_recipe
# ---------------------------------------------------------------------------


def test_delete_recipe_removes_recipe_and_children(seeded: Session, owner: User) -> None:
    data = _simple_recipe_in(
        lines=[
            IngredientLineIn(
                original_text="1 cup flour", quantity=1, unit="cup", name="all-purpose flour"
            )
        ]
    )
    created = cookbook_service.create_recipe(seeded, owner_id=owner.id, data=data)
    recipe_id = created.id

    cookbook_service.delete_recipe(seeded, owner_id=owner.id, recipe_id=recipe_id)

    assert seeded.get(Recipe, recipe_id) is None


def test_delete_recipe_wrong_owner_raises_404(seeded: Session, owner: User) -> None:
    other = make_user(seeded, suffix=str(uuid.uuid4())[:8])
    created = cookbook_service.create_recipe(
        seeded,
        owner_id=owner.id,
        data=_simple_recipe_in(lines=[IngredientLineIn(original_text="water")]),
    )
    with pytest.raises(ApiError) as exc_info:
        cookbook_service.delete_recipe(seeded, owner_id=other.id, recipe_id=created.id)
    assert exc_info.value.status_code == 404


def test_delete_recipe_vocab_untouched(seeded: Session, owner: User) -> None:
    from recipe_normalizer.cookbook.models import Cuisine

    data = _simple_recipe_in(
        cuisines=["DeleteTestCuisine"],
        lines=[IngredientLineIn(original_text="water")],
    )
    created = cookbook_service.create_recipe(seeded, owner_id=owner.id, data=data)
    cuisine_name = "DeleteTestCuisine"

    cookbook_service.delete_recipe(seeded, owner_id=owner.id, recipe_id=created.id)

    from sqlalchemy import select

    cuisine = seeded.execute(
        select(Cuisine).where(Cuisine.name == cuisine_name)
    ).scalar_one_or_none()
    assert cuisine is not None


# ---------------------------------------------------------------------------
# Duplicate fingerprint
# ---------------------------------------------------------------------------


def test_duplicate_fingerprint_same_owner_raises_409(seeded: Session, owner: User) -> None:
    data = _simple_recipe_in(lines=[IngredientLineIn(original_text="water")])
    cookbook_service.create_recipe(
        seeded, owner_id=owner.id, data=data, source_fingerprint="fp-abc"
    )
    with pytest.raises(DuplicateRecipeError) as exc_info:
        cookbook_service.create_recipe(
            seeded, owner_id=owner.id, data=data, source_fingerprint="fp-abc"
        )
    err = exc_info.value
    assert err.status_code == 409
    assert err.existing_id is not None


def test_duplicate_fingerprint_different_owners_ok(seeded: Session, owner: User) -> None:
    """Same fingerprint for different owners is allowed."""
    other = make_user(seeded, suffix=str(uuid.uuid4())[:8])
    data = _simple_recipe_in(lines=[IngredientLineIn(original_text="water")])
    cookbook_service.create_recipe(
        seeded, owner_id=owner.id, data=data, source_fingerprint="fp-shared"
    )
    # Should not raise
    cookbook_service.create_recipe(
        seeded, owner_id=other.id, data=data, source_fingerprint="fp-shared"
    )


# ---------------------------------------------------------------------------
# Merge re-pointing hook
# ---------------------------------------------------------------------------


def test_merge_hook_repoints_ingredient_lines(seeded: Session, owner: User) -> None:
    """After merging gremlin_dust → flour, recipe line canonical_ingredient_id updates."""
    # Create recipe with an unreviewed ingredient
    data = _simple_recipe_in(
        lines=[
            IngredientLineIn(
                original_text="1 tsp gremlin dust",
                quantity=1,
                unit="tsp",
                name="gremlin dust",
            )
        ]
    )
    out = cookbook_service.create_recipe(seeded, owner_id=owner.id, data=data)
    line_id = out.groups[0].lines[0].id
    gremlin_id = out.groups[0].lines[0].canonical_ingredient_id
    assert gremlin_id is not None

    # Get flour's canonical id
    flour = catalog_service.match(seeded, "all-purpose flour")
    assert flour is not None
    flour_id = flour.id

    # Merge gremlin dust into flour
    catalog_service.merge(seeded, source_id=gremlin_id, target_id=flour_id)

    # Re-fetch the line directly
    line = seeded.get(IngredientLine, line_id)
    assert line is not None
    assert line.canonical_ingredient_id == flour_id


# ---------------------------------------------------------------------------
# list_recipes — search, filters, dietary rule, pagination
# ---------------------------------------------------------------------------


def _recipe_with_ingredient(
    seeded: Session,
    owner: User,
    *,
    title: str,
    ingredient_name: str | None,
    original_text: str | None = None,
    **kwargs: Any,
) -> Any:
    lines = [
        IngredientLineIn(
            original_text=original_text or (ingredient_name or "a pinch of something"),
            quantity=1,
            unit="cup" if ingredient_name else None,
            name=ingredient_name,
        )
    ]
    data = _simple_recipe_in(title=title, lines=lines)
    return cookbook_service.create_recipe(seeded, owner_id=owner.id, data=data, **kwargs)


def test_list_recipes_search_matches_title(seeded: Session, owner: User) -> None:
    _recipe_with_ingredient(seeded, owner, title="Grandma's Apple Pie", ingredient_name=None)
    _recipe_with_ingredient(seeded, owner, title="Beef Stew", ingredient_name=None)

    page = cookbook_service.list_recipes(seeded, owner_id=owner.id, q="Apple Pie")
    assert [r.title for r in page.items] == ["Grandma's Apple Pie"]
    assert page.total == 1


def test_list_recipes_search_matches_description_via_fts(seeded: Session, owner: User) -> None:
    data = _simple_recipe_in(
        title="Weeknight Dinner",
        lines=[IngredientLineIn(original_text="a pinch of something")],
    )
    data.description = "A comforting bowl of spicy lentil soup for cold nights."
    cookbook_service.create_recipe(seeded, owner_id=owner.id, data=data)
    _recipe_with_ingredient(seeded, owner, title="Unrelated Recipe", ingredient_name=None)

    page = cookbook_service.list_recipes(seeded, owner_id=owner.id, q="lentil soup")
    assert [r.title for r in page.items] == ["Weeknight Dinner"]


def test_list_recipes_search_matches_ingredient_trigram(seeded: Session, owner: User) -> None:
    _recipe_with_ingredient(
        seeded,
        owner,
        title="Mystery Bake",
        ingredient_name=None,
        original_text="two cups of pomegranate molasses",
    )
    _recipe_with_ingredient(seeded, owner, title="Unrelated Recipe", ingredient_name=None)

    page = cookbook_service.list_recipes(seeded, owner_id=owner.id, q="pomegranate molasses")
    assert [r.title for r in page.items] == ["Mystery Bake"]


def test_list_recipes_search_no_match_returns_empty(seeded: Session, owner: User) -> None:
    _recipe_with_ingredient(seeded, owner, title="Beef Stew", ingredient_name=None)
    page = cookbook_service.list_recipes(seeded, owner_id=owner.id, q="xyzzy nonexistent term")
    assert page.items == []
    assert page.total == 0


def test_list_recipes_filter_by_cuisine(seeded: Session, owner: User) -> None:
    cookbook_service.create_recipe(
        seeded,
        owner_id=owner.id,
        data=_simple_recipe_in(
            title="Pasta", cuisines=["Italian"], lines=[IngredientLineIn(original_text="water")]
        ),
    )
    cookbook_service.create_recipe(
        seeded,
        owner_id=owner.id,
        data=_simple_recipe_in(
            title="Curry", cuisines=["Indian"], lines=[IngredientLineIn(original_text="water")]
        ),
    )
    page = cookbook_service.list_recipes(seeded, owner_id=owner.id, cuisine="italian")
    assert [r.title for r in page.items] == ["Pasta"]


def test_list_recipes_filter_by_dish_type(seeded: Session, owner: User) -> None:
    cookbook_service.create_recipe(
        seeded,
        owner_id=owner.id,
        data=_simple_recipe_in(
            title="Cake", dish_types=["dessert"], lines=[IngredientLineIn(original_text="water")]
        ),
    )
    cookbook_service.create_recipe(
        seeded,
        owner_id=owner.id,
        data=_simple_recipe_in(
            title="Stew", dish_types=["main"], lines=[IngredientLineIn(original_text="water")]
        ),
    )
    page = cookbook_service.list_recipes(seeded, owner_id=owner.id, dish_type="dessert")
    assert [r.title for r in page.items] == ["Cake"]


def test_list_recipes_filter_by_tag(seeded: Session, owner: User) -> None:
    cookbook_service.create_recipe(
        seeded,
        owner_id=owner.id,
        data=_simple_recipe_in(
            title="Quick Salad", tags=["quick"], lines=[IngredientLineIn(original_text="water")]
        ),
    )
    cookbook_service.create_recipe(
        seeded,
        owner_id=owner.id,
        data=_simple_recipe_in(
            title="Slow Roast", tags=["slow"], lines=[IngredientLineIn(original_text="water")]
        ),
    )
    page = cookbook_service.list_recipes(seeded, owner_id=owner.id, tag="quick")
    assert [r.title for r in page.items] == ["Quick Salad"]


def test_list_recipes_filter_by_max_total_min(seeded: Session, owner: User) -> None:
    quick = _simple_recipe_in(title="Quick", lines=[IngredientLineIn(original_text="water")])
    quick.total_min = 15
    slow = _simple_recipe_in(title="Slow", lines=[IngredientLineIn(original_text="water")])
    slow.total_min = 120
    unknown = _simple_recipe_in(title="Unknown", lines=[IngredientLineIn(original_text="water")])

    cookbook_service.create_recipe(seeded, owner_id=owner.id, data=quick)
    cookbook_service.create_recipe(seeded, owner_id=owner.id, data=slow)
    cookbook_service.create_recipe(seeded, owner_id=owner.id, data=unknown)

    page = cookbook_service.list_recipes(seeded, owner_id=owner.id, max_total_min=30)
    # Recipes with unknown total_min are excluded — we can't confirm they fit.
    assert [r.title for r in page.items] == ["Quick"]


def test_list_recipes_filter_by_source_type(seeded: Session, owner: User) -> None:
    cookbook_service.create_recipe(
        seeded,
        owner_id=owner.id,
        data=_simple_recipe_in(title="Manual", lines=[IngredientLineIn(original_text="water")]),
        source_type=SourceType.manual,
    )
    cookbook_service.create_recipe(
        seeded,
        owner_id=owner.id,
        data=_simple_recipe_in(title="Web", lines=[IngredientLineIn(original_text="water")]),
        source_type=SourceType.web,
    )
    page = cookbook_service.list_recipes(seeded, owner_id=owner.id, source_type=SourceType.web)
    assert [r.title for r in page.items] == ["Web"]


def test_list_recipes_filter_by_favorites(seeded: Session, owner: User) -> None:
    fav = cookbook_service.create_recipe(
        seeded,
        owner_id=owner.id,
        data=_simple_recipe_in(title="Favorite", lines=[IngredientLineIn(original_text="water")]),
    )
    cookbook_service.create_recipe(
        seeded,
        owner_id=owner.id,
        data=_simple_recipe_in(
            title="Not Favorite", lines=[IngredientLineIn(original_text="water")]
        ),
    )
    cookbook_service.set_personal(seeded, owner_id=owner.id, recipe_id=fav.id, is_favorite=True)

    page = cookbook_service.list_recipes(seeded, owner_id=owner.id, favorites=True)
    assert [r.title for r in page.items] == ["Favorite"]


def test_list_recipes_filters_compose(seeded: Session, owner: User) -> None:
    """cuisine + q both apply (ANDed) — a recipe matching only one is excluded."""
    cookbook_service.create_recipe(
        seeded,
        owner_id=owner.id,
        data=_simple_recipe_in(
            title="Italian Apple Cake",
            cuisines=["Italian"],
            lines=[IngredientLineIn(original_text="water")],
        ),
    )
    cookbook_service.create_recipe(
        seeded,
        owner_id=owner.id,
        data=_simple_recipe_in(
            title="Indian Apple Curry",
            cuisines=["Indian"],
            lines=[IngredientLineIn(original_text="water")],
        ),
    )
    page = cookbook_service.list_recipes(seeded, owner_id=owner.id, q="Apple", cuisine="italian")
    assert [r.title for r in page.items] == ["Italian Apple Cake"]


def test_list_recipes_pagination_total_and_slice(seeded: Session, owner: User) -> None:
    for i in range(5):
        cookbook_service.create_recipe(
            seeded,
            owner_id=owner.id,
            data=_simple_recipe_in(
                title=f"Recipe {i}", lines=[IngredientLineIn(original_text="water")]
            ),
        )
    page1 = cookbook_service.list_recipes(seeded, owner_id=owner.id, limit=2, offset=0)
    assert page1.total == 5
    assert page1.limit == 2
    assert page1.offset == 0
    assert [r.title for r in page1.items] == ["Recipe 4", "Recipe 3"]

    page2 = cookbook_service.list_recipes(seeded, owner_id=owner.id, limit=2, offset=2)
    assert page2.total == 5
    assert [r.title for r in page2.items] == ["Recipe 2", "Recipe 1"]


def test_list_recipes_default_behavior_returns_all_newest_first(
    seeded: Session, owner: User
) -> None:
    """No params: identical to the pre-pagination behavior, just RecipePage-wrapped."""
    for i in range(3):
        cookbook_service.create_recipe(
            seeded,
            owner_id=owner.id,
            data=_simple_recipe_in(
                title=f"Recipe {i}", lines=[IngredientLineIn(original_text="water")]
            ),
        )
    page = cookbook_service.list_recipes(seeded, owner_id=owner.id)
    assert page.total == 3
    assert [r.title for r in page.items] == ["Recipe 2", "Recipe 1", "Recipe 0"]


def test_list_recipes_invalid_dietary_raises_422(seeded: Session, owner: User) -> None:
    with pytest.raises(ApiError) as exc_info:
        cookbook_service.list_recipes(seeded, owner_id=owner.id, dietary="carnivore")
    assert exc_info.value.status_code == 422


# ---------------------------------------------------------------------------
# list_recipes — dietary rule
# ---------------------------------------------------------------------------


def test_list_recipes_dietary_gluten_free_excludes_gluten(seeded: Session, owner: User) -> None:
    _recipe_with_ingredient(seeded, owner, title="Bread", ingredient_name="all-purpose flour")
    _recipe_with_ingredient(seeded, owner, title="Rice Bowl", ingredient_name="olive oil")

    page = cookbook_service.list_recipes(seeded, owner_id=owner.id, dietary="gluten_free")
    assert [r.title for r in page.items] == ["Rice Bowl"]


def test_list_recipes_dietary_vegan_excludes_animal_products(seeded: Session, owner: User) -> None:
    _recipe_with_ingredient(seeded, owner, title="Vinaigrette", ingredient_name="olive oil")
    _recipe_with_ingredient(seeded, owner, title="Omelet", ingredient_name="egg")
    _recipe_with_ingredient(seeded, owner, title="Beef Stew", ingredient_name="ground beef")

    page = cookbook_service.list_recipes(seeded, owner_id=owner.id, dietary="vegan")
    assert [r.title for r in page.items] == ["Vinaigrette"]


def test_list_recipes_dietary_vegetarian_allows_dairy_and_egg_excludes_meat_and_fish(
    seeded: Session, owner: User
) -> None:
    _recipe_with_ingredient(seeded, owner, title="Omelet", ingredient_name="egg")
    _recipe_with_ingredient(seeded, owner, title="Milk Pudding", ingredient_name="milk")
    _recipe_with_ingredient(seeded, owner, title="Beef Stew", ingredient_name="ground beef")
    _recipe_with_ingredient(seeded, owner, title="Grilled Salmon", ingredient_name="salmon")
    _recipe_with_ingredient(seeded, owner, title="Shrimp Cocktail", ingredient_name="shrimp")

    page = cookbook_service.list_recipes(seeded, owner_id=owner.id, dietary="vegetarian")
    assert {r.title for r in page.items} == {"Omelet", "Milk Pudding"}


def test_list_recipes_dietary_unmatched_ingredient_line_does_not_disqualify(
    seeded: Session, owner: User
) -> None:
    """A free-text line with no catalog match doesn't disqualify a vegan recipe,
    even though its text mentions a non-vegan word."""
    data = _simple_recipe_in(
        title="Ambiguous Salad",
        lines=[
            IngredientLineIn(original_text="olive oil", quantity=1, unit="cup", name="olive oil"),
            # No `name` -> no catalog match attempted -> canonical_ingredient_id stays
            # None regardless of what the free text says.
            IngredientLineIn(original_text="a dash of bacon flavoring (imaginary)"),
        ],
    )
    cookbook_service.create_recipe(seeded, owner_id=owner.id, data=data)

    page = cookbook_service.list_recipes(seeded, owner_id=owner.id, dietary="vegan")
    assert [r.title for r in page.items] == ["Ambiguous Salad"]


# ---------------------------------------------------------------------------
# recommendations_for_recipe — Phase 3 Task 8 (deterministic content overlap)
# ---------------------------------------------------------------------------


def _recipe_with(
    seeded: Session,
    owner: User,
    *,
    title: str,
    cuisines: list[str] | None = None,
    ingredient_names: list[str] | None = None,
) -> Any:
    """Create a recipe with the given cuisines and catalog-matched ingredient names.

    Every name in *ingredient_names* must be an exact seed catalog name (e.g.
    "olive oil", "garlic", "onion", "butter") so `create_recipe`'s fuzzy
    matching (no LLM) resolves it to a real `canonical_ingredient_id` —
    required for the ingredient-overlap leg of the scoring formula to fire.
    """
    lines = [
        IngredientLineIn(original_text=name, quantity=1, unit="cup", name=name)
        for name in (ingredient_names or [])
    ] or [IngredientLineIn(original_text="a pinch of something")]
    data = _simple_recipe_in(title=title, lines=lines, cuisines=cuisines)
    return cookbook_service.create_recipe(seeded, owner_id=owner.id, data=data)


def test_recommendations_ranks_cuisine_and_ingredient_overlap_above_single_ingredient(
    seeded: Session, owner: User
) -> None:
    """A shared cuisine + 2 shared ingredients outranks a recipe sharing only 1 ingredient.

    target: Italian, {olive oil, garlic, onion}
    strong: Italian, {garlic, onion}       -> 4*1 (cuisine) + 1*2 (ingredients) = 6
    weak:   (no cuisine overlap), {garlic} -> 1*1 (ingredient)                  = 1
    """
    target = _recipe_with(
        seeded,
        owner,
        title="Target",
        cuisines=["Italian"],
        ingredient_names=["olive oil", "garlic", "onion"],
    )
    strong = _recipe_with(
        seeded,
        owner,
        title="Strong Match",
        cuisines=["Italian"],
        ingredient_names=["garlic", "onion"],
    )
    weak = _recipe_with(
        seeded, owner, title="Weak Match", cuisines=["Mexican"], ingredient_names=["garlic"]
    )

    results = cookbook_service.recommendations_for_recipe(
        seeded, user_id=owner.id, recipe_id=target.id
    )
    assert [r.id for r in results] == [strong.id, weak.id]


def test_recommendations_excludes_self(seeded: Session, owner: User) -> None:
    target = _recipe_with(
        seeded, owner, title="Target", cuisines=["Italian"], ingredient_names=["olive oil"]
    )
    _recipe_with(seeded, owner, title="Other", cuisines=["Italian"], ingredient_names=["olive oil"])

    results = cookbook_service.recommendations_for_recipe(
        seeded, user_id=owner.id, recipe_id=target.id
    )
    assert target.id not in [r.id for r in results]


def test_recommendations_owner_scoped_excludes_other_users_recipes(
    seeded: Session, owner: User
) -> None:
    other = make_user(seeded, suffix=str(uuid.uuid4())[:8])
    target = _recipe_with(
        seeded,
        owner,
        title="Target",
        cuisines=["Italian"],
        ingredient_names=["olive oil", "garlic"],
    )
    # Same owner as `owner` for genuine overlap-> should show up.
    mine = _recipe_with(
        seeded,
        owner,
        title="Mine",
        cuisines=["Italian"],
        ingredient_names=["olive oil", "garlic"],
    )
    # Belongs to a DIFFERENT user, even though it overlaps perfectly -> must never appear.
    _recipe_with(
        seeded,
        other,
        title="Not Mine",
        cuisines=["Italian"],
        ingredient_names=["olive oil", "garlic"],
    )

    results = cookbook_service.recommendations_for_recipe(
        seeded, user_id=owner.id, recipe_id=target.id
    )
    assert [r.id for r in results] == [mine.id]


def test_recommendations_empty_when_no_overlap(seeded: Session, owner: User) -> None:
    target = _recipe_with(
        seeded, owner, title="Target", cuisines=["Italian"], ingredient_names=["olive oil"]
    )
    _recipe_with(seeded, owner, title="Unrelated", cuisines=["Japanese"], ingredient_names=["egg"])

    results = cookbook_service.recommendations_for_recipe(
        seeded, user_id=owner.id, recipe_id=target.id
    )
    assert results == []


def test_recommendations_ties_broken_by_recency(seeded: Session, owner: User) -> None:
    """Two candidates with an IDENTICAL score order newest-created first."""
    target = _recipe_with(
        seeded, owner, title="Target", cuisines=["Italian"], ingredient_names=["olive oil"]
    )
    older = _recipe_with(
        seeded, owner, title="Older Match", cuisines=["Italian"], ingredient_names=[]
    )
    newer = _recipe_with(
        seeded, owner, title="Newer Match", cuisines=["Italian"], ingredient_names=[]
    )

    results = cookbook_service.recommendations_for_recipe(
        seeded, user_id=owner.id, recipe_id=target.id
    )
    assert [r.id for r in results] == [newer.id, older.id]


def test_recommendations_respects_limit(seeded: Session, owner: User) -> None:
    target = _recipe_with(
        seeded, owner, title="Target", cuisines=["Italian"], ingredient_names=["olive oil"]
    )
    for i in range(3):
        _recipe_with(seeded, owner, title=f"Match {i}", cuisines=["Italian"], ingredient_names=[])

    results = cookbook_service.recommendations_for_recipe(
        seeded, user_id=owner.id, recipe_id=target.id, limit=2
    )
    assert len(results) == 2


def test_recommendations_missing_recipe_raises_404(seeded: Session, owner: User) -> None:
    with pytest.raises(ApiError) as exc_info:
        cookbook_service.recommendations_for_recipe(
            seeded, user_id=owner.id, recipe_id=uuid.uuid4()
        )
    assert exc_info.value.status_code == 404


def test_recommendations_wrong_owner_raises_404(seeded: Session, owner: User) -> None:
    other = make_user(seeded, suffix=str(uuid.uuid4())[:8])
    target = _recipe_with(
        seeded, other, title="Target", cuisines=["Italian"], ingredient_names=["olive oil"]
    )
    with pytest.raises(ApiError) as exc_info:
        cookbook_service.recommendations_for_recipe(seeded, user_id=owner.id, recipe_id=target.id)
    assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# Collections — create/rename/delete/list
# ---------------------------------------------------------------------------


def test_create_collection_happy_path(seeded: Session, owner: User) -> None:
    out = cookbook_service.create_collection(seeded, owner_id=owner.id, name="Weeknight Dinners")
    assert out.name == "Weeknight Dinners"
    assert out.recipe_count == 0
    assert out.id is not None


def test_create_collection_strips_whitespace(seeded: Session, owner: User) -> None:
    out = cookbook_service.create_collection(seeded, owner_id=owner.id, name="  Brunch  ")
    assert out.name == "Brunch"


def test_create_collection_duplicate_name_same_owner_raises_409(
    seeded: Session, owner: User
) -> None:
    cookbook_service.create_collection(seeded, owner_id=owner.id, name="Desserts")
    with pytest.raises(DuplicateCollectionNameError) as exc_info:
        cookbook_service.create_collection(seeded, owner_id=owner.id, name="Desserts")
    assert exc_info.value.status_code == 409
    assert exc_info.value.code == "duplicate_name"


def test_create_collection_same_name_different_owners_ok(seeded: Session, owner: User) -> None:
    other = make_user(seeded, suffix=str(uuid.uuid4())[:8])
    cookbook_service.create_collection(seeded, owner_id=owner.id, name="Favorites")
    out = cookbook_service.create_collection(seeded, owner_id=other.id, name="Favorites")
    assert out.name == "Favorites"


def test_rename_collection_happy_path(seeded: Session, owner: User) -> None:
    created = cookbook_service.create_collection(seeded, owner_id=owner.id, name="Old Name")
    renamed = cookbook_service.rename_collection(
        seeded, owner_id=owner.id, collection_id=created.id, name="New Name"
    )
    assert renamed.name == "New Name"
    assert renamed.id == created.id


def test_rename_collection_to_same_name_is_noop(seeded: Session, owner: User) -> None:
    created = cookbook_service.create_collection(seeded, owner_id=owner.id, name="Same")
    renamed = cookbook_service.rename_collection(
        seeded, owner_id=owner.id, collection_id=created.id, name="Same"
    )
    assert renamed.name == "Same"


def test_rename_collection_duplicate_name_raises_409(seeded: Session, owner: User) -> None:
    cookbook_service.create_collection(seeded, owner_id=owner.id, name="Taken")
    other = cookbook_service.create_collection(seeded, owner_id=owner.id, name="Free")
    with pytest.raises(DuplicateCollectionNameError):
        cookbook_service.rename_collection(
            seeded, owner_id=owner.id, collection_id=other.id, name="Taken"
        )


def test_rename_collection_wrong_owner_raises_404(seeded: Session, owner: User) -> None:
    other = make_user(seeded, suffix=str(uuid.uuid4())[:8])
    created = cookbook_service.create_collection(seeded, owner_id=owner.id, name="Mine")
    with pytest.raises(ApiError) as exc_info:
        cookbook_service.rename_collection(
            seeded, owner_id=other.id, collection_id=created.id, name="Stolen"
        )
    assert exc_info.value.status_code == 404


def test_rename_collection_missing_raises_404(seeded: Session, owner: User) -> None:
    with pytest.raises(ApiError) as exc_info:
        cookbook_service.rename_collection(
            seeded, owner_id=owner.id, collection_id=uuid.uuid4(), name="Whatever"
        )
    assert exc_info.value.status_code == 404


def test_delete_collection_wrong_owner_raises_404(seeded: Session, owner: User) -> None:
    other = make_user(seeded, suffix=str(uuid.uuid4())[:8])
    created = cookbook_service.create_collection(seeded, owner_id=owner.id, name="Mine")
    with pytest.raises(ApiError) as exc_info:
        cookbook_service.delete_collection(seeded, owner_id=other.id, collection_id=created.id)
    assert exc_info.value.status_code == 404


def test_delete_collection_missing_raises_404(seeded: Session, owner: User) -> None:
    with pytest.raises(ApiError) as exc_info:
        cookbook_service.delete_collection(seeded, owner_id=owner.id, collection_id=uuid.uuid4())
    assert exc_info.value.status_code == 404


def test_delete_collection_leaves_recipes_intact(seeded: Session, owner: User) -> None:
    """Deleting a collection removes membership only; the recipe itself survives."""
    recipe = cookbook_service.create_recipe(seeded, owner_id=owner.id, data=_simple_recipe_in())
    collection = cookbook_service.create_collection(seeded, owner_id=owner.id, name="Temp")
    cookbook_service.set_recipe_collections(
        seeded, owner_id=owner.id, recipe_id=recipe.id, collection_ids=[collection.id]
    )

    cookbook_service.delete_collection(seeded, owner_id=owner.id, collection_id=collection.id)

    still_there = cookbook_service.get_recipe(seeded, owner_id=owner.id, recipe_id=recipe.id)
    assert still_there.id == recipe.id
    assert still_there.collection_ids == []
    assert cookbook_service.list_collections(seeded, owner_id=owner.id) == []


def test_list_collections_empty_for_new_owner(seeded: Session, owner: User) -> None:
    assert cookbook_service.list_collections(seeded, owner_id=owner.id) == []


def test_list_collections_includes_recipe_count(seeded: Session, owner: User) -> None:
    r1 = cookbook_service.create_recipe(
        seeded, owner_id=owner.id, data=_simple_recipe_in(title="R1")
    )
    r2 = cookbook_service.create_recipe(
        seeded, owner_id=owner.id, data=_simple_recipe_in(title="R2")
    )
    collection = cookbook_service.create_collection(seeded, owner_id=owner.id, name="Batch")
    cookbook_service.set_recipe_collections(
        seeded, owner_id=owner.id, recipe_id=r1.id, collection_ids=[collection.id]
    )
    cookbook_service.set_recipe_collections(
        seeded, owner_id=owner.id, recipe_id=r2.id, collection_ids=[collection.id]
    )

    listed = cookbook_service.list_collections(seeded, owner_id=owner.id)
    assert len(listed) == 1
    assert listed[0].id == collection.id
    assert listed[0].recipe_count == 2


def test_list_collections_only_owners_own(seeded: Session, owner: User) -> None:
    other = make_user(seeded, suffix=str(uuid.uuid4())[:8])
    cookbook_service.create_collection(seeded, owner_id=other.id, name="Not Mine")
    assert cookbook_service.list_collections(seeded, owner_id=owner.id) == []


# ---------------------------------------------------------------------------
# set_recipe_collections — full-replace semantics + ownership
# ---------------------------------------------------------------------------


def test_set_recipe_collections_full_replace_semantics(seeded: Session, owner: User) -> None:
    recipe = cookbook_service.create_recipe(seeded, owner_id=owner.id, data=_simple_recipe_in())
    c1 = cookbook_service.create_collection(seeded, owner_id=owner.id, name="C1")
    c2 = cookbook_service.create_collection(seeded, owner_id=owner.id, name="C2")

    out = cookbook_service.set_recipe_collections(
        seeded, owner_id=owner.id, recipe_id=recipe.id, collection_ids=[c1.id, c2.id]
    )
    assert set(out.collection_ids) == {c1.id, c2.id}

    # Replacing with just c2 drops c1 entirely (full-replace, not additive).
    out2 = cookbook_service.set_recipe_collections(
        seeded, owner_id=owner.id, recipe_id=recipe.id, collection_ids=[c2.id]
    )
    assert out2.collection_ids == [c2.id]


def test_set_recipe_collections_empty_list_clears(seeded: Session, owner: User) -> None:
    recipe = cookbook_service.create_recipe(seeded, owner_id=owner.id, data=_simple_recipe_in())
    c1 = cookbook_service.create_collection(seeded, owner_id=owner.id, name="C1")
    cookbook_service.set_recipe_collections(
        seeded, owner_id=owner.id, recipe_id=recipe.id, collection_ids=[c1.id]
    )
    out = cookbook_service.set_recipe_collections(
        seeded, owner_id=owner.id, recipe_id=recipe.id, collection_ids=[]
    )
    assert out.collection_ids == []


def test_set_recipe_collections_dedupes_ids(seeded: Session, owner: User) -> None:
    recipe = cookbook_service.create_recipe(seeded, owner_id=owner.id, data=_simple_recipe_in())
    c1 = cookbook_service.create_collection(seeded, owner_id=owner.id, name="C1")
    out = cookbook_service.set_recipe_collections(
        seeded, owner_id=owner.id, recipe_id=recipe.id, collection_ids=[c1.id, c1.id]
    )
    assert out.collection_ids == [c1.id]


def test_set_recipe_collections_wrong_owner_recipe_raises_404(seeded: Session, owner: User) -> None:
    other = make_user(seeded, suffix=str(uuid.uuid4())[:8])
    recipe = cookbook_service.create_recipe(seeded, owner_id=owner.id, data=_simple_recipe_in())
    with pytest.raises(ApiError) as exc_info:
        cookbook_service.set_recipe_collections(
            seeded, owner_id=other.id, recipe_id=recipe.id, collection_ids=[]
        )
    assert exc_info.value.status_code == 404


def test_set_recipe_collections_foreign_collection_raises_404(seeded: Session, owner: User) -> None:
    """A collection_id belonging to another user 404s rather than silently linking."""
    other = make_user(seeded, suffix=str(uuid.uuid4())[:8])
    recipe = cookbook_service.create_recipe(seeded, owner_id=owner.id, data=_simple_recipe_in())
    foreign_collection = cookbook_service.create_collection(
        seeded, owner_id=other.id, name="Not Yours"
    )
    with pytest.raises(ApiError) as exc_info:
        cookbook_service.set_recipe_collections(
            seeded,
            owner_id=owner.id,
            recipe_id=recipe.id,
            collection_ids=[foreign_collection.id],
        )
    assert exc_info.value.status_code == 404

    # And the recipe's membership set is unchanged (all-or-nothing, no partial apply).
    reloaded = cookbook_service.get_recipe(seeded, owner_id=owner.id, recipe_id=recipe.id)
    assert reloaded.collection_ids == []


def test_set_recipe_collections_missing_collection_id_raises_404(
    seeded: Session, owner: User
) -> None:
    recipe = cookbook_service.create_recipe(seeded, owner_id=owner.id, data=_simple_recipe_in())
    with pytest.raises(ApiError) as exc_info:
        cookbook_service.set_recipe_collections(
            seeded, owner_id=owner.id, recipe_id=recipe.id, collection_ids=[uuid.uuid4()]
        )
    assert exc_info.value.status_code == 404


def test_recipe_out_includes_collection_ids(seeded: Session, owner: User) -> None:
    """create_recipe/get_recipe expose collection_ids ([] by default)."""
    created = cookbook_service.create_recipe(seeded, owner_id=owner.id, data=_simple_recipe_in())
    assert created.collection_ids == []
    fetched = cookbook_service.get_recipe(seeded, owner_id=owner.id, recipe_id=created.id)
    assert fetched.collection_ids == []


def test_recipe_summary_does_not_include_collection_ids(seeded: Session, owner: User) -> None:
    """RecipeSummary (the list_recipes item shape) never grows a collections field."""
    cookbook_service.create_recipe(seeded, owner_id=owner.id, data=_simple_recipe_in())
    page = cookbook_service.list_recipes(seeded, owner_id=owner.id)
    assert not hasattr(page.items[0], "collection_ids")


# ---------------------------------------------------------------------------
# list_recipes — collection filter
# ---------------------------------------------------------------------------


def test_list_recipes_filter_by_collection(seeded: Session, owner: User) -> None:
    in_collection = cookbook_service.create_recipe(
        seeded, owner_id=owner.id, data=_simple_recipe_in(title="In Collection")
    )
    cookbook_service.create_recipe(
        seeded, owner_id=owner.id, data=_simple_recipe_in(title="Not In Collection")
    )
    collection = cookbook_service.create_collection(seeded, owner_id=owner.id, name="Picks")
    cookbook_service.set_recipe_collections(
        seeded, owner_id=owner.id, recipe_id=in_collection.id, collection_ids=[collection.id]
    )

    page = cookbook_service.list_recipes(seeded, owner_id=owner.id, collection=collection.id)
    assert [r.title for r in page.items] == ["In Collection"]


def test_list_recipes_filter_by_collection_anded_with_other_filters(
    seeded: Session, owner: User
) -> None:
    matching = cookbook_service.create_recipe(
        seeded,
        owner_id=owner.id,
        data=_simple_recipe_in(title="Matches Both", tags=["quick"]),
    )
    cookbook_service.create_recipe(
        seeded,
        owner_id=owner.id,
        data=_simple_recipe_in(title="Right Collection Wrong Tag"),
    )
    collection = cookbook_service.create_collection(seeded, owner_id=owner.id, name="Picks")
    other_in_collection = cookbook_service.create_recipe(
        seeded,
        owner_id=owner.id,
        data=_simple_recipe_in(title="Right Tag Wrong Collection", tags=["quick"]),
    )
    cookbook_service.set_recipe_collections(
        seeded, owner_id=owner.id, recipe_id=matching.id, collection_ids=[collection.id]
    )
    # Give the "wrong collection" recipe a *different* collection so it's a
    # collection member (just not this one) — confirms the filter isn't a
    # no-op tautology.
    other_collection = cookbook_service.create_collection(seeded, owner_id=owner.id, name="Other")
    cookbook_service.set_recipe_collections(
        seeded,
        owner_id=owner.id,
        recipe_id=other_in_collection.id,
        collection_ids=[other_collection.id],
    )

    page = cookbook_service.list_recipes(
        seeded, owner_id=owner.id, collection=collection.id, tag="quick"
    )
    assert [r.title for r in page.items] == ["Matches Both"]


def test_list_recipes_filter_by_foreign_collection_matches_nothing(
    seeded: Session, owner: User
) -> None:
    other = make_user(seeded, suffix=str(uuid.uuid4())[:8])
    cookbook_service.create_recipe(seeded, owner_id=owner.id, data=_simple_recipe_in())
    foreign_collection = cookbook_service.create_collection(
        seeded, owner_id=other.id, name="Not Yours"
    )
    page = cookbook_service.list_recipes(
        seeded, owner_id=owner.id, collection=foreign_collection.id
    )
    assert page.items == []


# ---------------------------------------------------------------------------
# copy_recipe — deep-copy fidelity (the copy-on-share primitive)
# ---------------------------------------------------------------------------


def _rich_recipe_in() -> RecipeIn:
    return RecipeIn(
        title="Copyable Cake",
        description="A cake worth sharing.",
        language="en",
        servings=None,
        prep_min=10,
        cook_min=20,
        total_min=30,
        cuisines=["Italian"],
        dish_types=["main"],
        tags=["quick"],
        groups=[
            IngredientGroupIn(
                name="Dry",
                lines=[
                    IngredientLineIn(
                        original_text="1 cup flour",
                        quantity=1,
                        unit="cup",
                        name="all-purpose flour",
                    ),
                    IngredientLineIn(original_text="a pinch of salt", note="to taste"),
                ],
            ),
            IngredientGroupIn(
                name="Wet",
                lines=[IngredientLineIn(original_text="2 eggs", is_optional=True)],
            ),
        ],
        steps=[StepIn(original_text="Mix dry."), StepIn(original_text="Add wet.")],
    )


def test_copy_recipe_deep_copies_groups_lines_and_remaps_step_refs(
    seeded: Session, owner: User
) -> None:
    recipient = make_user(seeded, suffix=str(uuid.uuid4())[:8])
    created = cookbook_service.create_recipe(
        seeded,
        owner_id=owner.id,
        data=_rich_recipe_in(),
        source="https://example.com/cake",
        source_type=SourceType.web,
    )
    cookbook_service.verify_recipe(seeded, owner.id, created.id)

    # Set an image_ref directly at the ORM layer (bypasses the FileStore-backed
    # upload endpoint, which is irrelevant here) to prove the copy shares the
    # ref BY VALUE without re-deriving or clearing it.
    recipe_row = seeded.get(Recipe, created.id)
    assert recipe_row is not None
    recipe_row.image_ref = "deadbeef1234/deadbeef1234.jpg"
    seeded.flush()

    flour_line_id = created.groups[0].lines[0].id

    # ingredient_line_refs isn't settable via RecipeIn yet (see cookbook/models.py's
    # Step docstring) — set it directly at the ORM layer to exercise the remap.
    step_row = seeded.scalars(
        select(Step).where(Step.recipe_id == created.id, Step.order_index == 0)
    ).first()
    assert step_row is not None
    step_row.ingredient_line_refs = [str(flour_line_id)]
    seeded.flush()

    copied = cookbook_service.copy_recipe(
        seeded,
        created.id,
        new_owner_id=recipient.id,
        provenance={
            "shared_by": owner.email,
            "shared_at": "2026-07-08T00:00:00+00:00",
            "origin_recipe_id": str(created.id),
        },
    )
    seeded.flush()

    assert copied.id != created.id
    assert copied.owner_id == recipient.id

    out = cookbook_service.get_recipe(seeded, owner_id=recipient.id, recipe_id=copied.id)

    # Verbatim scalar fields
    assert out.title == "Copyable Cake"
    assert out.description == "A cake worth sharing."
    assert out.language == "en"
    assert out.source == "https://example.com/cake"
    assert out.source_type == "web"
    assert out.prep_min == 10
    assert out.cook_min == 20
    assert out.total_min == 30
    assert out.is_verified is True  # preserved
    # Shared BY REFERENCE — same store key, no file duplication.
    assert out.image_ref == "/api/files/deadbeef1234/deadbeef1234.jpg"

    # Vocab links
    assert out.cuisines == ["Italian"]
    assert out.dish_types == ["main"]
    assert out.tags == ["quick"]

    # Groups / lines: same shape, new ids, all fields carried over
    assert len(out.groups) == 2
    assert [g.name for g in out.groups] == ["Dry", "Wet"]
    dry_lines = out.groups[0].lines
    assert len(dry_lines) == 2

    copied_flour_line = dry_lines[0]
    original_flour_line = created.groups[0].lines[0]
    assert copied_flour_line.id != original_flour_line.id  # new row, not shared
    assert copied_flour_line.original_text == original_flour_line.original_text
    assert copied_flour_line.canonical_ingredient_id == original_flour_line.canonical_ingredient_id
    assert copied_flour_line.normalized_amount == original_flour_line.normalized_amount
    assert copied_flour_line.normalized_unit == original_flour_line.normalized_unit
    assert copied_flour_line.is_approx == original_flour_line.is_approx

    salt_line = dry_lines[1]
    assert salt_line.original_text == "a pinch of salt"
    assert salt_line.note == "to taste"

    wet_line = out.groups[1].lines[0]
    assert wet_line.original_text == "2 eggs"
    assert wet_line.is_optional is True

    # Steps: text carried over, refs remapped to the NEW line ids (not left
    # dangling pointed at the original recipe's lines).
    assert len(out.steps) == 2
    assert out.steps[0].original_text == "Mix dry."
    assert out.steps[0].ingredient_line_refs == [str(copied_flour_line.id)]
    assert out.steps[0].ingredient_line_refs != [str(flour_line_id)]
    assert out.steps[1].ingredient_line_refs == []


def test_copy_recipe_not_copied_fields(seeded: Session, owner: User) -> None:
    recipient = make_user(seeded, suffix=str(uuid.uuid4())[:8])
    created = cookbook_service.create_recipe(
        seeded,
        owner_id=owner.id,
        data=_simple_recipe_in(),
        source_fingerprint="fp-123",
        extraction_meta={"tier_used": 1, "confidence": 0.9},
    )
    collection = cookbook_service.create_collection(seeded, owner_id=owner.id, name="Mine")
    cookbook_service.set_recipe_collections(
        seeded, owner_id=owner.id, recipe_id=created.id, collection_ids=[collection.id]
    )
    cookbook_service.set_personal(
        seeded, owner_id=owner.id, recipe_id=created.id, is_favorite=True, notes="delicious"
    )

    copied = cookbook_service.copy_recipe(
        seeded,
        created.id,
        new_owner_id=recipient.id,
        provenance={"shared_by": owner.email, "shared_at": "now", "origin_recipe_id": "x"},
    )
    seeded.flush()

    out = cookbook_service.get_recipe(seeded, owner_id=recipient.id, recipe_id=copied.id)
    assert out.is_favorite is False
    assert out.notes is None
    assert out.collection_ids == []
    assert out.extraction_meta is None
    assert out.derived_from is None
    assert out.last_edited_by is None
    assert out.last_edited_at is None

    copied_row = seeded.get(Recipe, copied.id)
    assert copied_row is not None
    assert copied_row.source_fingerprint is None

    # The original is completely untouched by the copy.
    original = cookbook_service.get_recipe(seeded, owner_id=owner.id, recipe_id=created.id)
    assert original.is_favorite is True
    assert original.notes == "delicious"
    assert original.collection_ids == [collection.id]


def test_copy_recipe_missing_recipe_raises_404(seeded: Session, owner: User) -> None:
    with pytest.raises(ApiError) as exc_info:
        cookbook_service.copy_recipe(
            seeded,
            uuid.uuid4(),
            new_owner_id=owner.id,
            provenance={"shared_by": "x", "shared_at": "now", "origin_recipe_id": "x"},
        )
    assert exc_info.value.status_code == 404


def test_copy_recipe_provenance_stored_verbatim(seeded: Session, owner: User) -> None:
    recipient = make_user(seeded, suffix=str(uuid.uuid4())[:8])
    created = cookbook_service.create_recipe(seeded, owner_id=owner.id, data=_simple_recipe_in())
    provenance = {
        "shared_by": owner.email,
        "shared_at": "2026-07-08T12:00:00+00:00",
        "origin_recipe_id": str(created.id),
    }
    copied = cookbook_service.copy_recipe(
        seeded, created.id, new_owner_id=recipient.id, provenance=provenance
    )
    seeded.flush()
    out = cookbook_service.get_recipe(seeded, owner_id=recipient.id, recipe_id=copied.id)
    assert out.provenance == provenance


# ---------------------------------------------------------------------------
# Fuzzy corpus is hoisted per request, not rebuilt per ingredient line
# ---------------------------------------------------------------------------


def _fuzzy_lines(n: int) -> list[IngredientLineIn]:
    """n lines that all miss the exact/alias index and hit the fuzzy path."""
    return [
        IngredientLineIn(original_text=f"a bit of zzq widget {i}", name=f"zzq widget {i}")
        for i in range(n)
    ]


def test_create_recipe_loads_fuzzy_corpus_once(
    seeded: Session, owner: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A multi-line recipe must materialise the catalog corpus at most once.

    Every unmatched line used to reload the whole alias + canonical corpus and
    re-run rapidfuzz over it, so a 40-line recipe meant ~40 full-catalog reads
    inside a single request.
    """
    calls = 0
    real_load = catalog_service._load_corpus

    def counting_load(db: Session) -> Any:
        nonlocal calls
        calls += 1
        return real_load(db)

    monkeypatch.setattr(catalog_service, "_load_corpus", counting_load)

    cookbook_service.create_recipe(
        seeded, owner_id=owner.id, data=_simple_recipe_in(lines=_fuzzy_lines(6))
    )
    assert calls == 1


def test_update_recipe_loads_fuzzy_corpus_once(
    seeded: Session, owner: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = cookbook_service.create_recipe(seeded, owner_id=owner.id, data=_simple_recipe_in())

    calls = 0
    real_load = catalog_service._load_corpus

    def counting_load(db: Session) -> Any:
        nonlocal calls
        calls += 1
        return real_load(db)

    monkeypatch.setattr(catalog_service, "_load_corpus", counting_load)

    cookbook_service.update_recipe(
        seeded,
        recipe_id=created.id,
        owner_id=owner.id,
        editor_id=owner.id,
        data=_simple_recipe_in(lines=_fuzzy_lines(5)),
    )
    assert calls == 1


# ---------------------------------------------------------------------------
# Recipe access derives from its cookbook (Task 5)
# ---------------------------------------------------------------------------


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


def test_create_recipe_into_editable_cookbook_ok(seeded: Session, owner: User) -> None:
    editor = make_user(seeded, suffix=str(uuid.uuid4())[:8])
    cookbook = make_cookbook(seeded, owner, name="Shared Book")
    add_member(seeded, cookbook, editor, CookbookRole.editor)

    created = cookbook_service.create_recipe(
        seeded, owner_id=editor.id, data=_simple_recipe_in(), cookbook_id=cookbook.id
    )
    assert created.cookbook_id == cookbook.id


def test_create_recipe_into_viewer_only_cookbook_raises_404(seeded: Session, owner: User) -> None:
    viewer = make_user(seeded, suffix=str(uuid.uuid4())[:8])
    cookbook = make_cookbook(seeded, owner, name="Viewer Book")
    add_member(seeded, cookbook, viewer, CookbookRole.viewer)

    with pytest.raises(ApiError) as exc_info:
        cookbook_service.create_recipe(
            seeded, owner_id=viewer.id, data=_simple_recipe_in(), cookbook_id=cookbook.id
        )
    assert exc_info.value.status_code == 404


def test_create_recipe_into_cookbook_not_a_member_of_raises_404(
    seeded: Session, owner: User
) -> None:
    stranger = make_user(seeded, suffix=str(uuid.uuid4())[:8])
    cookbook = make_cookbook(seeded, owner, name="Private Book")

    with pytest.raises(ApiError) as exc_info:
        cookbook_service.create_recipe(
            seeded, owner_id=stranger.id, data=_simple_recipe_in(), cookbook_id=cookbook.id
        )
    assert exc_info.value.status_code == 404


def test_create_recipe_omitted_cookbook_id_lands_in_default(seeded: Session, owner: User) -> None:
    created = cookbook_service.create_recipe(seeded, owner_id=owner.id, data=_simple_recipe_in())
    default_cookbook = cookbook_service.ensure_default_cookbook(seeded, owner.id)
    assert created.cookbook_id == default_cookbook.id


def test_move_recipe_between_editable_cookbooks_ok(seeded: Session, owner: User) -> None:
    source = make_cookbook(seeded, owner, name="Source")
    dest = make_cookbook(seeded, owner, name="Dest")
    created = cookbook_service.create_recipe(
        seeded, owner_id=owner.id, data=_simple_recipe_in(), cookbook_id=source.id
    )

    moved = cookbook_service.move_recipe(seeded, created.id, owner.id, dest.id)

    assert moved.cookbook_id == dest.id


def test_move_recipe_destination_not_editable_raises_404(seeded: Session, owner: User) -> None:
    source = make_cookbook(seeded, owner, name="Source")
    dest_owner = make_user(seeded, suffix=str(uuid.uuid4())[:8])
    dest = make_cookbook(seeded, dest_owner, name="Someone Else's Book")
    created = cookbook_service.create_recipe(
        seeded, owner_id=owner.id, data=_simple_recipe_in(), cookbook_id=source.id
    )

    with pytest.raises(ApiError) as exc_info:
        cookbook_service.move_recipe(seeded, created.id, owner.id, dest.id)
    assert exc_info.value.status_code == 404


def test_move_recipe_source_not_editable_raises_404(seeded: Session, owner: User) -> None:
    """A viewer-only member of the recipe's current cookbook cannot move it out,
    even if they have editor access to the destination cookbook."""
    source = make_cookbook(seeded, owner, name="Source")
    viewer = make_user(seeded, suffix=str(uuid.uuid4())[:8])
    add_member(seeded, source, viewer, CookbookRole.viewer)
    dest = make_cookbook(seeded, viewer, name="Viewer's Own Book")
    created = cookbook_service.create_recipe(
        seeded, owner_id=owner.id, data=_simple_recipe_in(), cookbook_id=source.id
    )

    with pytest.raises(ApiError) as exc_info:
        cookbook_service.move_recipe(seeded, created.id, viewer.id, dest.id)
    assert exc_info.value.status_code == 404


def test_viewer_of_shared_cookbook_can_get_but_not_update_or_delete(
    seeded: Session, owner: User
) -> None:
    viewer = make_user(seeded, suffix=str(uuid.uuid4())[:8])
    cookbook = make_cookbook(seeded, owner, name="Shared Book")
    add_member(seeded, cookbook, viewer, CookbookRole.viewer)
    created = cookbook_service.create_recipe(
        seeded, owner_id=owner.id, data=_simple_recipe_in(), cookbook_id=cookbook.id
    )

    fetched = cookbook_service.get_recipe(seeded, owner_id=viewer.id, recipe_id=created.id)
    assert fetched.id == created.id

    with pytest.raises(ApiError) as exc_info:
        cookbook_service.update_recipe(
            seeded,
            owner_id=viewer.id,
            recipe_id=created.id,
            data=_simple_recipe_in(title="Hacked"),
            editor_id=viewer.id,
        )
    assert exc_info.value.status_code == 404

    with pytest.raises(ApiError) as exc_info:
        cookbook_service.delete_recipe(seeded, owner_id=viewer.id, recipe_id=created.id)
    assert exc_info.value.status_code == 404


def test_editor_of_shared_cookbook_can_update_and_delete(seeded: Session, owner: User) -> None:
    editor = make_user(seeded, suffix=str(uuid.uuid4())[:8])
    cookbook = make_cookbook(seeded, owner, name="Shared Book")
    add_member(seeded, cookbook, editor, CookbookRole.editor)
    created = cookbook_service.create_recipe(
        seeded, owner_id=owner.id, data=_simple_recipe_in(), cookbook_id=cookbook.id
    )

    updated = cookbook_service.update_recipe(
        seeded,
        owner_id=editor.id,
        recipe_id=created.id,
        data=_simple_recipe_in(title="Edited by editor"),
        editor_id=editor.id,
    )
    assert updated.title == "Edited by editor"

    cookbook_service.delete_recipe(seeded, owner_id=editor.id, recipe_id=created.id)
    assert seeded.get(Recipe, created.id) is None


def test_legacy_null_cookbook_recipe_owner_only_access(seeded: Session, owner: User) -> None:
    """A recipe with cookbook_id=NULL (legacy, pre-migration) falls back to
    owner-only access — not derived from any cookbook — so it never 500s and
    never leaks to a non-owner."""
    other = make_user(seeded, suffix=str(uuid.uuid4())[:8])
    created = cookbook_service.create_recipe(seeded, owner_id=owner.id, data=_simple_recipe_in())

    # Simulate a legacy row: null out cookbook_id directly (bypassing create_recipe).
    recipe = seeded.get(Recipe, created.id)
    assert recipe is not None
    recipe.cookbook_id = None
    seeded.flush()

    # Owner still has full access.
    fetched = cookbook_service.get_recipe(seeded, owner_id=owner.id, recipe_id=created.id)
    assert fetched.id == created.id
    updated = cookbook_service.update_recipe(
        seeded,
        owner_id=owner.id,
        recipe_id=created.id,
        data=_simple_recipe_in(title="Still Mine"),
        editor_id=owner.id,
    )
    assert updated.title == "Still Mine"

    # Non-owner gets 404 on every operation.
    with pytest.raises(ApiError) as exc_info:
        cookbook_service.get_recipe(seeded, owner_id=other.id, recipe_id=created.id)
    assert exc_info.value.status_code == 404

    with pytest.raises(ApiError) as exc_info:
        cookbook_service.update_recipe(
            seeded,
            owner_id=other.id,
            recipe_id=created.id,
            data=_simple_recipe_in(title="Stolen"),
            editor_id=other.id,
        )
    assert exc_info.value.status_code == 404

    cookbook_service.delete_recipe(seeded, owner_id=owner.id, recipe_id=created.id)
    assert seeded.get(Recipe, created.id) is None
