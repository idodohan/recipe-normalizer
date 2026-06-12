"""Tests for cookbook models: Recipe aggregate with groups, lines, steps, vocab."""

import uuid

from sqlalchemy import select
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
    # user survives
    assert db_session.get(User, owner_id) is not None
