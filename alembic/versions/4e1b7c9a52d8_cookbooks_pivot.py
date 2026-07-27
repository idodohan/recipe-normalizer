"""cookbooks pivot (additive, data-preserving)

Revision ID: 4e1b7c9a52d8
Revises: 533866b8e4fb
Create Date: 2026-07-28 09:00:00.000000

Deliberately ADDITIVE ONLY. It creates `cookbooks` / `cookbook_members`, adds
`recipes.cookbook_id`, and moves every existing recipe (plus all
shared-cookbook data) into the new world — but it does NOT flip
`recipes.cookbook_id` to NOT NULL and does NOT drop the `shared_cookbook*`
tables. Both of those wait for a follow-up migration, because at this
revision there is still application code that inserts NULL-cookbook recipes
(sharing.copy_recipe, extraction persist) and code that reads the old shared
tables. Dropping/tightening here would break every intermediate deploy.

All data steps run through ``op.get_bind()`` with lightweight ``sa.table()``
constructs rather than the ORM models: a migration has to keep producing the
same result years from now, against whatever the models happen to look like
then.
"""

import logging
import uuid
from collections.abc import Sequence
from typing import NamedTuple

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4e1b7c9a52d8"
down_revision: str | Sequence[str] | None = "533866b8e4fb"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger("alembic.runtime.migration")

#: Kept in sync with cookbook.service.DEFAULT_COOKBOOK_NAME by convention —
#: duplicated as a literal on purpose (see module docstring on import stability).
DEFAULT_COOKBOOK_NAME = "My Cookbook"

_VISIBILITY = sa.Enum("private", "unlisted", "public", name="cookbookvisibility")
_ROLE = sa.Enum("editor", "viewer", name="cookbookrole")


class _Claim(NamedTuple):
    """One recipe moved into a shared-derived cookbook, plus who owns it."""

    cookbook_id: uuid.UUID
    recipe_id: uuid.UUID
    owner_id: uuid.UUID


class _SharedStats(NamedTuple):
    """Counts for the one-line summary the migration logs on the way out."""

    cookbooks: int
    members: int
    synthesized_members: int
    recipes_claimed: int
    contested_links: int


# --- lightweight table handles for the data steps ---------------------------

_users = sa.table("users", sa.column("id", sa.Uuid()))
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
_cookbook_members = sa.table(
    "cookbook_members",
    sa.column("cookbook_id", sa.Uuid()),
    sa.column("user_id", sa.Uuid()),
    sa.column("role", _ROLE),
    sa.column("added_by", sa.Uuid()),
)
_shared_cookbooks = sa.table(
    "shared_cookbooks",
    sa.column("id", sa.Uuid()),
    sa.column("name", sa.String()),
    sa.column("created_by", sa.Uuid()),
    sa.column("created_at", sa.DateTime(timezone=True)),
)
_shared_members = sa.table(
    "shared_cookbook_members",
    sa.column("cookbook_id", sa.Uuid()),
    sa.column("user_id", sa.Uuid()),
    sa.column("added_by", sa.Uuid()),
)
_shared_recipes = sa.table(
    "shared_cookbook_recipes",
    sa.column("cookbook_id", sa.Uuid()),
    sa.column("recipe_id", sa.Uuid()),
)


def upgrade() -> None:
    """Upgrade schema."""
    _create_schema()
    _migrate_data()


