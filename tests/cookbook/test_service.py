"""Service-level tests for cookbook CRUD, catalog matching, and dedupe."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.orm import Session

from recipe_normalizer.catalog import service as catalog_service
from recipe_normalizer.catalog.seed_loader import load_seed
from recipe_normalizer.cookbook import service as cookbook_service
from recipe_normalizer.cookbook.models import IngredientLine, Recipe
from recipe_normalizer.cookbook.schemas import (
    IngredientGroupIn,
    IngredientLineIn,
    RecipeIn,
    StepIn,
)
from recipe_normalizer.cookbook.service import DuplicateRecipeError
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
    assert line.display == "1 cup flour → ~120 g (approx.)"

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
    summaries = cookbook_service.list_recipes(seeded, owner_id=owner.id)
    assert len(summaries) == 3
    # newest first — created_at descending
    titles = [s.title for s in summaries]
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
    summaries = cookbook_service.list_recipes(seeded, owner_id=owner.id)
    assert len(summaries) == 1


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
