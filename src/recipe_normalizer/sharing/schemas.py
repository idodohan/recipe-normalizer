"""Pydantic schemas for the sharing API."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr, Field


class ShareRecipeIn(BaseModel):
    """Body for POST /api/share/recipe."""

    recipe_id: uuid.UUID
    to_email: EmailStr = Field(max_length=320)


class ShareOut(BaseModel):
    id: uuid.UUID
    copied_recipe_id: uuid.UUID
    to_email: str
    created_at: datetime

    model_config = {"from_attributes": True}
