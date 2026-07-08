"""Service-level tests for shared cookbooks: CRUD, membership, and — the
critical deliverable — the recipe-access authorization matrix that the
membership-checker hook widens cookbook.service to support.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from recipe_normalizer.cookbook import service as cookbook_service
from recipe_normalizer.cookbook.schemas import IngredientGroupIn, IngredientLineIn, RecipeIn
from recipe_normalizer.errors import ApiError
from recipe_normalizer.sharing import service as sharing_service
from recipe_normalizer.users.models import User

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_user(db: Session, name: str = "Chef") -> User:
    user = User(
        email=f"{name.lower()}-{uuid.uuid4().hex[:8]}@example.com",
        password_hash="hash",
        display_name=name,
    )
    db.add(user)
    db.flush()
    return user


def _recipe_in(title: str = "Test Cake") -> RecipeIn:
    return RecipeIn(
        title=title,
        groups=[IngredientGroupIn(name="Main", lines=[IngredientLineIn(original_text="salt")])],
    )


def _create_recipe(db: Session, owner_id: uuid.UUID, title: str = "Test Cake") -> uuid.UUID:
    return cookbook_service.create_recipe(db, owner_id=owner_id, data=_recipe_in(title)).id


@pytest.fixture()
def alice(db_session: Session) -> User:
    return make_user(db_session, "Alice")


@pytest.fixture()
def bob(db_session: Session) -> User:
    return make_user(db_session, "Bob")


@pytest.fixture()
def carol(db_session: Session) -> User:
    return make_user(db_session, "Carol")


# ---------------------------------------------------------------------------
# create_shared_cookbook / list_my_shared_cookbooks
# ---------------------------------------------------------------------------


def test_create_shared_cookbook_auto_inserts_creator_as_member(
    db_session: Session, alice: User
) -> None:
    out = sharing_service.create_shared_cookbook(db_session, creator_id=alice.id, name="Sundays")
    assert out.name == "Sundays"
    assert out.created_by == alice.id
    assert out.member_count == 1
    assert out.recipe_count == 0

    detail = sharing_service.get_shared_cookbook(db_session, user_id=alice.id, cookbook_id=out.id)
    assert len(detail.members) == 1
    assert detail.members[0].user_id == alice.id
    assert detail.members[0].is_creator is True
    assert detail.members[0].display_name == "Alice"


def test_list_my_shared_cookbooks_only_shows_cookbooks_im_a_member_of(
    db_session: Session, alice: User, bob: User
) -> None:
    mine = sharing_service.create_shared_cookbook(db_session, creator_id=alice.id, name="Mine")
    sharing_service.create_shared_cookbook(db_session, creator_id=bob.id, name="Not mine")

    listed = sharing_service.list_my_shared_cookbooks(db_session, user_id=alice.id)
    assert [c.id for c in listed] == [mine.id]


def test_list_my_shared_cookbooks_counts(db_session: Session, alice: User, bob: User) -> None:
    cb = sharing_service.create_shared_cookbook(db_session, creator_id=alice.id, name="Cookbook")
    sharing_service.invite_member(db_session, user_id=alice.id, cookbook_id=cb.id, email=bob.email)
    recipe_id = _create_recipe(db_session, alice.id)
    sharing_service.add_recipe_to_shared_cookbook(
        db_session, user_id=alice.id, cookbook_id=cb.id, recipe_id=recipe_id
    )

    listed = sharing_service.list_my_shared_cookbooks(db_session, user_id=alice.id)
    assert listed[0].member_count == 2
    assert listed[0].recipe_count == 1


# ---------------------------------------------------------------------------
# get_shared_cookbook — member-gated 404, detail shape
# ---------------------------------------------------------------------------


def test_get_shared_cookbook_non_member_raises_404(
    db_session: Session, alice: User, bob: User
) -> None:
    cb = sharing_service.create_shared_cookbook(db_session, creator_id=alice.id, name="Cookbook")
    with pytest.raises(ApiError) as exc_info:
        sharing_service.get_shared_cookbook(db_session, user_id=bob.id, cookbook_id=cb.id)
    assert exc_info.value.status_code == 404


def test_get_shared_cookbook_unknown_id_raises_404(db_session: Session, alice: User) -> None:
    with pytest.raises(ApiError) as exc_info:
        sharing_service.get_shared_cookbook(db_session, user_id=alice.id, cookbook_id=uuid.uuid4())
    assert exc_info.value.status_code == 404


def test_get_shared_cookbook_recipe_shows_last_edited_by_display_name(
    db_session: Session, alice: User, bob: User
) -> None:
    cb = sharing_service.create_shared_cookbook(db_session, creator_id=alice.id, name="Cookbook")
    sharing_service.invite_member(db_session, user_id=alice.id, cookbook_id=cb.id, email=bob.email)
    recipe_id = _create_recipe(db_session, alice.id)
    sharing_service.add_recipe_to_shared_cookbook(
        db_session, user_id=alice.id, cookbook_id=cb.id, recipe_id=recipe_id
    )

    # Bob (a member, not the owner) edits the recipe via the widened update_recipe.
    cookbook_service.update_recipe(
        db_session,
        owner_id=bob.id,
        recipe_id=recipe_id,
        data=_recipe_in("Edited by Bob"),
        editor_id=bob.id,
    )

    detail = sharing_service.get_shared_cookbook(db_session, user_id=alice.id, cookbook_id=cb.id)
    assert len(detail.recipes) == 1
    row = detail.recipes[0]
    assert row.title == "Edited by Bob"
    assert row.last_edited_by == bob.id
    assert row.last_edited_by_name == "Bob"


def test_shared_cookbook_member_out_has_no_email_field(db_session: Session, alice: User) -> None:
    """DECISION: shared-cookbook members see display names only, never email."""
    cb = sharing_service.create_shared_cookbook(db_session, creator_id=alice.id, name="Cookbook")
    detail = sharing_service.get_shared_cookbook(db_session, user_id=alice.id, cookbook_id=cb.id)
    assert "email" not in detail.members[0].model_dump()


# ---------------------------------------------------------------------------
# invite_member
# ---------------------------------------------------------------------------


def test_invite_member_any_member_may_invite(
    db_session: Session, alice: User, bob: User, carol: User
) -> None:
    cb = sharing_service.create_shared_cookbook(db_session, creator_id=alice.id, name="Cookbook")
    sharing_service.invite_member(db_session, user_id=alice.id, cookbook_id=cb.id, email=bob.email)
    # Bob (not the creator) invites Carol — "any member may invite".
    out = sharing_service.invite_member(
        db_session, user_id=bob.id, cookbook_id=cb.id, email=carol.email
    )
    assert out.user_id == carol.id
    assert out.is_creator is False

    detail = sharing_service.get_shared_cookbook(db_session, user_id=alice.id, cookbook_id=cb.id)
    assert {m.user_id for m in detail.members} == {alice.id, bob.id, carol.id}


def test_invite_member_non_member_inviter_raises_404(
    db_session: Session, alice: User, bob: User, carol: User
) -> None:
    cb = sharing_service.create_shared_cookbook(db_session, creator_id=alice.id, name="Cookbook")
    with pytest.raises(ApiError) as exc_info:
        sharing_service.invite_member(
            db_session, user_id=bob.id, cookbook_id=cb.id, email=carol.email
        )
    assert exc_info.value.status_code == 404


def test_invite_member_unknown_email_raises_404_recipient_not_found(
    db_session: Session, alice: User
) -> None:
    cb = sharing_service.create_shared_cookbook(db_session, creator_id=alice.id, name="Cookbook")
    with pytest.raises(ApiError) as exc_info:
        sharing_service.invite_member(
            db_session, user_id=alice.id, cookbook_id=cb.id, email="nobody@example.com"
        )
    assert exc_info.value.status_code == 404
    assert exc_info.value.code == "recipient_not_found"


def test_invite_member_already_member_raises_409(
    db_session: Session, alice: User, bob: User
) -> None:
    cb = sharing_service.create_shared_cookbook(db_session, creator_id=alice.id, name="Cookbook")
    sharing_service.invite_member(db_session, user_id=alice.id, cookbook_id=cb.id, email=bob.email)
    with pytest.raises(ApiError) as exc_info:
        sharing_service.invite_member(
            db_session, user_id=alice.id, cookbook_id=cb.id, email=bob.email
        )
    assert exc_info.value.status_code == 409
    assert exc_info.value.code == "already_member"


# ---------------------------------------------------------------------------
# remove_member
# ---------------------------------------------------------------------------


def test_remove_member_self_leave(db_session: Session, alice: User, bob: User) -> None:
    cb = sharing_service.create_shared_cookbook(db_session, creator_id=alice.id, name="Cookbook")
    sharing_service.invite_member(db_session, user_id=alice.id, cookbook_id=cb.id, email=bob.email)

    sharing_service.remove_member(
        db_session, user_id=bob.id, cookbook_id=cb.id, target_user_id=bob.id
    )

    detail = sharing_service.get_shared_cookbook(db_session, user_id=alice.id, cookbook_id=cb.id)
    assert {m.user_id for m in detail.members} == {alice.id}


def test_remove_member_creator_removes_anyone(db_session: Session, alice: User, bob: User) -> None:
    cb = sharing_service.create_shared_cookbook(db_session, creator_id=alice.id, name="Cookbook")
    sharing_service.invite_member(db_session, user_id=alice.id, cookbook_id=cb.id, email=bob.email)

    sharing_service.remove_member(
        db_session, user_id=alice.id, cookbook_id=cb.id, target_user_id=bob.id
    )

    detail = sharing_service.get_shared_cookbook(db_session, user_id=alice.id, cookbook_id=cb.id)
    assert {m.user_id for m in detail.members} == {alice.id}


def test_remove_member_non_creator_cannot_remove_someone_else(
    db_session: Session, alice: User, bob: User, carol: User
) -> None:
    cb = sharing_service.create_shared_cookbook(db_session, creator_id=alice.id, name="Cookbook")
    sharing_service.invite_member(db_session, user_id=alice.id, cookbook_id=cb.id, email=bob.email)
    sharing_service.invite_member(
        db_session, user_id=alice.id, cookbook_id=cb.id, email=carol.email
    )

    with pytest.raises(ApiError) as exc_info:
        sharing_service.remove_member(
            db_session, user_id=bob.id, cookbook_id=cb.id, target_user_id=carol.id
        )
    assert exc_info.value.status_code == 404

    # Carol is still a member.
    detail = sharing_service.get_shared_cookbook(db_session, user_id=alice.id, cookbook_id=cb.id)
    assert carol.id in {m.user_id for m in detail.members}


def test_remove_member_creator_cannot_leave_while_others_present(
    db_session: Session, alice: User, bob: User
) -> None:
    cb = sharing_service.create_shared_cookbook(db_session, creator_id=alice.id, name="Cookbook")
    sharing_service.invite_member(db_session, user_id=alice.id, cookbook_id=cb.id, email=bob.email)

    with pytest.raises(ApiError) as exc_info:
        sharing_service.remove_member(
            db_session, user_id=alice.id, cookbook_id=cb.id, target_user_id=alice.id
        )
    assert exc_info.value.status_code == 422
    assert exc_info.value.code == "creator_cannot_leave"


def test_remove_member_last_member_leaving_deletes_cookbook(
    db_session: Session, alice: User
) -> None:
    cb = sharing_service.create_shared_cookbook(db_session, creator_id=alice.id, name="Cookbook")

    sharing_service.remove_member(
        db_session, user_id=alice.id, cookbook_id=cb.id, target_user_id=alice.id
    )

    with pytest.raises(ApiError) as exc_info:
        sharing_service.get_shared_cookbook(db_session, user_id=alice.id, cookbook_id=cb.id)
    assert exc_info.value.status_code == 404


def test_remove_member_non_member_actor_raises_404(
    db_session: Session, alice: User, bob: User
) -> None:
    cb = sharing_service.create_shared_cookbook(db_session, creator_id=alice.id, name="Cookbook")
    with pytest.raises(ApiError) as exc_info:
        sharing_service.remove_member(
            db_session, user_id=bob.id, cookbook_id=cb.id, target_user_id=alice.id
        )
    assert exc_info.value.status_code == 404


def test_remove_member_target_not_a_member_raises_404(
    db_session: Session, alice: User, bob: User
) -> None:
    cb = sharing_service.create_shared_cookbook(db_session, creator_id=alice.id, name="Cookbook")
    with pytest.raises(ApiError) as exc_info:
        sharing_service.remove_member(
            db_session, user_id=alice.id, cookbook_id=cb.id, target_user_id=bob.id
        )
    assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# add_recipe_to_shared_cookbook / remove_recipe_from_shared_cookbook
# ---------------------------------------------------------------------------


def test_add_recipe_requires_actor_to_have_recipe_access(
    db_session: Session, alice: User, bob: User
) -> None:
    cb = sharing_service.create_shared_cookbook(db_session, creator_id=alice.id, name="Cookbook")
    sharing_service.invite_member(db_session, user_id=alice.id, cookbook_id=cb.id, email=bob.email)
    # A recipe owned by neither alice nor bob.
    carol = make_user(db_session, "Carol")
    foreign_recipe_id = _create_recipe(db_session, carol.id)

    with pytest.raises(ApiError) as exc_info:
        sharing_service.add_recipe_to_shared_cookbook(
            db_session, user_id=bob.id, cookbook_id=cb.id, recipe_id=foreign_recipe_id
        )
    assert exc_info.value.status_code == 404


def test_add_recipe_non_member_actor_raises_404(
    db_session: Session, alice: User, bob: User
) -> None:
    cb = sharing_service.create_shared_cookbook(db_session, creator_id=alice.id, name="Cookbook")
    recipe_id = _create_recipe(db_session, bob.id)
    with pytest.raises(ApiError) as exc_info:
        sharing_service.add_recipe_to_shared_cookbook(
            db_session, user_id=bob.id, cookbook_id=cb.id, recipe_id=recipe_id
        )
    assert exc_info.value.status_code == 404


def test_add_recipe_already_added_raises_409(db_session: Session, alice: User) -> None:
    cb = sharing_service.create_shared_cookbook(db_session, creator_id=alice.id, name="Cookbook")
    recipe_id = _create_recipe(db_session, alice.id)
    sharing_service.add_recipe_to_shared_cookbook(
        db_session, user_id=alice.id, cookbook_id=cb.id, recipe_id=recipe_id
    )
    with pytest.raises(ApiError) as exc_info:
        sharing_service.add_recipe_to_shared_cookbook(
            db_session, user_id=alice.id, cookbook_id=cb.id, recipe_id=recipe_id
        )
    assert exc_info.value.status_code == 409
    assert exc_info.value.code == "already_added"


def test_add_recipe_via_membership_in_another_cookbook(
    db_session: Session, alice: User, bob: User
) -> None:
    """A member's existing shared-cookbook access to a recipe is enough to re-share it."""
    cb1 = sharing_service.create_shared_cookbook(db_session, creator_id=alice.id, name="One")
    cb2 = sharing_service.create_shared_cookbook(db_session, creator_id=bob.id, name="Two")
    sharing_service.invite_member(db_session, user_id=alice.id, cookbook_id=cb1.id, email=bob.email)
    sharing_service.invite_member(db_session, user_id=bob.id, cookbook_id=cb2.id, email=alice.email)
    recipe_id = _create_recipe(db_session, alice.id)
    sharing_service.add_recipe_to_shared_cookbook(
        db_session, user_id=alice.id, cookbook_id=cb1.id, recipe_id=recipe_id
    )

    # Bob has access via cb1 membership; he adds the same recipe into cb2.
    out = sharing_service.add_recipe_to_shared_cookbook(
        db_session, user_id=bob.id, cookbook_id=cb2.id, recipe_id=recipe_id
    )
    assert out.id == recipe_id


