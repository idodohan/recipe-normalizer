from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from recipe_normalizer.config import settings
from recipe_normalizer.errors import ApiError
from recipe_normalizer.users.auth import (
    PasswordAuthProvider,
    hash_token,
    new_session_token,
)
from recipe_normalizer.users.models import Session as DbSession
from recipe_normalizer.users.models import User

__all__ = ["AuthError", "User", "get_user_by_token", "login", "logout", "register"]

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
    """Create a new user; raises AuthError on duplicate email."""
    normalized = email.strip().lower()
    existing = db.scalars(select(User).where(User.email == normalized)).first()
    if existing is not None:
        raise AuthError("An account with that email already exists.")
    password_hash = _provider.hash_password(password)
    user = User(email=normalized, password_hash=password_hash, display_name=display_name)
    admin_emails = {e.strip().lower() for e in settings.admin_emails.split(",") if e.strip()}
    if user.email.lower() in admin_emails:
        user.is_admin = True
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
