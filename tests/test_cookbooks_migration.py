"""Alembic migration test for the additive cookbooks-pivot revision.

Every other test in the suite builds its schema with
``Base.metadata.create_all`` (model-driven), so the alembic scripts get zero
coverage there — and a *data-preserving* migration is exactly the kind of
thing create_all can never exercise. This module is therefore the one place
that actually runs alembic:

* a throwaway database is created on the session-scoped test Postgres
  container (``pg_url`` from conftest) for each test, and dropped after;
* ``alembic/env.py`` takes its URL from ``recipe_normalizer.config.settings``
  (it overwrites whatever ``sqlalchemy.url`` the Config carries), so the
  fixture monkeypatches ``settings.database_url`` to point at that throwaway
  database before invoking ``alembic.command``;
* the pre-migration state is seeded at the *previous* head with plain Core
  inserts (no ORM — the ORM models already describe the post-migration
  world), then ``upgrade head`` runs the real thing and the assertions look
  at raw rows.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, make_url, text
from sqlalchemy.exc import IntegrityError

from recipe_normalizer.config import settings

REPO_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"

#: Revision immediately before the cookbooks pivot — the state a production
#: database is in before this migration runs.
PREV_HEAD = "533866b8e4fb"
#: The cookbooks-pivot revision under test.
PIVOT_REV = "4e1b7c9a52d8"


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


@pytest.fixture()
def migration_engine(pg_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[Engine]:
    """A pristine, empty database on the test container, wired up for alembic.

    CREATE/DROP DATABASE cannot run inside a transaction, hence the
    AUTOCOMMIT admin engine against the container's default database.
    """
    server_url = make_url(pg_url)
    db_name = f"mig_{uuid.uuid4().hex[:12]}"
    admin = create_engine(server_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{db_name}"'))

    url = server_url.set(database=db_name).render_as_string(hide_password=False)
    # env.py does `config.set_main_option("sqlalchemy.url", settings.database_url)`,
    # so this — not the Config — is what actually decides where migrations run.
    monkeypatch.setattr(settings, "database_url", url)

    eng = create_engine(url)
    try:
        yield eng
    finally:
        eng.dispose()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)'))
        admin.dispose()


def _alembic_config(engine: Engine) -> Config:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", engine.url.render_as_string(hide_password=False))
    return cfg


def _upgrade(engine: Engine, revision: str) -> None:
    command.upgrade(_alembic_config(engine), revision)


def _downgrade(engine: Engine, revision: str) -> None:
    command.downgrade(_alembic_config(engine), revision)


# ---------------------------------------------------------------------------
# Pre-migration seed data (Core only — the ORM models describe the *post*-
# migration schema and would not match the tables that exist at PREV_HEAD).
# ---------------------------------------------------------------------------

users_t = sa.table(
    "users",
    sa.column("id", sa.Uuid()),
    sa.column("email", sa.String()),
    sa.column("password_hash", sa.String()),
    sa.column("display_name", sa.String()),
)
recipes_t = sa.table(
    "recipes",
    sa.column("id", sa.Uuid()),
    sa.column("owner_id", sa.Uuid()),
    sa.column("title", sa.String()),
    sa.column("source_type", sa.Enum("web", "pdf", "image", "text", "manual", name="sourcetype")),
    sa.column("cookbook_id", sa.Uuid()),
)
shared_cookbooks_t = sa.table(
    "shared_cookbooks",
    sa.column("id", sa.Uuid()),
    sa.column("name", sa.String()),
    sa.column("created_by", sa.Uuid()),
)
shared_members_t = sa.table(
    "shared_cookbook_members",
    sa.column("cookbook_id", sa.Uuid()),
    sa.column("user_id", sa.Uuid()),
    sa.column("added_by", sa.Uuid()),
)
shared_recipes_t = sa.table(
    "shared_cookbook_recipes",
    sa.column("cookbook_id", sa.Uuid()),
    sa.column("recipe_id", sa.Uuid()),
    sa.column("added_by", sa.Uuid()),
)

ALICE = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
BOB = uuid.UUID("00000000-0000-0000-0000-0000000000b0")
CAROL = uuid.UUID("00000000-0000-0000-0000-0000000000ca")
R_ALICE_SOLO = uuid.UUID("00000000-0000-0000-0000-0000000000e1")
R_ALICE_SHARED = uuid.UUID("00000000-0000-0000-0000-0000000000e2")
R_BOB_SHARED = uuid.UUID("00000000-0000-0000-0000-0000000000e3")
SHARED_CB = uuid.UUID("00000000-0000-0000-0000-00000000005c")


def _seed_pre_migration_state(engine: Engine) -> None:
    """Alice + Bob own recipes, Carol owns none; one shared cookbook.

    The shared cookbook is Alice's ("Family"), holds one recipe of hers and
    one of Bob's, and — as ``sharing.service.create_shared_cookbook`` really
    does — carries a self-membership row for its creator alongside Bob's.
    """
    with engine.begin() as conn:
        conn.execute(
            sa.insert(users_t),
            [
                {
                    "id": user_id,
                    "email": f"{name}@example.com",
                    "password_hash": "x",
                    "display_name": name.title(),
                }
                for user_id, name in ((ALICE, "alice"), (BOB, "bob"), (CAROL, "carol"))
            ],
        )
        conn.execute(
            sa.insert(recipes_t),
            [
                {
                    "id": R_ALICE_SOLO,
                    "owner_id": ALICE,
                    "title": "Alice solo",
                    "source_type": "manual",
                },
                {
                    "id": R_ALICE_SHARED,
                    "owner_id": ALICE,
                    "title": "Alice shared",
                    "source_type": "manual",
                },
                {
                    "id": R_BOB_SHARED,
                    "owner_id": BOB,
                    "title": "Bob shared",
                    "source_type": "manual",
                },
            ],
        )
        conn.execute(
            sa.insert(shared_cookbooks_t),
            [{"id": SHARED_CB, "name": "Family", "created_by": ALICE}],
        )
        conn.execute(
            sa.insert(shared_members_t),
            [
                {"cookbook_id": SHARED_CB, "user_id": ALICE, "added_by": ALICE},
                {"cookbook_id": SHARED_CB, "user_id": BOB, "added_by": ALICE},
            ],
        )
        conn.execute(
            sa.insert(shared_recipes_t),
            [
                {"cookbook_id": SHARED_CB, "recipe_id": R_ALICE_SHARED, "added_by": ALICE},
                {"cookbook_id": SHARED_CB, "recipe_id": R_BOB_SHARED, "added_by": ALICE},
            ],
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_backfill_preserves_every_recipe_and_shared_cookbook(migration_engine: Engine) -> None:
    """The full pre-state → post-state data-preservation contract."""
    _upgrade(migration_engine, PREV_HEAD)
    _seed_pre_migration_state(migration_engine)
    _upgrade(migration_engine, "head")

    with migration_engine.connect() as conn:
        # 1. No recipe was left behind.
        orphans = conn.execute(
            text("select count(*) from recipes where cookbook_id is null")
        ).scalar_one()
        assert orphans == 0

        # 2. Every user — including Carol, who owns no recipes — got exactly
        #    one default cookbook, private and named "My Cookbook".
        defaults = conn.execute(
            text(
                "select owner_id, name, visibility::text, count(*) over () as total "
                "from cookbooks where is_default"
            )
        ).all()
        assert len(defaults) == 3
        assert {row.owner_id for row in defaults} == {ALICE, BOB, CAROL}
        assert {row.name for row in defaults} == {"My Cookbook"}
        assert {row.visibility for row in defaults} == {"private"}

        # 3. The shared cookbook became a real cookbook owned by its creator.
        derived = conn.execute(
            text(
                "select id, owner_id, is_default, visibility::text as visibility "
                "from cookbooks where name = 'Family'"
            )
        ).one()
        assert derived.owner_id == ALICE
        assert derived.is_default is False
        assert derived.visibility == "private"

        # 4. Members carried over as editors, audit trail intact. The creator
        #    is deliberately NOT a member row — in the new model the owner is
        #    implicit (see cookbook/service.list_my_cookbooks).
        members = conn.execute(
            text(
                "select user_id, role::text as role, added_by from cookbook_members "
                "where cookbook_id = :cb"
            ),
            {"cb": derived.id},
        ).all()
        assert [(m.user_id, m.role, m.added_by) for m in members] == [(BOB, "editor", ALICE)]

        # 5. Recipes land where they should: shared ones (whoever owns them)
        #    in the derived cookbook, the rest in their owner's default.
        placement = dict(
            conn.execute(text("select id, cookbook_id from recipes")).all()  # type: ignore[arg-type]
        )
        alice_default = conn.execute(
            text("select id from cookbooks where is_default and owner_id = :o"), {"o": ALICE}
        ).scalar_one()
        assert placement[R_ALICE_SHARED] == derived.id
        assert placement[R_BOB_SHARED] == derived.id
        assert placement[R_ALICE_SOLO] == alice_default

        # 6. The old shared_cookbook* tables are still here — Task 9 drops
        #    them, only after the code that reads them is gone.
        for table in (
            "shared_cookbooks",
            "shared_cookbook_members",
            "shared_cookbook_recipes",
        ):
            assert conn.execute(
                text("select to_regclass(:t) is not null"), {"t": table}
            ).scalar_one(), f"{table} must survive this migration"

        # 7. recipes.cookbook_id stays NULLABLE until Task 9 fixes the writers.
        nullable = conn.execute(
            text(
                "select is_nullable from information_schema.columns "
                "where table_name = 'recipes' and column_name = 'cookbook_id'"
            )
        ).scalar_one()
        assert nullable == "YES"


def test_partial_unique_index_blocks_a_second_default_cookbook(migration_engine: Engine) -> None:
    """The DB backstop behind ensure_default_cookbook's read-then-insert race."""
    _upgrade(migration_engine, "head")

    cookbooks_t = sa.table(
        "cookbooks",
        sa.column("id", sa.Uuid()),
        sa.column("owner_id", sa.Uuid()),
        sa.column("name", sa.String()),
        sa.column("is_default", sa.Boolean()),
    )
    with migration_engine.begin() as conn:
        conn.execute(
            sa.insert(users_t),
            [
                {
                    "id": ALICE,
                    "email": "alice@example.com",
                    "password_hash": "x",
                    "display_name": "Alice",
                }
            ],
        )
        conn.execute(
            sa.insert(cookbooks_t),
            [{"id": uuid.uuid4(), "owner_id": ALICE, "name": "My Cookbook", "is_default": True}],
        )
        # A non-default second cookbook for the same owner is fine...
        conn.execute(
            sa.insert(cookbooks_t),
            [{"id": uuid.uuid4(), "owner_id": ALICE, "name": "Baking", "is_default": False}],
        )

    with pytest.raises(IntegrityError), migration_engine.begin() as conn:
        # ...a second *default* one is not.
        conn.execute(
            sa.insert(cookbooks_t),
            [{"id": uuid.uuid4(), "owner_id": ALICE, "name": "Dupe", "is_default": True}],
        )


