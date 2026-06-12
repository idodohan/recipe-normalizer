"""Tests for cookbook models: Recipe aggregate with groups, lines, steps, vocab."""

import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from recipe_normalizer.users.models import User


def make_user(db_session: Session, suffix: str = "") -> User:
    user = User(
        email=f"chef{suffix}@example.com",
        password_hash="hash",
        display_name=f"Chef{suffix}",
    )
    db_session.add(user)
    db_session.flush()
    return user


def test_recipe_aggregate_roundtrip(db_session: Session) -> None:
    """Build full Recipe aggregate, flush, reload via fresh query, assert order/content."""
    from recipe_normalizer.cookbook.models import (
        Cuisine,
        DishType,
        IngredientGroup,
        IngredientLine,
        Recipe,
        SourceType,
        Step,
        Tag,
    )

    owner = make_user(db_session, suffix=str(uuid.uuid4())[:8])

    # --- build vocab rows ---
    cuisine = Cuisine(name="Italian")
    dish_type = DishType(name="main")
    tag = Tag(name="comfort food")
    db_session.add_all([cuisine, dish_type, tag])
    db_session.flush()

    # --- build recipe ---
    recipe = Recipe(
        owner_id=owner.id,
        title="Pizza Margherita",
        source_type=SourceType.manual,
    )
    db_session.add(recipe)
    db_session.flush()

    # --- ingredient groups ---
    group0 = IngredientGroup(recipe_id=recipe.id, name="For the dough", order_index=0)
    group1 = IngredientGroup(recipe_id=recipe.id, name=None, order_index=1)
    db_session.add_all([group0, group1])
    db_session.flush()

    # --- ingredient lines ---
    line0 = IngredientLine(group_id=group0.id, order_index=0, original_text="500g flour")
    line1 = IngredientLine(group_id=group0.id, order_index=1, original_text="1 tsp salt")
    line2 = IngredientLine(group_id=group1.id, order_index=0, original_text="200ml tomato sauce")
    db_session.add_all([line0, line1, line2])
    db_session.flush()

    # --- steps ---
    step0 = Step(recipe_id=recipe.id, order_index=0, original_text="Mix flour and salt.")
    step1 = Step(recipe_id=recipe.id, order_index=1, original_text="Top with tomato sauce.")
    db_session.add_all([step0, step1])
    db_session.flush()

    # --- attach vocab via many-to-many ---
    recipe.cuisines.append(cuisine)
    recipe.dish_types.append(dish_type)
    recipe.tags.append(tag)
    db_session.flush()

    recipe_id = recipe.id

    # --- reload via fresh query ---
    db_session.expire_all()
    loaded = db_session.get(Recipe, recipe_id)
    assert loaded is not None

    # group order preserved
    groups = loaded.ingredient_groups
    assert len(groups) == 2
    assert groups[0].order_index == 0
    assert groups[0].name == "For the dough"
    assert groups[1].order_index == 1
    assert groups[1].name is None

    # line order within each group
    lines_g0 = groups[0].ingredient_lines
    assert len(lines_g0) == 2
    assert lines_g0[0].order_index == 0
    assert lines_g0[0].original_text == "500g flour"
    assert lines_g0[1].order_index == 1

    lines_g1 = groups[1].ingredient_lines
    assert len(lines_g1) == 1
    assert lines_g1[0].original_text == "200ml tomato sauce"

    # step order
    steps = loaded.steps
    assert len(steps) == 2
    assert steps[0].order_index == 0
    assert steps[1].order_index == 1

    # vocab names round-trip
    assert loaded.cuisines[0].name == "Italian"
    assert loaded.dish_types[0].name == "main"
    assert loaded.tags[0].name == "comfort food"


