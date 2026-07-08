"""Sharing API routes: copy-on-share."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from recipe_normalizer.api_deps import get_current_user, limit_by_user
from recipe_normalizer.db import get_db
from recipe_normalizer.sharing import service
from recipe_normalizer.sharing.schemas import ShareOut, ShareRecipeIn

router = APIRouter(prefix="/api/share", tags=["sharing"])

_share_limit = limit_by_user("share", 30, 3600.0)


@router.post("/recipe", status_code=201, response_model=ShareOut)
def share_recipe(
    body: ShareRecipeIn,
    db: Session = Depends(get_db),  # noqa: B008
    current_user: Any = Depends(get_current_user),  # noqa: B008
    _: None = Depends(_share_limit),  # noqa: B008
) -> ShareOut:
    return service.share_recipe(
        db,
        from_user_id=current_user.id,
        from_email=current_user.email,
        recipe_id=body.recipe_id,
        to_email=body.to_email,
    )
