"""Tests for the out-of-band admin-grant CLI.

Admin is deliberately NOT grantable at registration time (see
``test_register_never_grants_admin``): this CLI is the only path, and it runs
against an already-existing user, so it cannot be triggered by anything a
remote client sends.
"""

from contextlib import contextmanager

import pytest

from recipe_normalizer.users import make_admin, service


@pytest.fixture()
def cli(db_session, monkeypatch):  # type: ignore[no-untyped-def]
    """Point make_admin's SessionLocal at the test session (commit → flush)."""

    @contextmanager
    def _session_local():  # type: ignore[no-untyped-def]
        monkeypatch.setattr(db_session, "commit", db_session.flush)
        yield db_session

    monkeypatch.setattr(make_admin, "SessionLocal", _session_local)
    return make_admin


def test_make_admin_promotes_existing_user(cli, db_session, monkeypatch, capsys):  # type: ignore[no-untyped-def]
    user = service.register(
        db_session, email="boss@example.com", password="hunter22", display_name="Boss"
    )
    assert user.is_admin is False

    monkeypatch.setattr("sys.argv", ["make_admin", "Boss@Example.com"])
    cli.main()

    db_session.refresh(user)
    assert user.is_admin is True
    assert "is now an admin" in capsys.readouterr().out


def test_make_admin_unknown_email_exits(cli, monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setattr("sys.argv", ["make_admin", "ghost@example.com"])
    with pytest.raises(SystemExit):
        cli.main()


def test_make_admin_requires_one_arg(cli, monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setattr("sys.argv", ["make_admin"])
    with pytest.raises(SystemExit):
        cli.main()