def test_remove_recipe_any_member(db_session: Session, alice: User, bob: User) -> None:
    cb = sharing_service.create_shared_cookbook(db_session, creator_id=alice.id, name="Cookbook")
    sharing_service.invite_member(db_session, user_id=alice.id, cookbook_id=cb.id, email=bob.email)
    recipe_id = _create_recipe(db_session, alice.id)
    sharing_service.add_recipe_to_shared_cookbook(
        db_session, user_id=alice.id, cookbook_id=cb.id, recipe_id=recipe_id
    )

    sharing_service.remove_recipe_from_shared_cookbook(
        db_session, user_id=bob.id, cookbook_id=cb.id, recipe_id=recipe_id
    )

    detail = sharing_service.get_shared_cookbook(db_session, user_id=alice.id, cookbook_id=cb.id)
    assert detail.recipes == []


def test_remove_recipe_not_linked_raises_404(db_session: Session, alice: User) -> None:
    cb = sharing_service.create_shared_cookbook(db_session, creator_id=alice.id, name="Cookbook")
    recipe_id = _create_recipe(db_session, alice.id)
    with pytest.raises(ApiError) as exc_info:
        sharing_service.remove_recipe_from_shared_cookbook(
            db_session, user_id=alice.id, cookbook_id=cb.id, recipe_id=recipe_id
        )
    assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# THE AUTHORIZATION MATRIX — cookbook.service access widening
