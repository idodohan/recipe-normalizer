"""cookbook search fts trigram indexes

Revision ID: 6b6111bf2366
Revises: 7b5f9497c55f
Create Date: 2026-07-08 21:55:15.469330

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "6b6111bf2366"
down_revision: str | Sequence[str] | None = "7b5f9497c55f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    # Trigram index on title — powers `similarity(title, q) > 0.25` search.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_recipes_title_trgm ON recipes USING gin (title gin_trgm_ops)"
    )

    # Expression index on the title+description tsvector — powers
    # `to_tsvector('simple', title || ' ' || coalesce(description, ''))
    #  @@ websearch_to_tsquery('simple', q)`.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_recipes_title_description_fts "
        "ON recipes USING gin (to_tsvector('simple', title || ' ' || coalesce(description, '')))"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP INDEX IF EXISTS ix_recipes_title_description_fts")
    op.execute("DROP INDEX IF EXISTS ix_recipes_title_trgm")
    op.execute("DROP EXTENSION IF EXISTS pg_trgm")
