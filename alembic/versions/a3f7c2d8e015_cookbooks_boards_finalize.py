"""cookbooks as boards: backfill the join, fold collections in, drop the scaffolding

Revision ID: a3f7c2d8e015
Revises: b7d3f0c11a94
Create Date: 2026-07-28 18:00:00.000000

The BOARDS pivot's one and only schema migration. Task 1 introduced the
`cookbook_recipes` many-to-many in the ORM models ONLY — deliberately, since
the whole suite builds its schema with ``Base.metadata.create_all`` — and
started DUAL-WRITING every placement into both the join and the legacy
`recipes.cookbook_id`, with reads taking the union of the two. So this revision
both CREATES the join table and retires that scaffolding, folding the
standalone `collections` concept into cookbooks along the way. Five steps, in
this order and no other:

0. **Create `cookbook_recipes`** — `(cookbook_id, recipe_id)` composite PK,
   `added_by` (FK users, SET NULL), `added_at` (default now()), plus the
   standalone `recipe_id` index the composite PK cannot serve. Mirrors
   `cookbook.models.CookbookRecipe` exactly. Because no prior revision created
   it, a real database reaches this revision with NO join rows at all — the
   dual-write only ever landed in the ORM-built test/dev schemas.
1. **Backfill** one join row per recipe from `recipes.cookbook_id`
   (`added_by = recipes.owner_id`). ``ON CONFLICT DO NOTHING`` keeps the step
   idempotent (and covers any environment where the table was created out of
   band by ``create_all`` and dual-written into). After this every recipe has
   >= 1 placement, which is the invariant the later steps and all the
   post-migration code rely on.
2. **Fold collections into cookbooks** (design spec, "Migration" step 2): each
   `collections` row becomes a private, non-default cookbook owned by the
   collection's owner and keeping its name; each `collection_recipes` row
   becomes a `cookbook_recipes` placement into that new cookbook. Only the
   collection OWNER's own recipes are folded in — a collection is owner-scoped
   personal organization, and `set_recipe_collections` only ever let an owner
   file their OWN recipes, so a `collection_recipes` row pointing at someone
   else's recipe would be a pre-existing anomaly; folding it in would silently
   hand the collection's owner an editor-grade placement on a stranger's
   recipe. Counts (including anything skipped) are logged, never silent.
3. **Drop `recipes.cookbook_id`** (its index and FK first). `cookbook_recipes`
   alone now answers "which cookbooks is this recipe in".
4. **Drop `collection_recipes`, then `collections`** (children first).

Steps 0 and 1 are additive and safe to run against live traffic; 3 and 4 are
the destructive pair.

Step 3 is what removes the `recipes -> cookbooks` ON DELETE CASCADE, so from
here on deleting a cookbook can no longer destroy a recipe — the service
(`cookbook.service.delete_cookbook`) rescues a recipe whose ONLY placement was
the doomed cookbook into its owner's default cookbook first.

Like both pivot revisions, every data step runs through ``op.get_bind()`` with
lightweight ``sa.table()`` constructs rather than the ORM models: a migration
has to keep producing the same result years from now, against whatever the
models happen to look like then.

Deploy note: wants a write-quiesced window, same as the pivot revisions. Steps
1 and 3 are a backfill-then-drop pair, and this revision has to land TOGETHER
with the code that stops reading `recipes.cookbook_id` — a recipe INSERTed
between them by still-dual-writing code keeps its join row and is fine, but the
old code cannot run after step 3, and the new code cannot run before step 0.
`DROP COLUMN` takes an ACCESS EXCLUSIVE lock on `recipes` regardless.
"""

import logging
import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import insert as pg_insert

# revision identifiers, used by Alembic.
revision: str = "a3f7c2d8e015"
down_revision: str | Sequence[str] | None = "b7d3f0c11a94"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger("alembic.runtime.migration")

#: Kept in sync with cookbook.service.DEFAULT_COOKBOOK_NAME by convention —
#: duplicated as a literal on purpose (see 4e1b7c9a52d8's module docstring on
#: import stability), exactly as b7d3f0c11a94 does.
DEFAULT_COOKBOOK_NAME = "My Cookbook"