# ---------------------------------------------------------------------------


def test_member_can_get_recipe(db_session: Session, alice: User, bob: User) -> None:
    cb = sharing_service.create_shared_cookbook(db_session, creator_id=alice.id, name="Cookbook")
    sharing_service.invite_member(db_session, user_id=alice.id, cookbook_id=cb.id, email=bob.email)
    recipe_id = _create_recipe(db_session, alice.id)
    sharing_service.add_recipe_to_shared_cookbook(
        db_session, user_id=alice.id, cookbook_id=cb.id, recipe_id=recipe_id
    )

    out = cookbook_service.get_recipe(db_session, owner_id=bob.id, recipe_id=recipe_id)
    assert out.id == recipe_id
    assert cookbook_service.user_recipe_access(db_session, bob.id, recipe_id) == "member"
    assert cookbook_service.user_recipe_access(db_session, alice.id, recipe_id) == "owner"


def test_member_can_update_recipe_and_sets_last_edited_by(
    db_session: Session, alice: User, bob: User
) -> None:
    cb = sharing_service.create_shared_cookbook(db_session, creator_id=alice.id, name="Cookbook")
    sharing_service.invite_member(db_session, user_id=alice.id, cookbook_id=cb.id, email=bob.email)
    recipe_id = _create_recipe(db_session, alice.id)
    sharing_service.add_recipe_to_shared_cookbook(
        db_session, user_id=alice.id, cookbook_id=cb.id, recipe_id=recipe_id
    )

    updated = cookbook_service.update_recipe(
        db_session,
        owner_id=bob.id,
        recipe_id=recipe_id,
        data=_recipe_in("Bob's edit"),
        editor_id=bob.id,
    )
    assert updated.title == "Bob's edit"
    assert updated.last_edited_by == bob.id
    assert updated.last_edited_at is not None

    # The recipe is still owned by alice — editing doesn't transfer ownership.
    still_owned_by_alice = cookbook_service.get_recipe(
        db_session, owner_id=alice.id, recipe_id=recipe_id
    )
    assert still_owned_by_alice.owner_id == alice.id


