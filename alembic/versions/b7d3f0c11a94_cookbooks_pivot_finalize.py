"""cookbooks pivot, part 2: finalize (NOT NULL + drop the shared tables)

Revision ID: b7d3f0c11a94
Revises: 4e1b7c9a52d8
Create Date: 2026-07-28 15:00:00.000000

The destructive half of the pivot, deliberately split from the additive
4e1b7c9a52d8 so that every intermediate deploy stays runnable. It may only run
once the application code has (a) stopped writing cookbook-less recipes
(``sharing.copy_recipe`` and the extraction persist path both assign a
cookbook now) and (b) stopped referencing the ``shared_cookbook*`` tables at
all (the whole shared-cookbook feature is gone from `sharing`).

Three steps, in this order and no other:

1. Sweep any recipe still holding a NULL ``cookbook_id`` into its owner's
   default cookbook, creating that default when the owner hasn't got one.
   The additive migration already did this once, but between the two
   revisions the old writers were still live and new users could register
   without ever getting a default — so the sweep has to run again here, or
   step 2 aborts the deploy.
2. ``ALTER recipes.cookbook_id SET NOT NULL`` — "a recipe lives in exactly one
   cookbook" becomes a database invariant, not a convention.
3. Drop ``shared_cookbook_recipes`` → ``shared_cookbook_members`` →
   ``shared_cookbooks`` (children first; each also has ON DELETE CASCADE FKs,
   but the order is explicit rather than implicit).

Like 4e1b7c9a52d8, the data step runs through ``op.get_bind()`` with
lightweight ``sa.table()`` constructs rather than the ORM models: a migration
has to keep producing the same result years from now, against whatever the
models happen to look like then.

Deploy note (unchanged from the additive revision): this wants a
write-quiesced window. Step 1 and step 2 are a read-then-constrain pair, so a
concurrent INSERT of a cookbook-less recipe landing between them would abort
step 2 — and `SET NOT NULL` takes an ACCESS EXCLUSIVE lock on `recipes` while
it verifies the table anyway.
"""

import logging
import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7d3f0c11a94"
down_revision: str | Sequence[str] | None = "4e1b7c9a52d8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger("alembic.runtime.migration")

#: Kept in sync with cookbook.service.DEFAULT_COOKBOOK_NAME by convention —
#: duplicated as a literal on purpose (see module docstring on import
#: stability), exactly as 4e1b7c9a52d8 does.
DEFAULT_COOKBOOK_NAME = "My Cookbook"

# --- lightweight table handles for the data step ----------------------------

_recipes = sa.table(
    "recipes",
    sa.column("id", sa.Uuid()),
    sa.column("owner_id", sa.Uuid()),
    sa.column("cookbook_id", sa.Uuid()),
)
_cookbooks = sa.table(
    "cookbooks",
    sa.column("id", sa.Uuid()),
    sa.column("owner_id", sa.Uuid()),
    sa.column("name", sa.String()),
    sa.column("is_default", sa.Boolean()),
)


def upgrade() -> None:
    """Upgrade schema."""
    conn = op.get_bind()
    _sweep_remaining_null_cookbooks(conn)

    op.alter_column("recipes", "cookbook_id", existing_type=sa.Uuid(), nullable=False)

    op.drop_table("shared_cookbook_recipes")
    op.drop_table("shared_cookbook_members")
    op.drop_index(op.f("ix_shared_cookbooks_created_by"), table_name="shared_cookbooks")
    op.drop_table("shared_cookbooks")


def _sweep_remaining_null_cookbooks(conn: sa.Connection) -> None:
    """File every still-cookbook-less recipe in its owner's default cookbook.

    Owners who have no default cookbook get one first — otherwise the
    correlated assignment below would set NULL to NULL for their recipes and
    the ``SET NOT NULL`` that follows would fail. Only owners of an
    unassigned recipe are given one: an account with no recipes needs no
    landing place until it creates one, and `ensure_default_cookbook` makes it
    lazily at that point.
    """
    ownerless = list(
        conn.scalars(
            sa.select(_recipes.c.owner_id)
            .where(
                _recipes.c.cookbook_id.is_(None),
                ~sa.exists(
                    sa.select(sa.literal(1)).where(
                        _cookbooks.c.owner_id == _recipes.c.owner_id,
                        _cookbooks.c.is_default.is_(True),
                    )
                ),
            )
            .distinct()
            .order_by(_recipes.c.owner_id)
        )
    )
    if ownerless:
        conn.execute(
            sa.insert(_cookbooks),
            [
                # visibility / created_at / updated_at come from their column
                # server_defaults, exactly as in 4e1b7c9a52d8.
                {
                    "id": uuid.uuid4(),
                    "owner_id": owner_id,
                    "name": DEFAULT_COOKBOOK_NAME,
                    "is_default": True,
                }
                for owner_id in ownerless
            ],
        )

    # Same correlated assignment 4e1b7c9a52d8._assign_remaining_to_owner_default
    # uses — one UPDATE, no per-row round trip.
    result = conn.execute(
        sa.update(_recipes)
        .where(_recipes.c.cookbook_id.is_(None))
        .values(
            cookbook_id=sa.select(_cookbooks.c.id)
            .where(
                _cookbooks.c.owner_id == _recipes.c.owner_id,
                _cookbooks.c.is_default.is_(True),
            )
            .scalar_subquery()
        )
    )
    logger.info(
        "cookbooks finalize: %d straggler recipe(s) swept into a default cookbook "
        "(%d default cookbook(s) created for owners who had none)",
        result.rowcount,
        len(ownerless),
    )


def downgrade() -> None:
    """Downgrade schema.

    SCHEMA-ONLY, and deliberately so: the ``shared_cookbook*`` tables come
    back EMPTY. Their contents were converted into real cookbooks and
    membership rows by 4e1b7c9a52d8 and are not re-derivable from the new
    tables (a derived cookbook is indistinguishable from one a user created
    by hand, and the synthesized editor rows are indistinguishable from real
    memberships — by design). What this restores is enough for the previous
    revision's code to boot and serve: the tables exist, and cookbook_id is
    nullable again.

    Recipes keep the cookbook they were swept into by the upgrade; nothing
    reverts them to NULL, since "which cookbook was this in before?" is not
    recorded anywhere.
    """
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
            ["added_by"],
            ["users.id"],
            name=op.f("fk_shared_cookbook_members_added_by_users"),
            ondelete="CASCADE",
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
            ["added_by"],
            ["users.id"],
            name=op.f("fk_shared_cookbook_recipes_added_by_users"),
            ondelete="CASCADE",
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
        sa.PrimaryKeyConstraint(
            "cookbook_id", "recipe_id", name=op.f("pk_shared_cookbook_recipes")
        ),
    )

    op.alter_column("recipes", "cookbook_id", existing_type=sa.Uuid(), nullable=True)
