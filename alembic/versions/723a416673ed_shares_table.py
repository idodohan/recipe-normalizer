"""shares table

Revision ID: 723a416673ed
Revises: 80f2c4d93e7d
Create Date: 2026-07-08 23:05:18.284588

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "723a416673ed"
down_revision: str | Sequence[str] | None = "80f2c4d93e7d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # origin_recipe_id is deliberately a plain uuid column (NOT a foreign
    # key) — see sharing/models.py's Share docstring: the share row must
    # survive deletion of the original recipe.
    op.create_table(
        "shares",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("origin_recipe_id", sa.Uuid(), nullable=False),
        sa.Column("from_user_id", sa.Uuid(), nullable=False),
        sa.Column("to_user_id", sa.Uuid(), nullable=False),
        sa.Column("copied_recipe_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["from_user_id"],
            ["users.id"],
            name=op.f("fk_shares_from_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["to_user_id"],
            ["users.id"],
            name=op.f("fk_shares_to_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["copied_recipe_id"],
            ["recipes.id"],
            name=op.f("fk_shares_copied_recipe_id_recipes"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_shares")),
    )
    op.create_index(op.f("ix_shares_from_user_id"), "shares", ["from_user_id"], unique=False)
    op.create_index(op.f("ix_shares_to_user_id"), "shares", ["to_user_id"], unique=False)
    op.create_index(
        op.f("ix_shares_copied_recipe_id"), "shares", ["copied_recipe_id"], unique=False
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_shares_copied_recipe_id"), table_name="shares")
    op.drop_index(op.f("ix_shares_to_user_id"), table_name="shares")
    op.drop_index(op.f("ix_shares_from_user_id"), table_name="shares")
    op.drop_table("shares")