def test_member_cannot_delete_recipe(db_session: Session, alice: User, bob: User) -> None:
    cb = sharing_service.create_shared_cookbook(db_session, creator_id=alice.id, name="Cookbook")
    sharing_service.invite_member(db_session, user_id=alice.id, cookbook_id=cb.id, email=bob.email)
    recipe_id = _create_recipe(db_session, alice.id)
    sharing_service.add_recipe_to_shared_cookbook(
        db_session, user_id=alice.id, cookbook_id=cb.id, recipe_id=recipe_id
    )

    with pytest.raises(ApiError) as exc_info:
        cookbook_service.delete_recipe(db_session, owner_id=bob.id, recipe_id=recipe_id)
    assert exc_info.value.status_code == 404

    # Still there.
    cookbook_service.get_recipe(db_session, owner_id=alice.id, recipe_id=recipe_id)


def test_member_cannot_favorite_recipe(db_session: Session, alice: User, bob: User) -> None:
    cb = sharing_service.create_shared_cookbook(db_session, creator_id=alice.id, name="Cookbook")
    sharing_service.invite_member(db_session, user_id=alice.id, cookbook_id=cb.id, email=bob.email)
    recipe_id = _create_recipe(db_session, alice.id)
    sharing_service.add_recipe_to_shared_cookbook(
        db_session, user_id=alice.id, cookbook_id=cb.id, recipe_id=recipe_id
    )

    with pytest.raises(ApiError) as exc_info:
        cookbook_service.set_personal(
            db_session, owner_id=bob.id, recipe_id=recipe_id, is_favorite=True
        )
    assert exc_info.value.status_code == 404


