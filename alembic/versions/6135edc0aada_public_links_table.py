"""public_links table

Revision ID: 6135edc0aada
Revises: 723a416673ed
Create Date: 2026-07-09 09:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "6135edc0aada"
down_revision: str | Sequence[str] | None = "723a416673ed"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # recipe_id IS a real foreign key with ON DELETE CASCADE (unlike
    # shares.origin_recipe_id): a public link has no reason to outlive the
    # recipe it points at — see sharing/models.py's PublicLink docstring.
    op.create_table(
        "public_links",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("token", sa.String(length=64), nullable=False),
        sa.Column("recipe_id", sa.Uuid(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["recipe_id"],
            ["recipes.id"],
            name=op.f("fk_public_links_recipe_id_recipes"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_public_links_created_by_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_public_links")),
    )
    op.create_index(op.f("ix_public_links_token"), "public_links", ["token"], unique=True)
    op.create_index(op.f("ix_public_links_recipe_id"), "public_links", ["recipe_id"], unique=False)
    op.create_index(
        op.f("ix_public_links_created_by"), "public_links", ["created_by"], unique=False
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_public_links_created_by"), table_name="public_links")
    op.drop_index(op.f("ix_public_links_recipe_id"), table_name="public_links")
    op.drop_index(op.f("ix_public_links_token"), table_name="public_links")
    op.drop_table("public_links")
