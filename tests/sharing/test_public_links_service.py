"""Service-level tests for sharing.service public links.

Covers create/idempotency/list/revoke, token resolution (unknown vs revoked
both 404, indistinguishable), the scaled-endpoint reuse path, and — the
critical bit — a leak audit walking the full JSON payload of
``PublicRecipeOut`` for anything that shouldn't be there.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from recipe_normalizer.cookbook import service as cookbook_service
from recipe_normalizer.cookbook.models import Recipe
from recipe_normalizer.cookbook.schemas import IngredientGroupIn, IngredientLineIn, RecipeIn
from recipe_normalizer.errors import ApiError
from recipe_normalizer.filestore import LocalFileStore
from recipe_normalizer.sharing import service as sharing_service
from recipe_normalizer.sharing.models import PublicLink
from recipe_normalizer.users.models import User

_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"fake but sniffable png data"

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


def _recipe_in(title: str = "Test Cake") -> RecipeIn:
    return RecipeIn(
        title=title,
        servings=None,
        groups=[
            IngredientGroupIn(
                name="Main",
                lines=[IngredientLineIn(original_text="2 cups flour", quantity=2, unit="cup")],
            )
        ],
    )


@pytest.fixture()
def owner(db_session: Session) -> User:
    return make_user(db_session, suffix=str(uuid.uuid4())[:8])


# ---------------------------------------------------------------------------
# create_public_link
# ---------------------------------------------------------------------------


def test_create_public_link_happy_path(db_session: Session, owner: User) -> None:
    recipe = cookbook_service.create_recipe(db_session, owner_id=owner.id, data=_recipe_in())

    out = sharing_service.create_public_link(db_session, owner_id=owner.id, recipe_id=recipe.id)

    assert out.recipe_id == recipe.id
    assert out.recipe_title == recipe.title
    assert out.revoked_at is None
    assert len(out.token) >= 32  # secrets.token_urlsafe(24) -> 32 chars


def test_create_public_link_is_idempotent_for_unrevoked_link(
    db_session: Session, owner: User
) -> None:
    recipe = cookbook_service.create_recipe(db_session, owner_id=owner.id, data=_recipe_in())

    first = sharing_service.create_public_link(db_session, owner_id=owner.id, recipe_id=recipe.id)
    second = sharing_service.create_public_link(db_session, owner_id=owner.id, recipe_id=recipe.id)

    assert first.id == second.id
    assert first.token == second.token

    rows = db_session.scalars(select(PublicLink).where(PublicLink.recipe_id == recipe.id)).all()
    assert len(rows) == 1


def test_create_public_link_after_revoke_mints_a_new_one(db_session: Session, owner: User) -> None:
    recipe = cookbook_service.create_recipe(db_session, owner_id=owner.id, data=_recipe_in())

    first = sharing_service.create_public_link(db_session, owner_id=owner.id, recipe_id=recipe.id)
    sharing_service.revoke_public_link(db_session, owner_id=owner.id, link_id=first.id)
    second = sharing_service.create_public_link(db_session, owner_id=owner.id, recipe_id=recipe.id)

    assert second.id != first.id
    assert second.token != first.token


def test_create_public_link_non_owner_raises_404(db_session: Session, owner: User) -> None:
    other = make_user(db_session, suffix=str(uuid.uuid4())[:8])
    recipe = cookbook_service.create_recipe(db_session, owner_id=owner.id, data=_recipe_in())

    with pytest.raises(ApiError) as exc_info:
        sharing_service.create_public_link(db_session, owner_id=other.id, recipe_id=recipe.id)
    assert exc_info.value.status_code == 404


def test_create_public_link_missing_recipe_raises_404(db_session: Session, owner: User) -> None:
    with pytest.raises(ApiError) as exc_info:
        sharing_service.create_public_link(db_session, owner_id=owner.id, recipe_id=uuid.uuid4())
    assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# list_public_links / revoke_public_link
# ---------------------------------------------------------------------------


def test_list_public_links_is_owner_scoped(db_session: Session, owner: User) -> None:
    other = make_user(db_session, suffix=str(uuid.uuid4())[:8])
    mine = cookbook_service.create_recipe(db_session, owner_id=owner.id, data=_recipe_in("Mine"))
    theirs = cookbook_service.create_recipe(
        db_session, owner_id=other.id, data=_recipe_in("Theirs")
    )
    sharing_service.create_public_link(db_session, owner_id=owner.id, recipe_id=mine.id)
    sharing_service.create_public_link(db_session, owner_id=other.id, recipe_id=theirs.id)

    mine_links = sharing_service.list_public_links(db_session, owner_id=owner.id)
    assert len(mine_links) == 1
    assert mine_links[0].recipe_id == mine.id


def test_revoke_public_link_owner_scoped_404(db_session: Session, owner: User) -> None:
    other = make_user(db_session, suffix=str(uuid.uuid4())[:8])
    recipe = cookbook_service.create_recipe(db_session, owner_id=owner.id, data=_recipe_in())
    link = sharing_service.create_public_link(db_session, owner_id=owner.id, recipe_id=recipe.id)

    with pytest.raises(ApiError) as exc_info:
        sharing_service.revoke_public_link(db_session, owner_id=other.id, link_id=link.id)
    assert exc_info.value.status_code == 404

    # Never actually revoked by the failed attempt.
    row = db_session.get(PublicLink, link.id)
    assert row is not None
    assert row.revoked_at is None


def test_revoke_public_link_is_idempotent(db_session: Session, owner: User) -> None:
    recipe = cookbook_service.create_recipe(db_session, owner_id=owner.id, data=_recipe_in())
    link = sharing_service.create_public_link(db_session, owner_id=owner.id, recipe_id=recipe.id)

    sharing_service.revoke_public_link(db_session, owner_id=owner.id, link_id=link.id)
    row = db_session.get(PublicLink, link.id)
    assert row is not None
    first_revoked_at = row.revoked_at
    assert first_revoked_at is not None

    # Revoking again doesn't error and doesn't bump the timestamp.
    sharing_service.revoke_public_link(db_session, owner_id=owner.id, link_id=link.id)
    db_session.refresh(row)
    assert row.revoked_at == first_revoked_at


# ---------------------------------------------------------------------------
# get_public_recipe: unknown vs. revoked, both 404, indistinguishable
# ---------------------------------------------------------------------------


def test_get_public_recipe_unknown_token_raises_404(db_session: Session) -> None:
    with pytest.raises(ApiError) as exc_info:
        sharing_service.get_public_recipe(db_session, token="does-not-exist")
    assert exc_info.value.status_code == 404


def test_get_public_recipe_revoked_token_raises_404_identical_to_unknown(
    db_session: Session, owner: User
) -> None:
    recipe = cookbook_service.create_recipe(db_session, owner_id=owner.id, data=_recipe_in())
    link = sharing_service.create_public_link(db_session, owner_id=owner.id, recipe_id=recipe.id)
    sharing_service.revoke_public_link(db_session, owner_id=owner.id, link_id=link.id)

    with pytest.raises(ApiError) as revoked_exc:
        sharing_service.get_public_recipe(db_session, token=link.token)
    with pytest.raises(ApiError) as unknown_exc:
        sharing_service.get_public_recipe(db_session, token="totally-bogus-token")

    assert revoked_exc.value.status_code == unknown_exc.value.status_code == 404
    assert revoked_exc.value.code == unknown_exc.value.code
    assert revoked_exc.value.message == unknown_exc.value.message


def test_get_public_recipe_happy_path(db_session: Session, owner: User) -> None:
    recipe = cookbook_service.create_recipe(db_session, owner_id=owner.id, data=_recipe_in())
    link = sharing_service.create_public_link(db_session, owner_id=owner.id, recipe_id=recipe.id)

    public = sharing_service.get_public_recipe(db_session, token=link.token)
    assert public.id == recipe.id
    assert public.title == recipe.title


# ---------------------------------------------------------------------------
# Leak audit — walk the full JSON payload
# ---------------------------------------------------------------------------

_FORBIDDEN_KEYS = {
    "notes",
    "is_favorite",
    "extraction_meta",
    "provenance",
    "fingerprint",
    "source_fingerprint",
}


def _walk(value: Any) -> list[Any]:
    """Flatten every scalar leaf and every dict key found anywhere in *value*."""
    found: list[Any] = []
    if isinstance(value, dict):
        for k, v in value.items():
            found.append(k)
            found.extend(_walk(v))
    elif isinstance(value, list):
        for item in value:
            found.extend(_walk(item))
    else:
        found.append(value)
    return found


def test_public_recipe_payload_leak_audit(db_session: Session) -> None:
    """The public payload must carry no PII, no personal data, no internals.

    Sets up the worst case: a recipe that actually HAS notes, is_favorite,
    extraction_meta, and (via a real copy-on-share) provenance containing the
    sharer's email — then asserts every one of those is absent from the
    serialized public payload, and that no value anywhere looks like an email
    address.
    """
    sharer = make_user(db_session, suffix=str(uuid.uuid4())[:8])
    recipient = make_user(db_session, suffix=str(uuid.uuid4())[:8])

    origin = cookbook_service.create_recipe(
        db_session,
        owner_id=sharer.id,
        data=_recipe_in("Leaky Cake"),
        extraction_meta={"tier_used": 2, "actions_log": ["scrolled", "clicked"]},
    )

    shared = sharing_service.share_recipe(
        db_session,
        from_user_id=sharer.id,
        from_email=sharer.email,
        recipe_id=origin.id,
        to_email=recipient.email,
    )
    copy_id = shared.copied_recipe_id

    # copy_recipe deliberately drops extraction_meta on the copy (it
    # references the sharer's ingestion job, meaningless to the recipient —
    # see cookbook.service.copy_recipe's docstring). Stamp it directly on the
    # copy's row so this test still covers the worst case: a recipe that has
    # extraction_meta AND is reachable via a public link.
    copy_row = db_session.get(Recipe, copy_id)
    assert copy_row is not None
    copy_row.extraction_meta = {"tier_used": 2, "actions_log": ["scrolled", "clicked"]}
    db_session.flush()

    # The recipient marks it a favorite and adds kitchen notes — personal data
    # that must never reach a stranger.
    cookbook_service.set_personal(
        db_session,
        owner_id=recipient.id,
        recipe_id=copy_id,
        is_favorite=True,
        notes="Secret family notes — do not share!",
    )

    # Sanity: the copy really does carry all this sensitive data before we
    # assert it's excluded — otherwise this test would pass vacuously.
    full = cookbook_service.get_recipe(db_session, owner_id=recipient.id, recipe_id=copy_id)
    assert full.notes is not None
    assert full.is_favorite is True
    assert full.extraction_meta is not None
    assert full.provenance is not None
    assert full.provenance["shared_by"] == sharer.email

    link = sharing_service.create_public_link(db_session, owner_id=recipient.id, recipe_id=copy_id)
    public = sharing_service.get_public_recipe(db_session, token=link.token)

    payload = public.model_dump(mode="json")

    leaves = _walk(payload)
    leak_keys_present = _FORBIDDEN_KEYS & {leaf for leaf in leaves if isinstance(leaf, str)}
    assert not leak_keys_present, f"forbidden keys leaked into public payload: {leak_keys_present}"

    # No value anywhere (dict key or scalar leaf) may contain either email
    # address, nor generically look like an email address at all.
    for leaf in leaves:
        if isinstance(leaf, str):
            assert sharer.email not in leaf
            assert recipient.email not in leaf
            assert "@" not in leaf, f"value looks like it could contain an email: {leaf!r}"

    # Belt-and-suspenders: explicit attribute-level assertions too.
    assert not hasattr(public, "notes")
    assert not hasattr(public, "is_favorite")
    assert not hasattr(public, "extraction_meta")
    assert not hasattr(public, "provenance")


# ---------------------------------------------------------------------------
# Scaled endpoint reuse
# ---------------------------------------------------------------------------


def test_get_public_recipe_for_scaling_matches_owner_scoped_recipe(
    db_session: Session, owner: User
) -> None:
    recipe = cookbook_service.create_recipe(db_session, owner_id=owner.id, data=_recipe_in())
    link = sharing_service.create_public_link(db_session, owner_id=owner.id, recipe_id=recipe.id)

    for_scaling = sharing_service.get_public_recipe_for_scaling(db_session, token=link.token)
    owner_scoped = cookbook_service.get_recipe(db_session, owner_id=owner.id, recipe_id=recipe.id)

    assert for_scaling.model_dump() == owner_scoped.model_dump()


# ---------------------------------------------------------------------------
# image_url substitution on the public payload
# ---------------------------------------------------------------------------


def test_get_public_recipe_image_url_is_none_without_an_image(
    db_session: Session, owner: User
) -> None:
    recipe = cookbook_service.create_recipe(db_session, owner_id=owner.id, data=_recipe_in())
    link = sharing_service.create_public_link(db_session, owner_id=owner.id, recipe_id=recipe.id)

    public = sharing_service.get_public_recipe(db_session, token=link.token)
    assert public.image_url is None
    assert not hasattr(public, "image_ref")


def test_get_public_recipe_image_url_points_at_token_scoped_route(
    db_session: Session, owner: User, tmp_path: Any
) -> None:
    recipe = cookbook_service.create_recipe(db_session, owner_id=owner.id, data=_recipe_in())
    store = LocalFileStore(tmp_path)
    cookbook_service.set_recipe_image(
        db_session, owner_id=owner.id, recipe_id=recipe.id, data=_PNG_BYTES, store=store
    )
    link = sharing_service.create_public_link(db_session, owner_id=owner.id, recipe_id=recipe.id)

    public = sharing_service.get_public_recipe(db_session, token=link.token)
    assert public.image_url == f"/api/public/{link.token}/image"


# ---------------------------------------------------------------------------
# get_public_recipe_image_ref
# ---------------------------------------------------------------------------


def test_get_public_recipe_image_ref_happy_path(
    db_session: Session, owner: User, tmp_path: Any
) -> None:
    recipe = cookbook_service.create_recipe(db_session, owner_id=owner.id, data=_recipe_in())
    store = LocalFileStore(tmp_path)
    updated = cookbook_service.set_recipe_image(
        db_session, owner_id=owner.id, recipe_id=recipe.id, data=_PNG_BYTES, store=store
    )
    link = sharing_service.create_public_link(db_session, owner_id=owner.id, recipe_id=recipe.id)

    ref = sharing_service.get_public_recipe_image_ref(db_session, token=link.token)
    # updated.image_ref is the /api/files/ URL (RecipeOut's validator); the
    # raw ref is what's actually stored on the row.
    row = db_session.get(Recipe, recipe.id)
    assert row is not None
    assert ref == row.image_ref
    assert updated.image_ref == f"/api/files/{ref}"


def test_get_public_recipe_image_ref_no_image_raises_404(db_session: Session, owner: User) -> None:
    recipe = cookbook_service.create_recipe(db_session, owner_id=owner.id, data=_recipe_in())
    link = sharing_service.create_public_link(db_session, owner_id=owner.id, recipe_id=recipe.id)

    with pytest.raises(ApiError) as exc_info:
        sharing_service.get_public_recipe_image_ref(db_session, token=link.token)
    assert exc_info.value.status_code == 404


def test_get_public_recipe_image_ref_unknown_token_raises_404(db_session: Session) -> None:
    with pytest.raises(ApiError) as exc_info:
        sharing_service.get_public_recipe_image_ref(db_session, token="bogus-token")
    assert exc_info.value.status_code == 404


def test_get_public_recipe_image_ref_revoked_token_raises_404(
    db_session: Session, owner: User, tmp_path: Any
) -> None:
    recipe = cookbook_service.create_recipe(db_session, owner_id=owner.id, data=_recipe_in())
    store = LocalFileStore(tmp_path)
    cookbook_service.set_recipe_image(
        db_session, owner_id=owner.id, recipe_id=recipe.id, data=_PNG_BYTES, store=store
    )
    link = sharing_service.create_public_link(db_session, owner_id=owner.id, recipe_id=recipe.id)
    sharing_service.revoke_public_link(db_session, owner_id=owner.id, link_id=link.id)

    with pytest.raises(ApiError) as exc_info:
        sharing_service.get_public_recipe_image_ref(db_session, token=link.token)
    assert exc_info.value.status_code == 404


def test_get_public_recipe_image_ref_never_leaks_a_different_recipes_ref(
    db_session: Session, owner: User, tmp_path: Any
) -> None:
    """Two recipes, two tokens: each token resolves to its OWN ref only."""
    store = LocalFileStore(tmp_path)
    recipe_a = cookbook_service.create_recipe(
        db_session, owner_id=owner.id, data=_recipe_in("Recipe A")
    )
    cookbook_service.set_recipe_image(
        db_session, owner_id=owner.id, recipe_id=recipe_a.id, data=_PNG_BYTES, store=store
    )
    link_a = sharing_service.create_public_link(
        db_session, owner_id=owner.id, recipe_id=recipe_a.id
    )

    other_png = b"\x89PNG\r\n\x1a\n" + b"a different fake png payload"
    recipe_b = cookbook_service.create_recipe(
        db_session, owner_id=owner.id, data=_recipe_in("Recipe B")
    )
    cookbook_service.set_recipe_image(
        db_session, owner_id=owner.id, recipe_id=recipe_b.id, data=other_png, store=store
    )
    link_b = sharing_service.create_public_link(
        db_session, owner_id=owner.id, recipe_id=recipe_b.id
    )

    ref_a = sharing_service.get_public_recipe_image_ref(db_session, token=link_a.token)
    ref_b = sharing_service.get_public_recipe_image_ref(db_session, token=link_b.token)

    assert ref_a != ref_b
    assert store.open(ref_a) == _PNG_BYTES
    assert store.open(ref_b) == other_png
