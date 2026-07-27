"""widen llm_usage.model for openrouter slugs

Revision ID: 533866b8e4fb
Revises: 1f3c9a4e2b71
Create Date: 2026-07-27 18:26:32.346094

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "533866b8e4fb"
down_revision: str | Sequence[str] | None = "1f3c9a4e2b71"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Widen llm_usage.model so OpenRouter slugs (vendor/model:variant) fit."""
    op.alter_column(
        "llm_usage",
        "model",
        existing_type=sa.String(60),
        type_=sa.String(200),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "llm_usage",
        "model",
        existing_type=sa.String(200),
        type_=sa.String(60),
        existing_nullable=False,
    )