def test_member_cannot_set_recipe_collections(db_session: Session, alice: User, bob: User) -> None:
    cb = sharing_service.create_shared_cookbook(db_session, creator_id=alice.id, name="Cookbook")
    sharing_service.invite_member(db_session, user_id=alice.id, cookbook_id=cb.id, email=bob.email)
    recipe_id = _create_recipe(db_session, alice.id)
    sharing_service.add_recipe_to_shared_cookbook(
        db_session, user_id=alice.id, cookbook_id=cb.id, recipe_id=recipe_id
    )

    with pytest.raises(ApiError) as exc_info:
        cookbook_service.set_recipe_collections(
            db_session, owner_id=bob.id, recipe_id=recipe_id, collection_ids=[]
        )
    assert exc_info.value.status_code == 404


def test_member_cannot_upload_recipe_image(
    db_session: Session, alice: User, bob: User, tmp_path: Path
) -> None:
    from recipe_normalizer.filestore import LocalFileStore

    cb = sharing_service.create_shared_cookbook(db_session, creator_id=alice.id, name="Cookbook")
    sharing_service.invite_member(db_session, user_id=alice.id, cookbook_id=cb.id, email=bob.email)
    recipe_id = _create_recipe(db_session, alice.id)
    sharing_service.add_recipe_to_shared_cookbook(
        db_session, user_id=alice.id, cookbook_id=cb.id, recipe_id=recipe_id
    )

    png_bytes = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0"
        b"\x00\x00\x03\x01\x01\x00\x18\xdd\x8d\xb0\x00\x00\x00\x00IEND\xaeB`\x82"
    )

    with pytest.raises(ApiError) as exc_info:
        cookbook_service.set_recipe_image(
            db_session,
            owner_id=bob.id,
            recipe_id=recipe_id,
            data=png_bytes,
            store=LocalFileStore(tmp_path),
        )
    assert exc_info.value.status_code == 404


