"""FastAPI dependency functions shared across routers."""

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from recipe_normalizer.db import get_db
from recipe_normalizer.errors import ApiError
from recipe_normalizer.users.service import User, get_user_by_token


def get_current_user(
    request: Request,
    db: Session = Depends(get_db),  # noqa: B008
) -> User:
    """Read the session cookie and return the authenticated User.

    Raises ApiError(401) when the cookie is absent or the token is invalid.
    """
    token = request.cookies.get("session")
    if not token:
        raise ApiError(401, "unauthorized", "Not signed in.")
    user = get_user_by_token(db, token)
    if user is None:
        raise ApiError(401, "unauthorized", "Not signed in.")
    return user
