"""Auth routes: register, login, logout, /me."""

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.orm import Session

from recipe_normalizer.api_deps import get_current_user
from recipe_normalizer.config import settings
from recipe_normalizer.db import get_db
from recipe_normalizer.users import service
from recipe_normalizer.users.schemas import LoginIn, RegisterIn, UserOut

router = APIRouter(prefix="/api/auth", tags=["auth"])

_SESSION_COOKIE = "session"


@router.post("/register", status_code=201, response_model=UserOut)
def register(body: RegisterIn, db: Session = Depends(get_db)) -> UserOut:  # noqa: B008
    user = service.register(
        db,
        email=body.email,
        password=body.password,
        display_name=body.display_name,
    )
    return UserOut.model_validate(user)


@router.post("/login", status_code=200, response_model=UserOut)
def login(body: LoginIn, response: Response, db: Session = Depends(get_db)) -> UserOut:  # noqa: B008
    token, user = service.login(db, email=body.email, password=body.password)
    response.set_cookie(
        key=_SESSION_COOKIE,
        value=token,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        path="/",
        max_age=settings.session_ttl_hours * 3600,
    )
    return UserOut.model_validate(user)


@router.post("/logout", status_code=204)
def logout(request: Request, response: Response, db: Session = Depends(get_db)) -> None:  # noqa: B008
    token = request.cookies.get(_SESSION_COOKIE)
    if token:
        service.logout(db, token)
    response.delete_cookie(key=_SESSION_COOKIE, path="/")


@router.get("/me", response_model=UserOut)
def me(user: service.User = Depends(get_current_user)) -> UserOut:  # noqa: B008
    return UserOut.model_validate(user)
