"""Model-level tests for the Cookbook and CookbookMember entities."""

from __future__ import annotations

from sqlalchemy.orm import Session

from recipe_normalizer.cookbook.models import (
    Cookbook,
    CookbookMember,
    CookbookRecipe,
    CookbookRole,
    CookbookVisibility,
    Recipe,
    SourceType,
)
from recipe_normalizer.users.models import User


def make_user(db: Session, suffix: str = "") -> User:
    user = User(
        email=f"chef{suffix}@example.com",
        password_hash="hash",
        display_name=f"Chef{suffix}",
    )
    db.add(user)
    db.flush()
    return user


def test_cookbook_defaults_and_persistence(db_session: Session) -> None:
    owner = make_user(db_session, "1")
    cookbook = Cookbook(owner_id=owner.id, name="Family Favorites")
    db_session.add(cookbook)
    db_session.flush()

    db_session.expire_all()
    fetched = db_session.get(Cookbook, cookbook.id)
    assert fetched is not None
    assert fetched.owner_id == owner.id
    assert fetched.name == "Family Favorites"
    assert fetched.description is None
    assert fetched.cover_image_ref is None
    assert fetched.visibility == CookbookVisibility.private
    assert fetched.visibility.value == "private"
    assert fetched.public_token is None
    assert fetched.is_default is False
    assert fetched.created_at is not None
    assert fetched.updated_at is not None


def test_cookbook_member_defaults_and_persistence(db_session: Session) -> None:
    owner = make_user(db_session, "2")
    member_user = make_user(db_session, "3")
    cookbook = Cookbook(owner_id=owner.id, name="Shared Cookbook")
    db_session.add(cookbook)
    db_session.flush()

    member = CookbookMember(
        cookbook_id=cookbook.id,
        user_id=member_user.id,
        role=CookbookRole.editor,
        added_by=owner.id,
    )
    db_session.add(member)
    db_session.flush()

    db_session.expire_all()
    fetched = db_session.get(CookbookMember, (cookbook.id, member_user.id))
    assert fetched is not None
    assert fetched.cookbook_id == cookbook.id
    assert fetched.user_id == member_user.id
    assert fetched.role == CookbookRole.editor
    assert fetched.role.value == "editor"
    assert fetched.added_by == owner.id
    assert fetched.created_at is not None


def test_recipe_is_linked_to_a_cookbook_through_the_join(db_session: Session) -> None:
    """Containment is the `cookbook_recipes` join, both ways, and nothing else.

    Replaces the old `recipes.cookbook_id` / `Cookbook.recipes` round-trip: the
    column and that relationship are gone as of migration a3f7c2d8e015, so a
    recipe reaches its cookbooks via `cookbook_placements` and a cookbook
    reaches its recipes via `recipe_placements`.
    """
    owner = make_user(db_session, "4")
    cookbook = Cookbook(owner_id=owner.id, name="Weeknight Dinners")
    db_session.add(cookbook)
    db_session.flush()

    recipe = Recipe(
        owner_id=owner.id,
        title="Weeknight Pasta",
        source_type=SourceType.manual,
    )
    db_session.add(recipe)
    db_session.flush()
    db_session.add(CookbookRecipe(cookbook_id=cookbook.id, recipe_id=recipe.id, added_by=owner.id))
    db_session.flush()

    db_session.expire_all()
    fetched_recipe = db_session.get(Recipe, recipe.id)
    assert fetched_recipe is not None
    assert [p.cookbook_id for p in fetched_recipe.cookbook_placements] == [cookbook.id]

    fetched_cookbook = db_session.get(Cookbook, cookbook.id)
    assert fetched_cookbook is not None
    assert [p.recipe_id for p in fetched_cookbook.recipe_placements] == [recipe.id]


def test_recipe_needs_no_cookbook_at_all(db_session: Session) -> None:
    """A `Recipe` row is writable with zero placements — no `cookbook_id` exists.

    The inverse of the Phase-1 test this replaces, which asserted an
    IntegrityError for a cookbook-less recipe (`recipes.cookbook_id` was NOT
    NULL). Migration a3f7c2d8e015 dropped the column, so containment can no
    longer be a per-row DB constraint: the ">= 1 placement" invariant is
    enforced in `cookbook.service` (every create/copy path writes a join row,
    `remove_recipe_from_cookbook` refuses the last one, and `delete_cookbook`
    rescues a recipe placed only there).
    """
    owner = make_user(db_session, "5")
    recipe = Recipe(
        owner_id=owner.id,
        title="Unplaced Recipe",
        source_type=SourceType.manual,
    )
    db_session.add(recipe)
    db_session.flush()  # no error

    db_session.expire_all()
    fetched = db_session.get(Recipe, recipe.id)
    assert fetched is not None
    assert fetched.cookbook_placements == []
    assert not hasattr(fetched, "cookbook_id")


def test_cookbook_visibility_enum_values() -> None:
    assert CookbookVisibility.private.value == "private"
    assert CookbookVisibility.unlisted.value == "unlisted"
    assert CookbookVisibility.public.value == "public"


def test_cookbook_role_enum_values() -> None:
    assert CookbookRole.editor.value == "editor"
    assert CookbookRole.viewer.value == "viewer"
