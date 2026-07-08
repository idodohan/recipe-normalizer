import pytest

from recipe_normalizer.config import settings
from recipe_normalizer.users import service
from recipe_normalizer.users.service import AuthError


def test_register_and_login(db_session):
    user = service.register(db_session, email="ido@x.com", password="hunter22", display_name="Ido")
    assert user.email == "ido@x.com"

    token, logged_in = service.login(db_session, email="ido@x.com", password="hunter22")
    assert isinstance(token, str) and len(token) >= 32
    assert logged_in.id == user.id

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
    token, _ = service.login(db_session, email="a@x.com", password="p1234567")
    service.logout(db_session, token)
    assert service.get_user_by_token(db_session, token) is None


def test_register_admin_email_is_admin(db_session, monkeypatch):
    monkeypatch.setattr(settings, "admin_emails", "boss@example.com")
    user = service.register(
        db_session, email="boss@example.com", password="hunter22", display_name="Boss"
    )
    assert user.is_admin is True


def test_register_non_admin_email_is_not_admin(db_session, monkeypatch):
    monkeypatch.setattr(settings, "admin_emails", "boss@example.com")
    user = service.register(
        db_session, email="worker@example.com", password="hunter22", display_name="Worker"
    )
    assert user.is_admin is False


def test_expired_session_rejected(db_session):
    # register+login, then force the session's expires_at into the past and assert rejection
    from datetime import UTC, datetime, timedelta

    from recipe_normalizer.users.models import Session as DbSession

    service.register(db_session, email="a@x.com", password="p1234567", display_name="A")
    token, _ = service.login(db_session, email="a@x.com", password="p1234567")
    db_session.query(DbSession).update({"expires_at": datetime.now(UTC) - timedelta(hours=1)})
    assert service.get_user_by_token(db_session, token) is None