# --- lightweight table handles for the data steps ---------------------------

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
_cookbook_recipes = sa.table(
    "cookbook_recipes",
    sa.column("cookbook_id", sa.Uuid()),
    sa.column("recipe_id", sa.Uuid()),
    sa.column("added_by", sa.Uuid()),
    sa.column("added_at", sa.DateTime(timezone=True)),
)
_collections = sa.table(
    "collections",
    sa.column("id", sa.Uuid()),
    sa.column("owner_id", sa.Uuid()),
    sa.column("name", sa.String()),
)
_collection_recipes = sa.table(
    "collection_recipes",
    sa.column("collection_id", sa.Uuid()),
    sa.column("recipe_id", sa.Uuid()),
)


def upgrade() -> None:
    """Upgrade schema."""
    # Step 0 — the join table itself. Column-for-column the same shape as
    # `cookbook.models.CookbookRecipe`, so `create_all`-built schemas (every
    # test but this module) and migrated ones agree.
    op.create_table(
        "cookbook_recipes",
        sa.Column("cookbook_id", sa.Uuid(), nullable=False),
        sa.Column("recipe_id", sa.Uuid(), nullable=False),
        sa.Column("added_by", sa.Uuid(), nullable=True),
        sa.Column(
            "added_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["added_by"],
            ["users.id"],
            name=op.f("fk_cookbook_recipes_added_by_users"),
            # SET NULL, not CASCADE: the person who added the recipe leaving
            # the system must not silently un-place it from the cookbook.
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["cookbook_id"],
            ["cookbooks.id"],
            name=op.f("fk_cookbook_recipes_cookbook_id_cookbooks"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["recipe_id"],
            ["recipes.id"],
            name=op.f("fk_cookbook_recipes_recipe_id_recipes"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("cookbook_id", "recipe_id", name=op.f("pk_cookbook_recipes")),
    )
    # The composite PK is (cookbook_id, recipe_id), so it cannot serve a bare
    # `WHERE recipe_id = X` — THE access path of the boards model ("which
    # cookbooks is this recipe in", asked on every recipe read).
    op.create_index(
        op.f("ix_cookbook_recipes_recipe_id"), "cookbook_recipes", ["recipe_id"], unique=False
    )

    conn = op.get_bind()

    _backfill_join_rows_from_primary_placement(conn)
    _fold_collections_into_cookbooks(conn)

    # Step 3 — drop the legacy primary placement. Index and FK explicitly
    # first: Postgres would drop both with the column anyway, but naming them
    # keeps the reverse of `downgrade` symmetric and fails loudly if a future
    # revision renamed either.
    op.drop_index(op.f("ix_recipes_cookbook_id"), table_name="recipes")
    op.drop_constraint(op.f("fk_recipes_cookbook_id_cookbooks"), "recipes", type_="foreignkey")
    op.drop_column("recipes", "cookbook_id")

    # Step 4 — collections are cookbooks now. Children first.
    op.drop_table("collection_recipes")
    op.drop_index(op.f("ix_collections_owner_id"), table_name="collections")
    op.drop_table("collections")


def _backfill_join_rows_from_primary_placement(conn: sa.Connection) -> None:
    """One `cookbook_recipes` row per recipe, from its `recipes.cookbook_id`.

    ``added_by = recipes.owner_id``: the recipe's creator is the closest thing
    to "who put this here" that the pre-boards schema recorded. ``added_at``
    comes from the column's ``now()`` server_default (the transaction clock),
    so every backfilled row shares one timestamp — deliberate, and why the
    oldest-placement tie-breaks elsewhere fall through to ``cookbook_id``.

    ``ON CONFLICT DO NOTHING`` because the dual-write has been live since Task
    1, so the overwhelming majority of these rows already exist; the INSERT is
    only reaching the stragglers written before it (and is idempotent if this
    step is ever re-run).
    """
    inserted = len(
        conn.execute(
            pg_insert(_cookbook_recipes)
            .from_select(
                ["cookbook_id", "recipe_id", "added_by"],
                sa.select(_recipes.c.cookbook_id, _recipes.c.id, _recipes.c.owner_id),
            )
            .on_conflict_do_nothing()
            # RETURNING, not ``result.rowcount``: an INSERT..FROM SELECT with
            # ON CONFLICT reports -1 through the driver, so the rows have to be
            # counted explicitly.
            .returning(_cookbook_recipes.c.recipe_id)
        ).all()
    )
    total = conn.execute(sa.select(sa.func.count()).select_from(_recipes)).scalar_one()
    logger.info(
        "boards finalize: %d join row(s) backfilled from recipes.cookbook_id "
        "(%d recipe(s) total; any remainder already had one)",
        inserted,
        total,
    )


def _fold_collections_into_cookbooks(conn: sa.Connection) -> None:
    """Every collection becomes a private, non-default cookbook holding its recipes.

    The collection_id -> new cookbook_id map is built in Python (one uuid4 per
    collection) rather than in SQL: the ids are needed twice — once for the
    `cookbooks` INSERT and once per collection for the placement INSERT — and a
    dict is clearer than a CTE threaded through both.

    ``visibility``/``created_at``/``updated_at`` come from their column
    server_defaults ('private' / now()), same as 4e1b7c9a52d8 and
    b7d3f0c11a94 — deliberately not passed as bind params, since `visibility`
    is a Postgres ENUM and a text parameter would need an explicit cast.

    Placements are filtered to ``recipes.owner_id = collections.owner_id`` (see
    the module docstring) and inserted ``ON CONFLICT DO NOTHING`` — a recipe
    that a collection listed twice, or that is already placed in the brand-new
    cookbook, is a no-op rather than a duplicate-key abort.
    """
    collections = conn.execute(
        sa.select(_collections.c.id, _collections.c.owner_id, _collections.c.name).order_by(
            _collections.c.id
        )
    ).all()
    if not collections:
        logger.info("boards finalize: no collections to fold into cookbooks")
        return

    new_cookbook_id = {row.id: uuid.uuid4() for row in collections}
    conn.execute(
        sa.insert(_cookbooks),
        [
            {
                "id": new_cookbook_id[row.id],
                "owner_id": row.owner_id,
                "name": row.name,
                "is_default": False,
            }
            for row in collections
        ],
    )

    links = conn.execute(sa.select(sa.func.count()).select_from(_collection_recipes)).scalar_one()
    placed = 0
    for row in collections:
        result = conn.execute(
            pg_insert(_cookbook_recipes)
            .from_select(
                ["cookbook_id", "recipe_id", "added_by"],
                sa.select(
                    sa.literal(new_cookbook_id[row.id], type_=sa.Uuid()),
                    _collection_recipes.c.recipe_id,
                    sa.literal(row.owner_id, type_=sa.Uuid()),
                )
                .select_from(
                    _collection_recipes.join(
                        _recipes, _recipes.c.id == _collection_recipes.c.recipe_id
                    )
                )
                .where(
                    _collection_recipes.c.collection_id == row.id,
                    _recipes.c.owner_id == row.owner_id,
                ),
            )
            .on_conflict_do_nothing()
            # RETURNING for the same reason as the backfill above.
            .returning(_cookbook_recipes.c.recipe_id)
        )
        placed += len(result.all())

    logger.info(
        "boards finalize: %d collection(s) folded into cookbooks, %d placement(s) written "
        "from %d collection_recipes link(s) (%d skipped — not the collection owner's own "
        "recipe, or already placed)",
        len(collections),
        placed,
        links,
        links - placed,
    )


def downgrade() -> None:
    """Downgrade schema.

    Reverses the five steps in order. The SCHEMA comes back whole; neither the
    collections DATA nor the secondary placements do:

    * ``collections`` / ``collection_recipes`` are recreated EMPTY. Their rows
      became real cookbooks and placements in ``upgrade``, and a
      collection-derived cookbook is indistinguishable from one the user
      created by hand (same shape, same owner, same name) — exactly the
      trade-off b7d3f0c11a94's downgrade documents for the shared_cookbook*
      tables. The derived cookbooks are deliberately left in place: deleting
      them on the way down would destroy placements a user has since curated.
    * ``recipes.cookbook_id`` is rebuilt from each recipe's OLDEST placement
      (min ``added_at``, ties broken by ``cookbook_id`` — the same
      deterministic ordering the service used for its transitional repoints).
      "Which cookbook was primary before?" was never recorded separately, so
      oldest-wins is the best available reconstruction; a recipe placed in
      several cookbooks loses the distinction between them.

    A recipe with no placement at all would leave the column NULL and abort the
    ``SET NOT NULL``, so the same sweep b7d3f0c11a94 uses runs first — it files
    any such recipe into its owner's default cookbook, creating one when the
    owner has none. Unreachable if the >= 1 placement invariant held, and
    cheap insurance if it didn't.
    """
    op.create_table(
        "collections",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["users.id"],
            name=op.f("fk_collections_owner_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_collections")),
        sa.UniqueConstraint("owner_id", "name", name="uq_collections_owner_id_name"),
    )
    op.create_index(op.f("ix_collections_owner_id"), "collections", ["owner_id"], unique=False)
    op.create_table(
        "collection_recipes",
        sa.Column("collection_id", sa.Uuid(), nullable=False),
        sa.Column("recipe_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["collection_id"],
            ["collections.id"],
            name=op.f("fk_collection_recipes_collection_id_collections"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["recipe_id"],
            ["recipes.id"],
            name=op.f("fk_collection_recipes_recipe_id_recipes"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("collection_id", "recipe_id", name=op.f("pk_collection_recipes")),
    )

    op.add_column("recipes", sa.Column("cookbook_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f("fk_recipes_cookbook_id_cookbooks"),
        "recipes",
        "cookbooks",
        ["cookbook_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(op.f("ix_recipes_cookbook_id"), "recipes", ["cookbook_id"], unique=False)

    conn = op.get_bind()
    _restore_primary_placement_from_oldest_join_row(conn)
    _sweep_placementless_recipes_into_owner_default(conn)

    op.alter_column("recipes", "cookbook_id", existing_type=sa.Uuid(), nullable=False)

    # Last, once every placement has been distilled back into the column: the
    # join table goes. The secondary placements it recorded are NOT recoverable
    # from `recipes.cookbook_id` — a recipe on three boards comes back on one.
    op.drop_index(op.f("ix_cookbook_recipes_recipe_id"), table_name="cookbook_recipes")
    op.drop_table("cookbook_recipes")


def _restore_primary_placement_from_oldest_join_row(conn: sa.Connection) -> None:
    """Point every recipe's rebuilt `cookbook_id` at its oldest placement."""
    oldest = (
        sa.select(_cookbook_recipes.c.cookbook_id)
        .where(_cookbook_recipes.c.recipe_id == _recipes.c.id)
        .order_by(_cookbook_recipes.c.added_at, _cookbook_recipes.c.cookbook_id)
        .limit(1)
        .correlate(_recipes)
        .scalar_subquery()
    )
    result = conn.execute(sa.update(_recipes).values(cookbook_id=oldest))
    logger.info(
        "boards downgrade: recipes.cookbook_id restored from the oldest placement for %d recipe(s)",
        result.rowcount,
    )


def _sweep_placementless_recipes_into_owner_default(conn: sa.Connection) -> None:
    """File any recipe with NO placement at all into its owner's default cookbook.

    A verbatim reprise of b7d3f0c11a94._sweep_remaining_null_cookbooks: owners
    with no default cookbook get one first (otherwise the correlated assignment
    below sets NULL to NULL and the ``SET NOT NULL`` that follows aborts the
    downgrade), then one UPDATE files every straggler.
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
                {
                    "id": uuid.uuid4(),
                    "owner_id": owner_id,
                    "name": DEFAULT_COOKBOOK_NAME,
                    "is_default": True,
                }
                for owner_id in ownerless
            ],
        )

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
    if result.rowcount:
        logger.info(
            "boards downgrade: %d placementless recipe(s) swept into a default cookbook "
            "(%d default cookbook(s) created for owners who had none)",
            result.rowcount,
            len(ownerless),
        )
