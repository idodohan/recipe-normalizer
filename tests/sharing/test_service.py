"""Service-level tests for sharing.service.share_recipe (copy-on-share)."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from recipe_normalizer.cookbook import service as cookbook_service
from recipe_normalizer.cookbook.schemas import (
    IngredientGroupIn,
    IngredientLineIn,
    RecipeIn,
    RecipeOut,
)
from recipe_normalizer.errors import ApiError
from recipe_normalizer.sharing import service as sharing_service
from recipe_normalizer.sharing.models import Share
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


def _simple_recipe_in(title: str = "Test Cake") -> RecipeIn:
    return RecipeIn(
        title=title,
        groups=[IngredientGroupIn(name="Main", lines=[IngredientLineIn(original_text="salt")])],
    )


def _create_recipe(db: Session, owner_id: uuid.UUID, title: str = "Test Cake") -> RecipeOut:
    return cookbook_service.create_recipe(db, owner_id=owner_id, data=_simple_recipe_in(title))


@pytest.fixture()
def sharer(db_session: Session) -> User:
    return make_user(db_session, suffix=str(uuid.uuid4())[:8])


@pytest.fixture()
def recipient(db_session: Session) -> User:
    return make_user(db_session, suffix=str(uuid.uuid4())[:8])


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_share_recipe_creates_independent_copy_for_recipient(
    db_session: Session, sharer: User, recipient: User
) -> None:
    recipe = _create_recipe(db_session, sharer.id)

    out = sharing_service.share_recipe(
        db_session,
        from_user_id=sharer.id,
        from_email=sharer.email,
        recipe_id=recipe.id,
        to_email=recipient.email,
    )

    assert out.copied_recipe_id != recipe.id
    assert out.to_email == recipient.email
    assert out.id is not None
    assert out.created_at is not None

    # The recipient can see it as their own recipe immediately.
    copy = cookbook_service.get_recipe(
        db_session, owner_id=recipient.id, recipe_id=out.copied_recipe_id
    )
    assert copy.title == "Test Cake"

    # The sharer's original is untouched and still theirs alone.
    original = cookbook_service.get_recipe(db_session, owner_id=sharer.id, recipe_id=recipe.id)
    assert original.id == recipe.id
    with pytest.raises(ApiError):
        cookbook_service.get_recipe(db_session, owner_id=recipient.id, recipe_id=recipe.id)


def test_share_recipe_recipient_email_lookup_is_case_insensitive(
    db_session: Session, sharer: User, recipient: User
) -> None:
    recipe = _create_recipe(db_session, sharer.id)
    out = sharing_service.share_recipe(
        db_session,
        from_user_id=sharer.id,
        from_email=sharer.email,
        recipe_id=recipe.id,
        to_email=recipient.email.upper(),
    )
    assert out.to_email == recipient.email


def test_share_recipe_inserts_shares_row(
    db_session: Session, sharer: User, recipient: User
) -> None:
    recipe = _create_recipe(db_session, sharer.id)
    out = sharing_service.share_recipe(
        db_session,
        from_user_id=sharer.id,
        from_email=sharer.email,
        recipe_id=recipe.id,
        to_email=recipient.email,
    )

    row = db_session.scalars(select(Share).where(Share.id == out.id)).first()
    assert row is not None
    assert row.origin_recipe_id == recipe.id
    assert row.from_user_id == sharer.id
    assert row.to_user_id == recipient.id
    assert row.copied_recipe_id == out.copied_recipe_id


def test_share_recipe_provenance_content(
    db_session: Session, sharer: User, recipient: User
) -> None:
    recipe = _create_recipe(db_session, sharer.id)
    out = sharing_service.share_recipe(
        db_session,
        from_user_id=sharer.id,
        from_email=sharer.email,
        recipe_id=recipe.id,
        to_email=recipient.email,
    )
    copy = cookbook_service.get_recipe(
        db_session, owner_id=recipient.id, recipe_id=out.copied_recipe_id
    )
    assert copy.provenance is not None
    assert copy.provenance["shared_by"] == sharer.email
    assert copy.provenance["origin_recipe_id"] == str(recipe.id)
    # ISO-8601 UTC timestamp — just sanity-check it round-trips and carries a
    # UTC offset rather than asserting an exact value (nondeterministic).
    assert "T" in copy.provenance["shared_at"]
    assert copy.provenance["shared_at"].endswith("+00:00")


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


def test_share_recipe_self_share_raises_422(db_session: Session, sharer: User) -> None:
    recipe = _create_recipe(db_session, sharer.id)
    with pytest.raises(ApiError) as exc_info:
        sharing_service.share_recipe(
            db_session,
            from_user_id=sharer.id,
            from_email=sharer.email,
            recipe_id=recipe.id,
            to_email=sharer.email,
        )
    assert exc_info.value.status_code == 422


def test_share_recipe_unknown_email_raises_404_recipient_not_found(
    db_session: Session, sharer: User
) -> None:
    recipe = _create_recipe(db_session, sharer.id)
    with pytest.raises(ApiError) as exc_info:
        sharing_service.share_recipe(
            db_session,
            from_user_id=sharer.id,
            from_email=sharer.email,
            recipe_id=recipe.id,
            to_email="nobody@example.com",
        )
    assert exc_info.value.status_code == 404
    assert exc_info.value.code == "recipient_not_found"
    assert exc_info.value.message == "No account with that email."


def test_share_recipe_non_owner_raises_404(
    db_session: Session, sharer: User, recipient: User
) -> None:
    other = make_user(db_session, suffix=str(uuid.uuid4())[:8])
    recipe = _create_recipe(db_session, other.id)
    with pytest.raises(ApiError) as exc_info:
        sharing_service.share_recipe(
            db_session,
            from_user_id=sharer.id,
            from_email=sharer.email,
            recipe_id=recipe.id,
            to_email=recipient.email,
        )
    assert exc_info.value.status_code == 404


def test_share_recipe_missing_recipe_raises_404(
    db_session: Session, sharer: User, recipient: User
) -> None:
    with pytest.raises(ApiError) as exc_info:
        sharing_service.share_recipe(
            db_session,
            from_user_id=sharer.id,
            from_email=sharer.email,
            recipe_id=uuid.uuid4(),
            to_email=recipient.email,
        )
    assert exc_info.value.status_code == 404
