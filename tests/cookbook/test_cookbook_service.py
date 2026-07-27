"""Service-level tests for cookbook CRUD, visibility, and membership.

One behavior per test, per the Task 4 brief. Fixtures mirror
test_cookbook_access.py's make_user/make_cookbook helpers.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from recipe_normalizer.cookbook.models import (
    Cookbook,
    CookbookMember,
    CookbookRole,
    CookbookVisibility,
    Recipe,
    SourceType,
)
from recipe_normalizer.cookbook.service import (
    create_cookbook,
    delete_cookbook,
    ensure_default_cookbook,
    invite_cookbook_member,
    leave_cookbook,
    list_my_cookbooks,
    remove_cookbook_member,
    rename_cookbook,
    set_cookbook_cover,
    set_cookbook_description,
    set_cookbook_member_role,
    set_cookbook_visibility,
)
from recipe_normalizer.errors import ApiError
from recipe_normalizer.users.models import User

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_user(db: Session, suffix: str = "") -> User:
    user = User(
        email=f"cook{suffix}@example.com",
        password_hash="hash",
        display_name=f"Cook{suffix}",
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
    is_default: bool = False,
) -> Cookbook:
    cookbook = Cookbook(owner_id=owner.id, name=name, visibility=visibility, is_default=is_default)
    db.add(cookbook)
    db.flush()
    return cookbook


def add_member(db: Session, cookbook: Cookbook, user: User, role: CookbookRole) -> CookbookMember:
    member = CookbookMember(cookbook_id=cookbook.id, user_id=user.id, role=role)
    db.add(member)
    db.flush()
    return member


# ---------------------------------------------------------------------------
# create_cookbook / list_my_cookbooks
# ---------------------------------------------------------------------------


def test_create_cookbook_appears_in_list_with_owner_role_and_count(db_session: Session) -> None:
    owner = make_user(db_session, "1")

    created = create_cookbook(db_session, owner_id=owner.id, name="Family Favorites")

    recipe = Recipe(
        owner_id=owner.id,
        title="Pasta",
        source_type=SourceType.manual,
        cookbook_id=created.id,
    )
    db_session.add(recipe)
    db_session.flush()

    summaries = list_my_cookbooks(db_session, owner.id)
    matching = [s for s in summaries if s.id == created.id]
    assert len(matching) == 1
    summary = matching[0]
    assert summary.name == "Family Favorites"
    assert summary.role == "owner"
    assert summary.recipe_count == 1
    assert summary.is_default is False


def test_list_my_cookbooks_includes_cookbooks_user_is_member_of(db_session: Session) -> None:
    owner = make_user(db_session, "2")
    editor = make_user(db_session, "3")
    cookbook = make_cookbook(db_session, owner, name="Shared Book")
    add_member(db_session, cookbook, editor, CookbookRole.editor)

    summaries = list_my_cookbooks(db_session, editor.id)
    matching = [s for s in summaries if s.id == cookbook.id]
    assert len(matching) == 1
    assert matching[0].role == "editor"


# ---------------------------------------------------------------------------
# rename / description / cover persist
# ---------------------------------------------------------------------------


def test_rename_cookbook_persists(db_session: Session) -> None:
    owner = make_user(db_session, "4")
    cookbook = create_cookbook(db_session, owner_id=owner.id, name="Old Name")

    rename_cookbook(db_session, cookbook.id, owner.id, "New Name")

    db_session.expire_all()
    fetched = db_session.get(Cookbook, cookbook.id)
    assert fetched is not None
    assert fetched.name == "New Name"


def test_set_cookbook_description_persists(db_session: Session) -> None:
    owner = make_user(db_session, "5")
    cookbook = create_cookbook(db_session, owner_id=owner.id, name="Book")

    set_cookbook_description(db_session, cookbook.id, owner.id, "A lovely collection")

    db_session.expire_all()
    fetched = db_session.get(Cookbook, cookbook.id)
    assert fetched is not None
    assert fetched.description == "A lovely collection"


def test_set_cookbook_cover_persists(db_session: Session) -> None:
    owner = make_user(db_session, "6")
    cookbook = create_cookbook(db_session, owner_id=owner.id, name="Book")

    set_cookbook_cover(db_session, cookbook.id, owner.id, "sha16/abc.jpg")

    db_session.expire_all()
    fetched = db_session.get(Cookbook, cookbook.id)
    assert fetched is not None
    assert fetched.cover_image_ref == "sha16/abc.jpg"


# ---------------------------------------------------------------------------
# delete_cookbook
# ---------------------------------------------------------------------------


def test_delete_non_default_cookbook_ok(db_session: Session) -> None:
    owner = make_user(db_session, "7")
    cookbook = create_cookbook(db_session, owner_id=owner.id, name="Disposable")

    delete_cookbook(db_session, cookbook.id, owner.id)

    db_session.expire_all()
    assert db_session.get(Cookbook, cookbook.id) is None


def test_delete_default_cookbook_raises_409(db_session: Session) -> None:
    owner = make_user(db_session, "8")
    default_cookbook = ensure_default_cookbook(db_session, owner.id)

    with pytest.raises(ApiError) as exc_info:
        delete_cookbook(db_session, default_cookbook.id, owner.id)
    assert exc_info.value.status_code == 409
    assert exc_info.value.code == "cannot_delete_default"


# ---------------------------------------------------------------------------
# set_cookbook_visibility
# ---------------------------------------------------------------------------


def test_set_visibility_public_mints_token(db_session: Session) -> None:
    owner = make_user(db_session, "9")
    cookbook = create_cookbook(db_session, owner_id=owner.id, name="Book")
    assert cookbook.public_token is None

    set_cookbook_visibility(db_session, cookbook.id, owner.id, CookbookVisibility.public)

    db_session.expire_all()
    fetched = db_session.get(Cookbook, cookbook.id)
    assert fetched is not None
    assert fetched.visibility == CookbookVisibility.public
    assert fetched.public_token is not None


def test_set_visibility_back_to_private_clears_token(db_session: Session) -> None:
    owner = make_user(db_session, "10")
    cookbook = create_cookbook(db_session, owner_id=owner.id, name="Book")
    set_cookbook_visibility(db_session, cookbook.id, owner.id, CookbookVisibility.public)

    set_cookbook_visibility(db_session, cookbook.id, owner.id, CookbookVisibility.private)

    db_session.expire_all()
    fetched = db_session.get(Cookbook, cookbook.id)
    assert fetched is not None
    assert fetched.visibility == CookbookVisibility.private
    assert fetched.public_token is None


def test_set_visibility_public_does_not_remint_existing_token(db_session: Session) -> None:
    owner = make_user(db_session, "11")
    cookbook = create_cookbook(db_session, owner_id=owner.id, name="Book")
    set_cookbook_visibility(db_session, cookbook.id, owner.id, CookbookVisibility.unlisted)
    db_session.expire_all()
    first_token = db_session.get(Cookbook, cookbook.id).public_token  # type: ignore[union-attr]

    set_cookbook_visibility(db_session, cookbook.id, owner.id, CookbookVisibility.public)

    db_session.expire_all()
    fetched = db_session.get(Cookbook, cookbook.id)
    assert fetched is not None
    assert fetched.public_token == first_token


# ---------------------------------------------------------------------------
# ensure_default_cookbook
# ---------------------------------------------------------------------------


def test_ensure_default_cookbook_creates_one_when_missing(db_session: Session) -> None:
    owner = make_user(db_session, "12")

    cookbook = ensure_default_cookbook(db_session, owner.id)

    assert cookbook.is_default is True
    assert cookbook.name == "My Cookbook"
    assert cookbook.visibility == CookbookVisibility.private
    assert cookbook.owner_id == owner.id


def test_ensure_default_cookbook_is_idempotent(db_session: Session) -> None:
    owner = make_user(db_session, "13")

    first = ensure_default_cookbook(db_session, owner.id)
    second = ensure_default_cookbook(db_session, owner.id)

    assert first.id == second.id

    count = (
        db_session.query(Cookbook)
        .filter(Cookbook.owner_id == owner.id, Cookbook.is_default.is_(True))
        .count()
    )
    assert count == 1


# ---------------------------------------------------------------------------
# invite_cookbook_member
# ---------------------------------------------------------------------------


def test_invite_existing_user_adds_member_with_role(db_session: Session) -> None:
    owner = make_user(db_session, "14")
    invitee = make_user(db_session, "15")
    cookbook = create_cookbook(db_session, owner_id=owner.id, name="Book")

    invite_cookbook_member(db_session, cookbook.id, owner.id, invitee.email, CookbookRole.viewer)

    db_session.expire_all()
    member = db_session.get(CookbookMember, (cookbook.id, invitee.id))
    assert member is not None
    assert member.role == CookbookRole.viewer


def test_invite_unknown_email_raises_error(db_session: Session) -> None:
    owner = make_user(db_session, "16")
    cookbook = create_cookbook(db_session, owner_id=owner.id, name="Book")

    with pytest.raises(ApiError) as exc_info:
        invite_cookbook_member(
            db_session, cookbook.id, owner.id, "nobody@example.com", CookbookRole.viewer
        )
    assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# non-owner manage attempts -> 404 via require_cookbook_access
# ---------------------------------------------------------------------------


def test_editor_member_cannot_rename_cookbook(db_session: Session) -> None:
    owner = make_user(db_session, "17")
    editor = make_user(db_session, "18")
    cookbook = create_cookbook(db_session, owner_id=owner.id, name="Book")
    add_member(db_session, cookbook, editor, CookbookRole.editor)

    with pytest.raises(ApiError) as exc_info:
        rename_cookbook(db_session, cookbook.id, editor.id, "Hijacked")
    assert exc_info.value.status_code == 404


def test_editor_member_cannot_invite(db_session: Session) -> None:
    owner = make_user(db_session, "19")
    editor = make_user(db_session, "20")
    invitee = make_user(db_session, "21")
    cookbook = create_cookbook(db_session, owner_id=owner.id, name="Book")
    add_member(db_session, cookbook, editor, CookbookRole.editor)

    with pytest.raises(ApiError) as exc_info:
        invite_cookbook_member(
            db_session, cookbook.id, editor.id, invitee.email, CookbookRole.viewer
        )
    assert exc_info.value.status_code == 404


def test_editor_member_cannot_set_member_role(db_session: Session) -> None:
    owner = make_user(db_session, "22")
    editor = make_user(db_session, "23")
    viewer = make_user(db_session, "24")
    cookbook = create_cookbook(db_session, owner_id=owner.id, name="Book")
    add_member(db_session, cookbook, editor, CookbookRole.editor)
    add_member(db_session, cookbook, viewer, CookbookRole.viewer)

    with pytest.raises(ApiError) as exc_info:
        set_cookbook_member_role(db_session, cookbook.id, editor.id, viewer.id, CookbookRole.editor)
    assert exc_info.value.status_code == 404


def test_editor_member_cannot_remove_member(db_session: Session) -> None:
    owner = make_user(db_session, "25")
    editor = make_user(db_session, "26")
    viewer = make_user(db_session, "27")
    cookbook = create_cookbook(db_session, owner_id=owner.id, name="Book")
    add_member(db_session, cookbook, editor, CookbookRole.editor)
    add_member(db_session, cookbook, viewer, CookbookRole.viewer)

    with pytest.raises(ApiError) as exc_info:
        remove_cookbook_member(db_session, cookbook.id, editor.id, viewer.id)
    assert exc_info.value.status_code == 404


def test_editor_member_cannot_delete_cookbook(db_session: Session) -> None:
    owner = make_user(db_session, "28")
    editor = make_user(db_session, "29")
    cookbook = create_cookbook(db_session, owner_id=owner.id, name="Book")
    add_member(db_session, cookbook, editor, CookbookRole.editor)

    with pytest.raises(ApiError) as exc_info:
        delete_cookbook(db_session, cookbook.id, editor.id)
    assert exc_info.value.status_code == 404


def test_editor_member_cannot_set_visibility(db_session: Session) -> None:
    owner = make_user(db_session, "30")
    editor = make_user(db_session, "31")
    cookbook = create_cookbook(db_session, owner_id=owner.id, name="Book")
    add_member(db_session, cookbook, editor, CookbookRole.editor)

    with pytest.raises(ApiError) as exc_info:
        set_cookbook_visibility(db_session, cookbook.id, editor.id, CookbookVisibility.public)
    assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# set_cookbook_member_role / remove_cookbook_member (owner happy path)
# ---------------------------------------------------------------------------


def test_owner_can_set_member_role(db_session: Session) -> None:
    owner = make_user(db_session, "32")
    member = make_user(db_session, "33")
    cookbook = create_cookbook(db_session, owner_id=owner.id, name="Book")
    add_member(db_session, cookbook, member, CookbookRole.viewer)

    set_cookbook_member_role(db_session, cookbook.id, owner.id, member.id, CookbookRole.editor)

    db_session.expire_all()
    fetched = db_session.get(CookbookMember, (cookbook.id, member.id))
    assert fetched is not None
    assert fetched.role == CookbookRole.editor


def test_owner_can_remove_member(db_session: Session) -> None:
    owner = make_user(db_session, "34")
    member = make_user(db_session, "35")
    cookbook = create_cookbook(db_session, owner_id=owner.id, name="Book")
    add_member(db_session, cookbook, member, CookbookRole.viewer)

    remove_cookbook_member(db_session, cookbook.id, owner.id, member.id)

    db_session.expire_all()
    assert db_session.get(CookbookMember, (cookbook.id, member.id)) is None


# ---------------------------------------------------------------------------
# leave_cookbook
# ---------------------------------------------------------------------------


def test_leave_cookbook_removes_callers_member_row(db_session: Session) -> None:
    owner = make_user(db_session, "36")
    member = make_user(db_session, "37")
    cookbook = create_cookbook(db_session, owner_id=owner.id, name="Book")
    add_member(db_session, cookbook, member, CookbookRole.viewer)

    leave_cookbook(db_session, cookbook.id, member.id)

    db_session.expire_all()
    assert db_session.get(CookbookMember, (cookbook.id, member.id)) is None


def test_leave_cookbook_nonexistent_membership_is_noop(db_session: Session) -> None:
    owner = make_user(db_session, "38")
    stranger = make_user(db_session, "39")
    cookbook = create_cookbook(db_session, owner_id=owner.id, name="Book")

    # Should not raise even though stranger was never a member.
    leave_cookbook(db_session, cookbook.id, stranger.id)
