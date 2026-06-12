import pytest

from recipe_normalizer.users import service
from recipe_normalizer.users.service import AuthError


def test_register_and_login(db_session):
    user = service.register(db_session, email="ido@x.com", password="hunter22", display_name="Ido")
    assert user.email == "ido@x.com"

    token = service.login(db_session, email="ido@x.com", password="hunter22")
    assert isinstance(token, str) and len(token) >= 32

    current = service.get_user_by_token(db_session, token)
    assert current is not None and current.id == user.id


def test_wrong_password_rejected(db_session):
    service.register(db_session, email="a@x.com", password="rightpass", display_name="A")
    with pytest.raises(AuthError):
        service.login(db_session, email="a@x.com", password="wrongpass")


def test_unknown_email_rejected(db_session):
    with pytest.raises(AuthError):
        service.login(db_session, email="ghost@x.com", password="whatever1")


def test_duplicate_email_rejected(db_session):
    service.register(db_session, email="a@x.com", password="p1234567", display_name="A")
    with pytest.raises(AuthError):
        service.register(db_session, email="a@x.com", password="p7654321", display_name="B")


def test_logout_invalidates_token(db_session):
    service.register(db_session, email="a@x.com", password="p1234567", display_name="A")
    token = service.login(db_session, email="a@x.com", password="p1234567")
    service.logout(db_session, token)
    assert service.get_user_by_token(db_session, token) is None


def test_expired_session_rejected(db_session):
    # register+login, then force the session's expires_at into the past and assert rejection
    from datetime import UTC, datetime, timedelta

    from recipe_normalizer.users.models import Session as DbSession

    service.register(db_session, email="a@x.com", password="p1234567", display_name="A")
    token = service.login(db_session, email="a@x.com", password="p1234567")
    db_session.query(DbSession).update({"expires_at": datetime.now(UTC) - timedelta(hours=1)})
    assert service.get_user_by_token(db_session, token) is None
