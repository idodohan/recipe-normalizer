"""shared cookbooks tables

Revision ID: 9a2f6b1c7d3e
Revises: 6135edc0aada
Create Date: 2026-07-09 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9a2f6b1c7d3e"
down_revision: str | Sequence[str] | None = "6135edc0aada"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "shared_cookbooks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_shared_cookbooks_created_by_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_shared_cookbooks")),
    )
    op.create_index(
        op.f("ix_shared_cookbooks_created_by"), "shared_cookbooks", ["created_by"], unique=False
    )

    op.create_table(
        "shared_cookbook_members",
        sa.Column("cookbook_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("added_by", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["cookbook_id"],
            ["shared_cookbooks.id"],
            name=op.f("fk_shared_cookbook_members_cookbook_id_shared_cookbooks"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_shared_cookbook_members_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["added_by"],
            ["users.id"],
            name=op.f("fk_shared_cookbook_members_added_by_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("cookbook_id", "user_id", name=op.f("pk_shared_cookbook_members")),
    )

    op.create_table(
        "shared_cookbook_recipes",
        sa.Column("cookbook_id", sa.Uuid(), nullable=False),
        sa.Column("recipe_id", sa.Uuid(), nullable=False),
        sa.Column("added_by", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["cookbook_id"],
            ["shared_cookbooks.id"],
            name=op.f("fk_shared_cookbook_recipes_cookbook_id_shared_cookbooks"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["recipe_id"],
            ["recipes.id"],
            name=op.f("fk_shared_cookbook_recipes_recipe_id_recipes"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["added_by"],
            ["users.id"],
            name=op.f("fk_shared_cookbook_recipes_added_by_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "cookbook_id", "recipe_id", name=op.f("pk_shared_cookbook_recipes")
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("shared_cookbook_recipes")
    op.drop_table("shared_cookbook_members")
    op.drop_index(op.f("ix_shared_cookbooks_created_by"), table_name="shared_cookbooks")
    op.drop_table("shared_cookbooks")