def test_upgrade_downgrade_upgrade_round_trips(migration_engine: Engine) -> None:
    """Reversible on an empty DB: head → -1 → head leaves no debris behind.

    A leaked enum type or index would make the second upgrade fail, so the
    round trip completing at all is most of the assertion.
    """
    _upgrade(migration_engine, "head")
    _downgrade(migration_engine, "-1")

    with migration_engine.connect() as conn:
        for table in ("cookbooks", "cookbook_members"):
            assert (
                conn.execute(text("select to_regclass(:t)"), {"t": table}).scalar_one() is None
            ), f"{table} should be gone after downgrade"
        assert (
            conn.execute(
                text(
                    "select count(*) from information_schema.columns "
                    "where table_name = 'recipes' and column_name = 'cookbook_id'"
                )
            ).scalar_one()
            == 0
        )
        for enum_name in ("cookbookvisibility", "cookbookrole"):
            assert (
                conn.execute(
                    text("select count(*) from pg_type where typname = :t"), {"t": enum_name}
                ).scalar_one()
                == 0
            ), f"enum {enum_name} leaked past downgrade"

    _upgrade(migration_engine, "head")

    with migration_engine.connect() as conn:
        assert (
            conn.execute(text("select version_num from alembic_version")).scalar_one() == PIVOT_REV
        )
        assert conn.execute(text("select to_regclass('cookbooks')")).scalar_one() is not None
