"""FastAPI dependency functions shared across routers."""

from collections.abc import Callable

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from recipe_normalizer.db import get_db
from recipe_normalizer.errors import ApiError
from recipe_normalizer.ratelimit import SlidingWindowLimiter
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


def require_admin(user: User = Depends(get_current_user)) -> User:  # noqa: B008
    """Gate for endpoints that mutate global shared data (the catalog)."""
    if not user.is_admin:
        raise ApiError(403, "forbidden", "Admin access required.")
    return user


def _too_many() -> ApiError:
    return ApiError(429, "rate_limited", "Too many requests — try again soon.")


def limit_by_ip(name: str, max_events: int, window_s: float) -> Callable[..., None]:
    """Build a FastAPI dependency that rate-limits by client IP.

    Each call creates its own limiter instance — callers keep a module-level
    reference so distinct endpoints (e.g. login vs. register) never share a
    bucket, and so tests can reach `.limiter.reset()` between cases.
    """
    limiter = SlidingWindowLimiter(max_events, window_s)

    def dep(request: Request) -> None:
        ip = request.client.host if request.client else "unknown"
        if not limiter.check(f"{name}:{ip}"):
            raise _too_many()

    dep.limiter = limiter  # type: ignore[attr-defined]  # test hook
    return dep


def limit_by_user(name: str, max_events: int, window_s: float) -> Callable[..., None]:
    """Build a FastAPI dependency that rate-limits by authenticated user id."""
    limiter = SlidingWindowLimiter(max_events, window_s)

    def dep(user: User = Depends(get_current_user)) -> None:  # noqa: B008
        if not limiter.check(f"{name}:{user.id}"):
            raise _too_many()

    dep.limiter = limiter  # type: ignore[attr-defined]  # test hook
    return dep