def test_recipe_cascade_delete(db_session: Session) -> None:
    """Deleting a recipe cascades to groups/lines/steps but NOT vocab rows or the user."""
    from recipe_normalizer.cookbook.models import (
        Cuisine,
        DishType,
        IngredientGroup,
        IngredientLine,
        Recipe,
        SourceType,
        Step,
        Tag,
    )

    owner = make_user(db_session, suffix=str(uuid.uuid4())[:8])
    cuisine = Cuisine(name=f"TestCuisine-{uuid.uuid4()}")
    dish_type = DishType(name=f"TestDish-{uuid.uuid4()}")
    tag = Tag(name=f"TestTag-{uuid.uuid4()}")
    db_session.add_all([cuisine, dish_type, tag])
    db_session.flush()

    recipe = Recipe(owner_id=owner.id, title="Temp", source_type=SourceType.text)
    db_session.add(recipe)
    db_session.flush()

    group = IngredientGroup(recipe_id=recipe.id, name="Stuff", order_index=0)
    db_session.add(group)
    db_session.flush()

    line = IngredientLine(group_id=group.id, order_index=0, original_text="1 egg")
    step = Step(recipe_id=recipe.id, order_index=0, original_text="Beat egg.")
    db_session.add_all([line, step])

    recipe.cuisines.append(cuisine)
    recipe.dish_types.append(dish_type)
    recipe.tags.append(tag)
    db_session.flush()

    recipe_id = recipe.id
    group_id = group.id
    cuisine_id = cuisine.id
    dish_type_id = dish_type.id
    tag_id = tag.id
    owner_id = owner.id

    # delete the recipe
    db_session.delete(recipe)
    db_session.flush()

    # groups/lines/steps gone
    assert db_session.get(Recipe, recipe_id) is None
    assert db_session.get(IngredientGroup, group_id) is None
    assert (
        db_session.execute(select(IngredientLine).where(IngredientLine.group_id == group_id))
        .scalars()
        .all()
        == []
    )
    assert db_session.execute(select(Step).where(Step.recipe_id == recipe_id)).scalars().all() == []

    # vocab rows survive
    assert db_session.get(Cuisine, cuisine_id) is not None
    assert db_session.get(DishType, dish_type_id) is not None
    assert db_session.get(Tag, tag_id) is not None
    # user survives
    assert db_session.get(User, owner_id) is not None


def test_owner_fingerprint_partial_unique_index(db_session: Session) -> None:
    """Duplicate (owner_id, source_fingerprint) rejected; NULL fingerprints unrestricted."""
    from recipe_normalizer.cookbook.models import Recipe, SourceType

    owner = make_user(db_session, suffix=str(uuid.uuid4())[:8])

    first = Recipe(
        owner_id=owner.id,
        title="First",
        source_type=SourceType.web,
        source_fingerprint="fp-123",
    )
    db_session.add(first)
    db_session.flush()

    # same (owner_id, source_fingerprint) -> IntegrityError on flush.
    # Scope the rollback to a savepoint so the fixture's transaction survives.
    duplicate = Recipe(
        owner_id=owner.id,
        title="Duplicate",
        source_type=SourceType.web,
        source_fingerprint="fp-123",
    )
    with pytest.raises(IntegrityError), db_session.begin_nested():
        db_session.add(duplicate)
        db_session.flush()

    # two recipes with NULL fingerprints for the same owner -> allowed
    owner2 = make_user(db_session, suffix=str(uuid.uuid4())[:8])
    null_a = Recipe(owner_id=owner2.id, title="Null A", source_type=SourceType.manual)
    null_b = Recipe(owner_id=owner2.id, title="Null B", source_type=SourceType.manual)
    db_session.add_all([null_a, null_b])
    db_session.flush()
    count = db_session.execute(
        select(func.count()).select_from(Recipe).where(Recipe.owner_id == owner2.id)
    ).scalar_one()
    assert count == 2


def test_out_of_order_inserts_returned_sorted(db_session: Session) -> None:
    """Groups/lines inserted out of order_index order are returned sorted by the relationship."""
    from recipe_normalizer.cookbook.models import (
        IngredientGroup,
        IngredientLine,
        Recipe,
        SourceType,
    )

    owner = make_user(db_session, suffix=str(uuid.uuid4())[:8])
    recipe = Recipe(owner_id=owner.id, title="Unordered", source_type=SourceType.manual)
    db_session.add(recipe)
    db_session.flush()

    # insert group with order_index 1 BEFORE group 0
    group_later = IngredientGroup(recipe_id=recipe.id, name="Second", order_index=1)
    db_session.add(group_later)
    db_session.flush()
    group_first = IngredientGroup(recipe_id=recipe.id, name="First", order_index=0)
    db_session.add(group_first)
    db_session.flush()

    # insert line with order_index 1 BEFORE line 0 within the same group
    line_later = IngredientLine(group_id=group_first.id, order_index=1, original_text="second")
    db_session.add(line_later)
    db_session.flush()
    line_first = IngredientLine(group_id=group_first.id, order_index=0, original_text="first")
    db_session.add(line_first)
    db_session.flush()

    db_session.expire_all()
    loaded = db_session.get(Recipe, recipe.id)
    assert loaded is not None

    assert [g.order_index for g in loaded.ingredient_groups] == [0, 1]
    assert [g.name for g in loaded.ingredient_groups] == ["First", "Second"]

    lines = loaded.ingredient_groups[0].ingredient_lines
    assert [ln.order_index for ln in lines] == [0, 1]
    assert [ln.original_text for ln in lines] == ["first", "second"]
