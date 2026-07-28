"""Tests for the cookbook access/role-resolution layer.

Covers the full access matrix `cookbook_access` and `require_cookbook_access`
must resolve: owner_id always wins, member rows grant their exact role,
public/unlisted cookbooks grant anonymous (user_id=None) viewer read only,
and a nonexistent cookbook resolves to None / raises 404 exactly like a
private cookbook the caller has no claim on.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.orm import Session

from recipe_normalizer.cookbook.models import (
    Cookbook,
    CookbookMember,
    CookbookRole,
    CookbookVisibility,
)
from recipe_normalizer.cookbook.service import cookbook_access, require_cookbook_access
from recipe_normalizer.errors import ApiError
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


# ---------------------------------------------------------------------------
# cookbook_access — owner
# ---------------------------------------------------------------------------


def test_owner_gets_owner_access(db_session: Session) -> None:
    owner = make_user(db_session, "1")
    cookbook = make_cookbook(db_session, owner)

    assert cookbook_access(db_session, user_id=owner.id, cookbook_id=cookbook.id) == "owner"


def test_owner_id_wins_even_if_also_a_member_row(db_session: Session) -> None:
    owner = make_user(db_session, "2")
    cookbook = make_cookbook(db_session, owner)
    # Pathological but should never override owner resolution.
    add_member(db_session, cookbook, owner, CookbookRole.viewer)

    assert cookbook_access(db_session, user_id=owner.id, cookbook_id=cookbook.id) == "owner"


# ---------------------------------------------------------------------------
# cookbook_access — members
# ---------------------------------------------------------------------------


def test_editor_member_gets_editor_access(db_session: Session) -> None:
    owner = make_user(db_session, "3")
    editor = make_user(db_session, "4")
    cookbook = make_cookbook(db_session, owner)
    add_member(db_session, cookbook, editor, CookbookRole.editor)

    assert (
        cookbook_access(db_session, user_id=editor.id, cookbook_id=cookbook.id)
        == CookbookRole.editor
    )


def test_viewer_member_gets_viewer_access(db_session: Session) -> None:
    owner = make_user(db_session, "5")
    viewer = make_user(db_session, "6")
    cookbook = make_cookbook(db_session, owner)
    add_member(db_session, cookbook, viewer, CookbookRole.viewer)

    assert (
        cookbook_access(db_session, user_id=viewer.id, cookbook_id=cookbook.id)
        == CookbookRole.viewer
    )


# ---------------------------------------------------------------------------
# cookbook_access — non-members
# ---------------------------------------------------------------------------


def test_non_member_on_private_cookbook_gets_none(db_session: Session) -> None:
    owner = make_user(db_session, "7")
    stranger = make_user(db_session, "8")
    cookbook = make_cookbook(db_session, owner)

    assert cookbook_access(db_session, user_id=stranger.id, cookbook_id=cookbook.id) is None


def test_nonexistent_cookbook_gets_none(db_session: Session) -> None:
    someone = make_user(db_session, "9")

    assert cookbook_access(db_session, user_id=someone.id, cookbook_id=uuid.uuid4()) is None


def test_anonymous_on_private_cookbook_gets_none(db_session: Session) -> None:
    owner = make_user(db_session, "10")
    cookbook = make_cookbook(db_session, owner)

    assert cookbook_access(db_session, user_id=None, cookbook_id=cookbook.id) is None


# ---------------------------------------------------------------------------
# cookbook_access — public/unlisted anonymous read
# ---------------------------------------------------------------------------


def test_anonymous_on_public_cookbook_gets_viewer(db_session: Session) -> None:
    owner = make_user(db_session, "11")
    cookbook = make_cookbook(db_session, owner, visibility=CookbookVisibility.public)

    assert cookbook_access(db_session, user_id=None, cookbook_id=cookbook.id) == CookbookRole.viewer


def test_anonymous_on_unlisted_cookbook_gets_viewer(db_session: Session) -> None:
    owner = make_user(db_session, "12")
    cookbook = make_cookbook(db_session, owner, visibility=CookbookVisibility.unlisted)

    assert cookbook_access(db_session, user_id=None, cookbook_id=cookbook.id) == CookbookRole.viewer


def test_any_authenticated_user_on_public_cookbook_gets_viewer(db_session: Session) -> None:
    owner = make_user(db_session, "13")
    rando = make_user(db_session, "14")
    cookbook = make_cookbook(db_session, owner, visibility=CookbookVisibility.public)

    assert (
        cookbook_access(db_session, user_id=rando.id, cookbook_id=cookbook.id)
        == CookbookRole.viewer
    )


def test_authenticated_non_member_on_unlisted_cookbook_gets_viewer(db_session: Session) -> None:
    owner = make_user(db_session, "29")
    rando = make_user(db_session, "30")
    cookbook = make_cookbook(db_session, owner, visibility=CookbookVisibility.unlisted)

    assert (
        cookbook_access(db_session, user_id=rando.id, cookbook_id=cookbook.id)
        == CookbookRole.viewer
    )


# ---------------------------------------------------------------------------
# cookbook_access — member role vs. visibility ordering invariant
#
# Step 2 (member row) must be consulted BEFORE step 3 (visibility fallback):
# an explicit-role member on a public/unlisted cookbook keeps their real
# role and must NEVER be downgraded to viewer by the visibility branch.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("visibility", [CookbookVisibility.public, CookbookVisibility.unlisted])
def test_editor_member_on_public_or_unlisted_cookbook_keeps_editor_role(
    db_session: Session, visibility: CookbookVisibility
) -> None:
    owner = make_user(db_session, f"31{visibility.value}")
    editor = make_user(db_session, f"32{visibility.value}")
    cookbook = make_cookbook(db_session, owner, visibility=visibility, name=f"CB-{visibility}-ed")
    add_member(db_session, cookbook, editor, CookbookRole.editor)

    assert (
        cookbook_access(db_session, user_id=editor.id, cookbook_id=cookbook.id)
        == CookbookRole.editor
    )


def test_viewer_member_on_public_cookbook_gets_viewer_via_member_row(db_session: Session) -> None:
    # Same *result* as the anonymous/rando visibility-fallback case, but this
    # pins that the member row is what's actually consulted for a member —
    # not an accidental early return from the visibility branch — so a
    # future reorder that skips the member lookup for public/unlisted
    # cookbooks would still be caught by the editor-member tests above.
    owner = make_user(db_session, "33")
    viewer = make_user(db_session, "34")
    cookbook = make_cookbook(db_session, owner, visibility=CookbookVisibility.public)
    add_member(db_session, cookbook, viewer, CookbookRole.viewer)

    assert (
        cookbook_access(db_session, user_id=viewer.id, cookbook_id=cookbook.id)
        == CookbookRole.viewer
    )


# ---------------------------------------------------------------------------
# require_cookbook_access — owner: every level ok
# ---------------------------------------------------------------------------


def test_require_owner_access_for_owner_at_every_level(db_session: Session) -> None:
    owner = make_user(db_session, "15")
    cookbook = make_cookbook(db_session, owner)

    for need in (CookbookRole.viewer, CookbookRole.editor, "owner"):
        result = require_cookbook_access(
            db_session, user_id=owner.id, cookbook_id=cookbook.id, need=need
        )
        assert result.id == cookbook.id


# ---------------------------------------------------------------------------
# require_cookbook_access — editor member: viewer+editor ok, owner denied
# ---------------------------------------------------------------------------


def test_require_editor_member_ok_at_viewer_and_editor(db_session: Session) -> None:
    owner = make_user(db_session, "16")
    editor = make_user(db_session, "17")
    cookbook = make_cookbook(db_session, owner)
    add_member(db_session, cookbook, editor, CookbookRole.editor)

    for need in (CookbookRole.viewer, CookbookRole.editor):
        result = require_cookbook_access(
            db_session, user_id=editor.id, cookbook_id=cookbook.id, need=need
        )
        assert result.id == cookbook.id


def test_require_editor_member_denied_owner(db_session: Session) -> None:
    owner = make_user(db_session, "18")
    editor = make_user(db_session, "19")
    cookbook = make_cookbook(db_session, owner)
    add_member(db_session, cookbook, editor, CookbookRole.editor)

    with pytest.raises(ApiError) as exc_info:
        require_cookbook_access(
            db_session, user_id=editor.id, cookbook_id=cookbook.id, need="owner"
        )
    assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# require_cookbook_access — viewer member: viewer ok, editor+owner denied
# ---------------------------------------------------------------------------


def test_require_viewer_member_ok_at_viewer(db_session: Session) -> None:
    owner = make_user(db_session, "20")
    viewer = make_user(db_session, "21")
    cookbook = make_cookbook(db_session, owner)
    add_member(db_session, cookbook, viewer, CookbookRole.viewer)

    result = require_cookbook_access(
        db_session, user_id=viewer.id, cookbook_id=cookbook.id, need=CookbookRole.viewer
    )
    assert result.id == cookbook.id


@pytest.mark.parametrize("need", [CookbookRole.editor, "owner"])
def test_require_viewer_member_denied_editor_and_owner(
    db_session: Session, need: CookbookRole | str
) -> None:
    owner = make_user(db_session, "22")
    viewer = make_user(db_session, "23")
    cookbook = make_cookbook(db_session, owner)
    add_member(db_session, cookbook, viewer, CookbookRole.viewer)

    with pytest.raises(ApiError) as exc_info:
        require_cookbook_access(db_session, user_id=viewer.id, cookbook_id=cookbook.id, need=need)
    assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# require_cookbook_access — non-member on private cookbook -> 404
# ---------------------------------------------------------------------------


def test_require_non_member_on_private_cookbook_raises_404(db_session: Session) -> None:
    owner = make_user(db_session, "24")
    stranger = make_user(db_session, "25")
    cookbook = make_cookbook(db_session, owner)

    with pytest.raises(ApiError) as exc_info:
        require_cookbook_access(
            db_session, user_id=stranger.id, cookbook_id=cookbook.id, need=CookbookRole.viewer
        )
    assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# require_cookbook_access — anonymous on public/unlisted: viewer ok, editor/owner denied
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("visibility", [CookbookVisibility.public, CookbookVisibility.unlisted])
def test_require_anonymous_viewer_ok_on_public_and_unlisted(
    db_session: Session, visibility: CookbookVisibility
) -> None:
    owner = make_user(db_session, "26")
    cookbook = make_cookbook(db_session, owner, visibility=visibility, name=f"CB-{visibility}")

    result = require_cookbook_access(
        db_session, user_id=None, cookbook_id=cookbook.id, need=CookbookRole.viewer
    )
    assert result.id == cookbook.id


@pytest.mark.parametrize("visibility", [CookbookVisibility.public, CookbookVisibility.unlisted])
@pytest.mark.parametrize("need", [CookbookRole.editor, "owner"])
def test_require_anonymous_denied_editor_and_owner_on_public_and_unlisted(
    db_session: Session, visibility: CookbookVisibility, need: CookbookRole | str
) -> None:
    owner = make_user(db_session, "27")
    cookbook = make_cookbook(
        db_session, owner, visibility=visibility, name=f"CB-{visibility}-{need}"
    )

    with pytest.raises(ApiError) as exc_info:
        require_cookbook_access(db_session, user_id=None, cookbook_id=cookbook.id, need=need)
    assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# require_cookbook_access — authenticated NON-member on public/unlisted:
# viewer ok, editor/owner denied (same shape as the anonymous case above,
# but with a real user_id that isn't the owner or a member row).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("visibility", [CookbookVisibility.public, CookbookVisibility.unlisted])
def test_require_authenticated_non_member_viewer_ok_on_public_and_unlisted(
    db_session: Session, visibility: CookbookVisibility
) -> None:
    owner = make_user(db_session, f"35{visibility.value}")
    rando = make_user(db_session, f"36{visibility.value}")
    cookbook = make_cookbook(db_session, owner, visibility=visibility, name=f"CB-{visibility}-r")

    result = require_cookbook_access(
        db_session, user_id=rando.id, cookbook_id=cookbook.id, need=CookbookRole.viewer
    )
    assert result.id == cookbook.id


@pytest.mark.parametrize("visibility", [CookbookVisibility.public, CookbookVisibility.unlisted])
@pytest.mark.parametrize("need", [CookbookRole.editor, "owner"])
def test_require_authenticated_non_member_denied_editor_and_owner_on_public_and_unlisted(
    db_session: Session, visibility: CookbookVisibility, need: CookbookRole | str
) -> None:
    owner = make_user(db_session, f"37{visibility.value}{need}")
    rando = make_user(db_session, f"38{visibility.value}{need}")
    cookbook = make_cookbook(
        db_session, owner, visibility=visibility, name=f"CB-{visibility}-{need}-r"
    )

    with pytest.raises(ApiError) as exc_info:
        require_cookbook_access(db_session, user_id=rando.id, cookbook_id=cookbook.id, need=need)
    assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# require_cookbook_access — nonexistent cookbook -> 404
# ---------------------------------------------------------------------------


def test_require_nonexistent_cookbook_raises_404(db_session: Session) -> None:
    someone = make_user(db_session, "28")

    with pytest.raises(ApiError) as exc_info:
        require_cookbook_access(
            db_session, user_id=someone.id, cookbook_id=uuid.uuid4(), need=CookbookRole.viewer
        )
    assert exc_info.value.status_code == 404


def test_require_nonexistent_cookbook_anonymous_raises_404(db_session: Session) -> None:
    with pytest.raises(ApiError) as exc_info:
        require_cookbook_access(
            db_session, user_id=None, cookbook_id=uuid.uuid4(), need=CookbookRole.viewer
        )
    assert exc_info.value.status_code == 404