def test_member_recipe_not_shown_in_personal_list_recipes(
    db_session: Session, alice: User, bob: User
) -> None:
    """A member's personal cookbook list never shows someone else's shared recipe."""
    cb = sharing_service.create_shared_cookbook(db_session, creator_id=alice.id, name="Cookbook")
    sharing_service.invite_member(db_session, user_id=alice.id, cookbook_id=cb.id, email=bob.email)
    recipe_id = _create_recipe(db_session, alice.id)
    sharing_service.add_recipe_to_shared_cookbook(
        db_session, user_id=alice.id, cookbook_id=cb.id, recipe_id=recipe_id
    )

    page = cookbook_service.list_recipes(db_session, owner_id=bob.id)
    assert recipe_id not in {r.id for r in page.items}


def test_non_member_gets_404_on_get_and_update(db_session: Session, alice: User, bob: User) -> None:
    cb = sharing_service.create_shared_cookbook(db_session, creator_id=alice.id, name="Cookbook")
    recipe_id = _create_recipe(db_session, alice.id)
    sharing_service.add_recipe_to_shared_cookbook(
        db_session, user_id=alice.id, cookbook_id=cb.id, recipe_id=recipe_id
    )
    # bob is not a member of cb at all.
    with pytest.raises(ApiError) as exc_info:
        cookbook_service.get_recipe(db_session, owner_id=bob.id, recipe_id=recipe_id)
    assert exc_info.value.status_code == 404

    with pytest.raises(ApiError) as exc_info:
        cookbook_service.update_recipe(
            db_session,
            owner_id=bob.id,
            recipe_id=recipe_id,
            data=_recipe_in("hijack"),
            editor_id=bob.id,
        )
    assert exc_info.value.status_code == 404


