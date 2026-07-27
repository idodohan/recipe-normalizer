"""Model-level tests for the Cookbook and CookbookMember entities."""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from recipe_normalizer.cookbook.models import (
    Cookbook,
    CookbookMember,
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


def test_recipe_links_to_cookbook(db_session: Session) -> None:
    owner = make_user(db_session, "4")
    cookbook = Cookbook(owner_id=owner.id, name="Weeknight Dinners")
    db_session.add(cookbook)
    db_session.flush()

    recipe = Recipe(
        owner_id=owner.id,
        title="Weeknight Pasta",
        source_type=SourceType.manual,
        cookbook_id=cookbook.id,
    )
    db_session.add(recipe)
    db_session.flush()

    db_session.expire_all()
    fetched_recipe = db_session.get(Recipe, recipe.id)
    assert fetched_recipe is not None
    assert fetched_recipe.cookbook_id == cookbook.id
    assert fetched_recipe.cookbook is not None
    assert fetched_recipe.cookbook.id == cookbook.id

    fetched_cookbook = db_session.get(Cookbook, cookbook.id)
    assert fetched_cookbook is not None
    assert len(fetched_cookbook.recipes) == 1
    assert fetched_cookbook.recipes[0].id == recipe.id


def test_recipe_cookbook_id_is_not_nullable(db_session: Session) -> None:
    """Exactly-one containment is a DB constraint, not a convention.

    Inverted from its original form: `cookbook_id` was nullable for the
    duration of the pivot (Tasks 2-8) so every intermediate state stayed
    runnable. The finalize migration (b7d3f0c11a94) flipped it, and this
    pins that a cookbook-less recipe can no longer be written at all —
    which is what lets `_recipe_access` derive access from the cookbook
    with no fallback branch.
    """
    owner = make_user(db_session, "5")
    recipe = Recipe(
        owner_id=owner.id,
        title="Unlinked Recipe",
        source_type=SourceType.manual,
    )
    db_session.add(recipe)

    with pytest.raises(IntegrityError), db_session.begin_nested():
        db_session.flush()


def test_cookbook_visibility_enum_values() -> None:
    assert CookbookVisibility.private.value == "private"
    assert CookbookVisibility.unlisted.value == "unlisted"
    assert CookbookVisibility.public.value == "public"


def test_cookbook_role_enum_values() -> None:
    assert CookbookRole.editor.value == "editor"
    assert CookbookRole.viewer.value == "viewer"