def _create_schema() -> None:
    op.create_table(
        "cookbooks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("cover_image_ref", sa.String(length=500), nullable=True),
        sa.Column("visibility", _VISIBILITY, server_default="private", nullable=False),
        sa.Column("public_token", sa.String(length=64), nullable=True),
        sa.Column("is_default", sa.Boolean(), server_default="false", nullable=False),
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
            ["owner_id"], ["users.id"], name=op.f("fk_cookbooks_owner_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cookbooks")),
        sa.UniqueConstraint("public_token", name=op.f("uq_cookbooks_public_token")),
    )
    op.create_index(op.f("ix_cookbooks_owner_id"), "cookbooks", ["owner_id"], unique=False)
    # DB backstop for cookbook.service.ensure_default_cookbook, whose
    # read-then-insert can race two concurrent requests into two default
    # cookbooks for one user. Deliberately NOT mirrored in the ORM model
    # (it is an invariant, not something the mapper needs to know about), so
    # `alembic revision --autogenerate` will propose dropping it — like the
    # trigram/FTS indexes from 6b6111bf2366, that proposal must be discarded.
    op.create_index(
        "uq_cookbooks_default_per_owner",
        "cookbooks",
        ["owner_id"],
        unique=True,
        postgresql_where=sa.text("is_default"),
    )

    op.create_table(
        "cookbook_members",
        sa.Column("cookbook_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("role", _ROLE, nullable=False),
        sa.Column("added_by", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["added_by"],
            ["users.id"],
            name=op.f("fk_cookbook_members_added_by_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["cookbook_id"],
            ["cookbooks.id"],
            name=op.f("fk_cookbook_members_cookbook_id_cookbooks"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_cookbook_members_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("cookbook_id", "user_id", name=op.f("pk_cookbook_members")),
    )
    op.create_index("ix_cookbook_members_user_id", "cookbook_members", ["user_id"], unique=False)

    # NULLABLE on purpose — see module docstring. The follow-up migration
    # flips it once no writer can produce a cookbook-less recipe.
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


def _migrate_data() -> None:
    """Move every recipe — and all shared-cookbook state — into the new tables.

    Order matters: shared-derived cookbooks claim their recipes first, and
    only then does the owner's default cookbook absorb whatever is left. Doing
    it the other way round would need a second pass to overwrite the defaults.
    """
    conn = op.get_bind()
    defaults = _create_default_cookbooks(conn)
    shared = _migrate_shared_cookbooks(conn)
    _assign_remaining_to_owner_default(conn)
    logger.info(
        "cookbooks pivot: %d default cookbook(s) created; %d shared cookbook(s) converted; "
        "%d member row(s) written (%d synthesized to keep recipe owners' access); "
        "%d recipe(s) claimed by a shared-derived cookbook; %d contested link(s) dropped",
        len(defaults),
        shared.cookbooks,
        shared.members,
        shared.synthesized_members,
        shared.recipes_claimed,
        shared.contested_links,
    )


def _create_default_cookbooks(conn: sa.Connection) -> dict[uuid.UUID, uuid.UUID]:
    """One private, is_default "My Cookbook" per user → {user_id: cookbook_id}.

    Every user gets one, not just recipe owners: the app assumes a landing
    place exists for the next recipe a user adds, and an empty cookbook costs
    a row.
    """
    user_ids = list(conn.scalars(sa.select(_users.c.id).order_by(_users.c.id)))
    if not user_ids:
        return {}

    defaults = {user_id: uuid.uuid4() for user_id in user_ids}
    conn.execute(
        sa.insert(_cookbooks),
        [
            # `visibility` is omitted so the column's server_default
            # ('private') supplies it; same for created_at/updated_at.
            {
                "id": cookbook_id,
                "owner_id": user_id,
                "name": DEFAULT_COOKBOOK_NAME,
                "is_default": True,
            }
            for user_id, cookbook_id in defaults.items()
        ],
    )
    return defaults


def _migrate_shared_cookbooks(conn: sa.Connection) -> _SharedStats:
    """Each `shared_cookbooks` row becomes a real, non-default cookbook.

    Recipes are claimed *before* the membership rows are written, because who
    needs to be a member depends on which recipes ended up here — see
    ``_build_member_rows``.
    """
    shared = conn.execute(
        sa.select(
            _shared_cookbooks.c.id, _shared_cookbooks.c.name, _shared_cookbooks.c.created_by
        ).order_by(_shared_cookbooks.c.created_at, _shared_cookbooks.c.id)
    ).all()
    if not shared:
        return _SharedStats(0, 0, 0, 0, 0)

    new_id = {row.id: uuid.uuid4() for row in shared}
    creator = {row.id: row.created_by for row in shared}
    #: new cookbook id -> its owner, for the synthesized rows below
    owner_of = {new_id[row.id]: row.created_by for row in shared}

    conn.execute(
        sa.insert(_cookbooks),
        [
            {
                "id": new_id[row.id],
                "owner_id": row.created_by,
                # shared_cookbooks.name is String(120); cookbooks.name is
                # String(200), so every existing name fits untruncated.
                "name": row.name,
                "is_default": False,
            }
            for row in shared
        ],
    )

    claims, contested = _claim_shared_recipes(conn, new_id)
    member_rows, synthesized = _build_member_rows(conn, new_id, creator, owner_of, claims)
    if member_rows:
        conn.execute(sa.insert(_cookbook_members), member_rows)

    return _SharedStats(
        cookbooks=len(shared),
        members=len(member_rows),
        synthesized_members=synthesized,
        recipes_claimed=len(claims),
        contested_links=contested,
    )


def _claim_shared_recipes(
    conn: sa.Connection, new_id: dict[uuid.UUID, uuid.UUID]
) -> tuple[list[_Claim], int]:
    """Point each shared recipe at its shared-derived cookbook (first wins).

    A recipe could sit in several shared cookbooks at once, but a recipe now
    lives in exactly one cookbook — so the oldest shared cookbook holding it
    wins and the rest are logged. In practice this is rare: sharing was
    copy-on-share, so the rows are mostly distinct copies already.

    Returns the claims made (each carrying the recipe's *owner*, which
    ``_build_member_rows`` needs) and the count of dropped links.
    """
    links = conn.execute(
        sa.select(_shared_recipes.c.cookbook_id, _shared_recipes.c.recipe_id, _recipes.c.owner_id)
        .select_from(
            _shared_recipes.join(
                _shared_cookbooks, _shared_recipes.c.cookbook_id == _shared_cookbooks.c.id
            ).join(_recipes, _shared_recipes.c.recipe_id == _recipes.c.id)
        )
        .order_by(
            _shared_cookbooks.c.created_at, _shared_cookbooks.c.id, _shared_recipes.c.recipe_id
        )
    ).all()

    seen: set[uuid.UUID] = set()
    claims: list[_Claim] = []
    contested = 0
    for row in links:
        if row.recipe_id in seen:
            contested += 1
            continue
        seen.add(row.recipe_id)
        claims.append(_Claim(new_id[row.cookbook_id], row.recipe_id, row.owner_id))

    if contested:
        logger.warning(
            "cookbooks pivot: %d recipe/shared-cookbook link(s) dropped — those recipes "
            "were in more than one shared cookbook; the oldest cookbook kept them",
            contested,
        )
    if claims:
        conn.execute(
            sa.update(_recipes)
            .where(_recipes.c.id == sa.bindparam("b_recipe_id", type_=sa.Uuid()))
            .values(cookbook_id=sa.bindparam("b_cookbook_id", type_=sa.Uuid())),
            [
                {"b_recipe_id": claim.recipe_id, "b_cookbook_id": claim.cookbook_id}
                for claim in claims
            ],
        )
    return claims, contested


def _build_member_rows(
    conn: sa.Connection,
    new_id: dict[uuid.UUID, uuid.UUID],
    creator: dict[uuid.UUID, uuid.UUID],
    owner_of: dict[uuid.UUID, uuid.UUID],
    claims: list[_Claim],
) -> tuple[list[dict[str, object]], int]:
    """Membership for the derived cookbooks: carried over, plus recipe owners.

    Two sources, in this order (so the real audit trail always wins over a
    synthesized row for the same person):

    1. ``shared_cookbook_members`` → ``editor`` (the old model had no roles;
       every member could edit), with ``added_by`` preserved — minus the
       creator's own self-membership row, since in the new model the owner is
       implicit and never has a `cookbook_members` row (see
       cookbook.service.list_my_cookbooks).
    2. **The owner of every recipe claimed into the cookbook.** Access is now
       derived from the cookbook, not from `recipes.owner_id`, so a recipe
       whose owner is *not* a member of the shared cookbook holding it would
       land in a private cookbook its own owner cannot read — every
       get/update/delete/move would 404 for them while the owner-scoped
       recipe list still showed it. That is reachable in the old data:
       `sharing.remove_member` deletes the membership row but leaves the
       recipe link behind. Synthesizing an ``editor`` row (``added_by`` = the
       cookbook owner) preserves both the access and the shared placement;
       the alternative — pulling the recipe back to the owner's default
       cookbook — would silently revoke it from everyone else.
    """
    members = conn.execute(
        sa.select(
            _shared_members.c.cookbook_id, _shared_members.c.user_id, _shared_members.c.added_by
        ).order_by(_shared_members.c.cookbook_id, _shared_members.c.user_id)
    ).all()

    rows: list[dict[str, object]] = []
    seen: set[tuple[uuid.UUID, uuid.UUID]] = set()

    def _add(cookbook_id: uuid.UUID, user_id: uuid.UUID, added_by: uuid.UUID | None) -> bool:
        if user_id == owner_of[cookbook_id] or (cookbook_id, user_id) in seen:
            return False
        seen.add((cookbook_id, user_id))
        rows.append(
            {
                "cookbook_id": cookbook_id,
                "user_id": user_id,
                "role": "editor",
                "added_by": added_by,
            }
        )
        return True

    for row in members:
        if row.user_id == creator[row.cookbook_id]:
            continue
        _add(new_id[row.cookbook_id], row.user_id, row.added_by)

    synthesized = 0
    # `claims` is already in deterministic order, so the synthesized rows are too.
    for claim in claims:
        if _add(claim.cookbook_id, claim.owner_id, owner_of[claim.cookbook_id]):
            synthesized += 1

    return rows, synthesized


def _assign_remaining_to_owner_default(conn: sa.Connection) -> None:
    """Every still-unassigned recipe lands in its owner's default cookbook."""
    conn.execute(
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


def downgrade() -> None:
    """Downgrade schema.

    A pure schema reverse: the backfilled rows are derived data, so there is
    nothing to restore — the `shared_cookbook*` tables this migration read
    from were never touched and still hold the original state.

    That is only true for a database that has not served traffic since the
    upgrade, though. Once the app has run, dropping `recipes.cookbook_id`
    discards every placement made *after* the migration — recipes filed into
    cookbooks created since, and any recipe moved between cookbooks — and
    none of that is re-derivable from the old shared tables. Downgrading a
    live database therefore loses real user data, not just derived data.
    """
    op.drop_index(op.f("ix_recipes_cookbook_id"), table_name="recipes")
    op.drop_constraint(op.f("fk_recipes_cookbook_id_cookbooks"), "recipes", type_="foreignkey")
    op.drop_column("recipes", "cookbook_id")

    op.drop_index("ix_cookbook_members_user_id", table_name="cookbook_members")
    op.drop_table("cookbook_members")

    op.drop_index(
        "uq_cookbooks_default_per_owner",
        table_name="cookbooks",
        postgresql_where=sa.text("is_default"),
    )
    op.drop_index(op.f("ix_cookbooks_owner_id"), table_name="cookbooks")
    op.drop_table("cookbooks")

    # Drop the native PG enum types created by upgrade().
    _ROLE.drop(op.get_bind(), checkfirst=True)
    _VISIBILITY.drop(op.get_bind(), checkfirst=True)