def test_removed_member_immediately_loses_access(
    db_session: Session, alice: User, bob: User
) -> None:
    cb = sharing_service.create_shared_cookbook(db_session, creator_id=alice.id, name="Cookbook")
    sharing_service.invite_member(db_session, user_id=alice.id, cookbook_id=cb.id, email=bob.email)
    recipe_id = _create_recipe(db_session, alice.id)
    sharing_service.add_recipe_to_shared_cookbook(
        db_session, user_id=alice.id, cookbook_id=cb.id, recipe_id=recipe_id
    )

    cookbook_service.get_recipe(db_session, owner_id=bob.id, recipe_id=recipe_id)  # has access

    sharing_service.remove_member(
        db_session, user_id=alice.id, cookbook_id=cb.id, target_user_id=bob.id
    )

    with pytest.raises(ApiError) as exc_info:
        cookbook_service.get_recipe(db_session, owner_id=bob.id, recipe_id=recipe_id)
    assert exc_info.value.status_code == 404


def test_recipe_removed_from_cookbook_loses_member_access(
    db_session: Session, alice: User, bob: User
) -> None:
    cb = sharing_service.create_shared_cookbook(db_session, creator_id=alice.id, name="Cookbook")
    sharing_service.invite_member(db_session, user_id=alice.id, cookbook_id=cb.id, email=bob.email)
    recipe_id = _create_recipe(db_session, alice.id)
    sharing_service.add_recipe_to_shared_cookbook(
        db_session, user_id=alice.id, cookbook_id=cb.id, recipe_id=recipe_id
    )

    cookbook_service.get_recipe(db_session, owner_id=bob.id, recipe_id=recipe_id)  # has access

    sharing_service.remove_recipe_from_shared_cookbook(
        db_session, user_id=alice.id, cookbook_id=cb.id, recipe_id=recipe_id
    )

    with pytest.raises(ApiError) as exc_info:
        cookbook_service.get_recipe(db_session, owner_id=bob.id, recipe_id=recipe_id)
    assert exc_info.value.status_code == 404


def test_recipe_in_two_cookbooks_removed_from_one_access_persists_via_other(
    db_session: Session, alice: User, bob: User
) -> None:
    cb1 = sharing_service.create_shared_cookbook(db_session, creator_id=alice.id, name="One")
    cb2 = sharing_service.create_shared_cookbook(db_session, creator_id=alice.id, name="Two")
    sharing_service.invite_member(db_session, user_id=alice.id, cookbook_id=cb1.id, email=bob.email)
    sharing_service.invite_member(db_session, user_id=alice.id, cookbook_id=cb2.id, email=bob.email)
    recipe_id = _create_recipe(db_session, alice.id)
    sharing_service.add_recipe_to_shared_cookbook(
        db_session, user_id=alice.id, cookbook_id=cb1.id, recipe_id=recipe_id
    )
    sharing_service.add_recipe_to_shared_cookbook(
        db_session, user_id=alice.id, cookbook_id=cb2.id, recipe_id=recipe_id
    )

    sharing_service.remove_recipe_from_shared_cookbook(
        db_session, user_id=alice.id, cookbook_id=cb1.id, recipe_id=recipe_id
    )

    # Bob still has access via cb2.
    out = cookbook_service.get_recipe(db_session, owner_id=bob.id, recipe_id=recipe_id)
    assert out.id == recipe_id
