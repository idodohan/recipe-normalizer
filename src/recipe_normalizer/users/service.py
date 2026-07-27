import uuid
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import delete, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from recipe_normalizer.errors import ApiError
from recipe_normalizer.users.auth import (
    PasswordAuthProvider,
    hash_token,
    new_session_token,
)
from recipe_normalizer.users.models import Session as DbSession
from recipe_normalizer.users.models import User

__all__ = [
    "AuthError",
    "User",
    "display_names_for_ids",
    "get_user_by_email",
    "get_user_by_token",
    "login",
    "logout",
    "purge_expired_sessions",
    "register",
]

_provider = PasswordAuthProvider()

# Pre-computed dummy hash used for timing-safe unknown-email rejection.
_DUMMY_HASH: str = _provider.hash_password("__dummy_constant_password__")

_AUTH_ERROR_MSG = "Invalid email or password."


class AuthError(ApiError):
    """Raised for authentication and registration failures."""

    def __init__(self, message: str = _AUTH_ERROR_MSG) -> None:
        super().__init__(status_code=401, code="auth_error", message=message)


def register(
    db: Session,
    *,
    email: str,
    password: str,
    display_name: str,
) -> User:
    """Create a new user; raises AuthError on duplicate email.

    Never grants admin. Registration is unauthenticated and proves nothing about
    who owns an address, so an email-allow-list here would let anyone who knows
    (or guesses) a listed address self-promote by signing up with it. Admin is
    granted out-of-band only: ``python -m recipe_normalizer.users.make_admin
    <email>``, which requires shell access and an already-existing user.
    """
    normalized = email.strip().lower()
    existing = db.scalars(select(User).where(User.email == normalized)).first()
    if existing is not None:
        raise AuthError("An account with that email already exists.")
    password_hash = _provider.hash_password(password)
    user = User(email=normalized, password_hash=password_hash, display_name=display_name)
    db.add(user)
    db.flush()
    return user


def login(db: Session, *, email: str, password: str) -> tuple[str, User]:
    """Verify credentials, create a session, return (plaintext token, user)."""
    normalized = email.strip().lower()
    user = db.scalars(select(User).where(User.email == normalized)).first()
    if user is None:
        # Timing-safe: always run a verify even on unknown email.
        _provider.verify_password(password, _DUMMY_HASH)
        raise AuthError()
    if not _provider.verify_password(password, user.password_hash):
        raise AuthError()
    token = new_session_token()
    session = DbSession(user_id=user.id, token_hash=hash_token(token))
    db.add(session)
    db.flush()
    return token, user


def logout(db: Session, token: str) -> None:
    """Delete the session for the given plaintext token if it exists."""
    token_h = hash_token(token)
    session = db.scalars(select(DbSession).where(DbSession.token_hash == token_h)).first()
    if session is not None:
        db.delete(session)
        db.flush()


def purge_expired_sessions(db: Session) -> int:
    """Delete expired session rows; returns how many were removed."""
    result = cast(
        "CursorResult[Any]",
        db.execute(delete(DbSession).where(DbSession.expires_at <= datetime.now(UTC))),
    )
    db.flush()
    return int(result.rowcount or 0)


def get_user_by_email(db: Session, email: str) -> User | None:
    """Return the user with the given email (case-insensitive), or None.

    Used by the sharing module to resolve a recipient without it ever
    importing `users.models` directly.
    """
    normalized = email.strip().lower()
    return db.scalars(select(User).where(User.email == normalized)).first()


def display_names_for_ids(db: Session, ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    """Map user ids to their display_name (for callers rendering member/editor names).

    Used by sharing.service to render shared-cookbook member lists and
    per-recipe "last edited by" without it ever importing `users.models`
    directly. Deliberately does NOT expose email — see sharing.service's
    module docstring for why shared-cookbook members only ever see each
    other's display names.
    """
    if not ids:
        return {}
    rows = db.execute(select(User.id, User.display_name).where(User.id.in_(ids))).all()
    return {row.id: row.display_name for row in rows}


def get_user_by_token(db: Session, token: str) -> User | None:
    """Return the user for a valid, non-expired token; None otherwise."""
    token_h = hash_token(token)
    now = datetime.now(UTC)
    result = db.scalars(
        select(User)
        .join(DbSession, DbSession.user_id == User.id)
        .where(DbSession.token_hash == token_h, DbSession.expires_at > now)
    ).first()
    return result
