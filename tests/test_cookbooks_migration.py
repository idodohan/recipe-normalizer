"""Alembic migration tests for the cookbooks-pivot and boards revisions.

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

It is also where ``test_orm_models_match_the_migration_chain`` lives — the
``compare_metadata`` guard against the models and the migration chain drifting
apart, which is what let ``cookbook_recipes`` exist in the ORM with no revision
for three tasks.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import Engine, create_engine, make_url, text
from sqlalchemy.exc import IntegrityError

from recipe_normalizer.config import settings
from recipe_normalizer.db import Base

REPO_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"

#: Revision immediately before the cookbooks pivot — the state a production
#: database is in before this migration runs.
PREV_HEAD = "533866b8e4fb"
#: The ADDITIVE cookbooks-pivot revision (creates cookbooks, backfills,
#: absorbs the shared-cookbook data; leaves cookbook_id nullable and the old
#: tables in place).
PIVOT_REV = "4e1b7c9a52d8"
#: The FINALIZE revision: sweeps stragglers, flips cookbook_id NOT NULL, and
#: drops the shared_cookbook* tables.
FINALIZE_REV = "b7d3f0c11a94"
#: The BOARDS finalize revision: backfills `cookbook_recipes` from
#: `recipes.cookbook_id`, folds `collections` into cookbooks, then drops
#: `recipes.cookbook_id` and the collections tables. The current head.
BOARDS_REV = "a3f7c2d8e015"


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
    sa.column("created_at", sa.DateTime(timezone=True)),
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
DAVE = uuid.UUID("00000000-0000-0000-0000-0000000000da")
R_ALICE_SOLO = uuid.UUID("00000000-0000-0000-0000-0000000000e1")
R_ALICE_SHARED = uuid.UUID("00000000-0000-0000-0000-0000000000e2")
R_BOB_SHARED = uuid.UUID("00000000-0000-0000-0000-0000000000e3")
R_DAVE_SHARED = uuid.UUID("00000000-0000-0000-0000-0000000000e4")
R_BOB_BAKERS = uuid.UUID("00000000-0000-0000-0000-0000000000e5")
FAMILY_CB = uuid.UUID("00000000-0000-0000-0000-00000000005c")
BAKERS_CB = uuid.UUID("00000000-0000-0000-0000-00000000005d")

# Explicit, distinct timestamps: `now()` is the *transaction* clock in
# Postgres, so seeding both cookbooks in one transaction would give them an
# identical created_at and the first-wins tie-break would silently fall
# through to the id ordering instead of the age ordering under test.
FAMILY_CREATED = datetime(2026, 1, 1, tzinfo=UTC)
BAKERS_CREATED = datetime(2026, 2, 1, tzinfo=UTC)


def _seed_pre_migration_state(engine: Engine) -> None:
    """Two shared cookbooks with different creators, plus the tricky cases.

    * "Family" (Alice's, older) holds a recipe of Alice's, one of Bob's, and
      one of **Dave's — and Dave is not a member of it**. That is reachable in
      production: ``sharing.remove_member`` drops the membership row but
      leaves the recipe link behind.
    * "Bakers" (Bob's, newer) holds one of Bob's own recipes plus the *same*
      Alice recipe Family already has — the contested, first-wins case.
    * Both carry the creator's own self-membership row, as
      ``sharing.service.create_shared_cookbook`` really does.
    * Carol is a member of Bakers but owns no recipes at all.
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
                for user_id, name in (
                    (ALICE, "alice"),
                    (BOB, "bob"),
                    (CAROL, "carol"),
                    (DAVE, "dave"),
                )
            ],
        )
        conn.execute(
            sa.insert(recipes_t),
            [
                {"id": recipe_id, "owner_id": owner, "title": title, "source_type": "manual"}
                for recipe_id, owner, title in (
                    (R_ALICE_SOLO, ALICE, "Alice solo"),
                    (R_ALICE_SHARED, ALICE, "Alice shared"),
                    (R_BOB_SHARED, BOB, "Bob shared"),
                    (R_DAVE_SHARED, DAVE, "Dave shared"),
                    (R_BOB_BAKERS, BOB, "Bob bakers"),
                )
            ],
        )
        conn.execute(
            sa.insert(shared_cookbooks_t),
            [
                {
                    "id": FAMILY_CB,
                    "name": "Family",
                    "created_by": ALICE,
                    "created_at": FAMILY_CREATED,
                },
                {
                    "id": BAKERS_CB,
                    "name": "Bakers",
                    "created_by": BOB,
                    "created_at": BAKERS_CREATED,
                },
            ],
        )
        conn.execute(
            sa.insert(shared_members_t),
            [
                {"cookbook_id": FAMILY_CB, "user_id": ALICE, "added_by": ALICE},
                {"cookbook_id": FAMILY_CB, "user_id": BOB, "added_by": ALICE},
                {"cookbook_id": BAKERS_CB, "user_id": BOB, "added_by": BOB},
                {"cookbook_id": BAKERS_CB, "user_id": CAROL, "added_by": BOB},
            ],
        )
        conn.execute(
            sa.insert(shared_recipes_t),
            [
                {"cookbook_id": FAMILY_CB, "recipe_id": R_ALICE_SHARED, "added_by": ALICE},
                {"cookbook_id": FAMILY_CB, "recipe_id": R_BOB_SHARED, "added_by": BOB},
                {"cookbook_id": FAMILY_CB, "recipe_id": R_DAVE_SHARED, "added_by": ALICE},
                {"cookbook_id": BAKERS_CB, "recipe_id": R_ALICE_SHARED, "added_by": BOB},
                {"cookbook_id": BAKERS_CB, "recipe_id": R_BOB_BAKERS, "added_by": BOB},
            ],
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def _members_of(
    conn: sa.Connection, cookbook_id: uuid.UUID
) -> list[tuple[uuid.UUID, str, uuid.UUID]]:
    rows = conn.execute(
        text(
            "select user_id, role::text as role, added_by from cookbook_members "
            "where cookbook_id = :cb order by user_id"
        ),
        {"cb": cookbook_id},
    ).all()
    return [(row.user_id, row.role, row.added_by) for row in rows]


def test_backfill_preserves_every_recipe_and_shared_cookbook(migration_engine: Engine) -> None:
    """The full pre-state → post-state data-preservation contract.

    Stops at ``PIVOT_REV`` on purpose, not ``head``: this is the *additive*
    migration's contract, and two of its guarantees (the shared tables
    survive, cookbook_id stays nullable) are precisely what the finalize
    revision then undoes — see the finalize tests at the bottom.
    """
    _upgrade(migration_engine, PREV_HEAD)
    _seed_pre_migration_state(migration_engine)
    _upgrade(migration_engine, PIVOT_REV)

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
                "select owner_id, name, visibility::text as visibility "
                "from cookbooks where is_default"
            )
        ).all()
        assert len(defaults) == 4
        assert {row.owner_id for row in defaults} == {ALICE, BOB, CAROL, DAVE}
        assert {row.name for row in defaults} == {"My Cookbook"}
        assert {row.visibility for row in defaults} == {"private"}

        # 3. Each shared cookbook became a real cookbook owned by its creator.
        family = conn.execute(
            text(
                "select id, owner_id, is_default, visibility::text as visibility "
                "from cookbooks where name = 'Family'"
            )
        ).one()
        bakers = conn.execute(
            text("select id, owner_id, is_default from cookbooks where name = 'Bakers'")
        ).one()
        assert (family.owner_id, family.is_default, family.visibility) == (ALICE, False, "private")
        assert (bakers.owner_id, bakers.is_default) == (BOB, False)

        # 4. Members carried over as editors, audit trail intact. Each
        #    cookbook's own creator is deliberately NOT a member row — in the
        #    new model the owner is implicit (see list_my_cookbooks), and a
        #    self-membership row would make the cookbook list it twice.
        #    Dave's row is *synthesized*: he owns a recipe that Family claimed
        #    but he was never a member of Family, so without it he would lose
        #    all access to his own recipe.
        assert _members_of(conn, family.id) == [
            (BOB, "editor", ALICE),  # carried over from shared_cookbook_members
            (DAVE, "editor", ALICE),  # synthesized — recipe owner, added_by = cookbook owner
        ]
        # Bob owns R_BOB_BAKERS which Bakers claimed, but Bob *is* Bakers'
        # owner, so no row is synthesized for him. Carol carries over.
        assert _members_of(conn, bakers.id) == [(CAROL, "editor", BOB)]

        # 5. Recipes land where they should: shared ones (whoever owns them)
        #    in the derived cookbook, the rest in their owner's default. The
        #    contested recipe — in BOTH shared cookbooks — goes to the older
        #    one (Family), deterministically, and Bakers silently loses it.
        placement = dict(
            conn.execute(text("select id, cookbook_id from recipes")).all()  # type: ignore[arg-type]
        )
        alice_default = conn.execute(
            text("select id from cookbooks where is_default and owner_id = :o"), {"o": ALICE}
        ).scalar_one()
        assert placement[R_ALICE_SHARED] == family.id
        assert placement[R_BOB_SHARED] == family.id
        assert placement[R_DAVE_SHARED] == family.id
        assert placement[R_BOB_BAKERS] == bakers.id
        assert placement[R_ALICE_SOLO] == alice_default

        # 6. The old shared_cookbook* tables are still here — the finalize
        #    revision drops them, only after the code reading them is gone.
        for table in (
            "shared_cookbooks",
            "shared_cookbook_members",
            "shared_cookbook_recipes",
        ):
            assert conn.execute(
                text("select to_regclass(:t) is not null"), {"t": table}
            ).scalar_one(), f"{table} must survive this migration"

        # 7. recipes.cookbook_id stays NULLABLE until the finalize revision
        #    (which runs after the writers stopped producing NULLs).
        nullable = conn.execute(
            text(
                "select is_nullable from information_schema.columns "
                "where table_name = 'recipes' and column_name = 'cookbook_id'"
            )
        ).scalar_one()
        assert nullable == "YES"

        # 8. THE invariant behind all of the above: nobody lost access to
        #    their own recipe. Access is now derived from the cookbook, so
        #    every recipe's owner must either own the cookbook holding it or
        #    have a member row on it — otherwise cookbook.service's
        #    _recipe_access returns None and the owner 404s on their own
        #    recipe while the owner-scoped list still shows it.
        stranded = conn.execute(
            text(
                "select r.id from recipes r "
                "join cookbooks c on c.id = r.cookbook_id "
                "left join cookbook_members m "
                "  on m.cookbook_id = c.id and m.user_id = r.owner_id "
                "where c.owner_id <> r.owner_id and m.user_id is null"
            )
        ).all()
        assert stranded == [], "recipe owners must keep access to their own recipes"


def test_backfill_logs_the_contested_shared_recipe_link(
    migration_engine: Engine, capfd: pytest.CaptureFixture[str]
) -> None:
    """The first-wins drop is reported, not silent.

    Asserted through captured stderr rather than ``caplog``: ``env.py`` calls
    ``fileConfig()`` on every alembic invocation, which wipes the handlers
    pytest's log-capture fixture installs. The console handler fileConfig
    then creates writes to stderr, which capfd does see.
    """
    _upgrade(migration_engine, PREV_HEAD)
    _seed_pre_migration_state(migration_engine)
    capfd.readouterr()  # drop everything the pre-migration upgrades emitted
    _upgrade(migration_engine, PIVOT_REV)

    err = capfd.readouterr().err
    assert "1 recipe/shared-cookbook link(s) dropped" in err
    # ...and the summary line accounts for the synthesized membership row.
    assert "2 shared cookbook(s) converted" in err
    assert "3 member row(s) written (1 synthesized" in err
    assert "4 recipe(s) claimed" in err


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


def test_ensure_default_cookbook_survives_losing_the_insert_race(
    migration_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The loser of the race gets the winner's row back, not an IntegrityError.

    This is the one test in the module that drives the *service* rather than
    raw rows: it needs the real ``uq_cookbooks_default_per_owner`` partial
    index, and this fixture is the only place it exists (every other test
    builds its schema with ``Base.metadata.create_all``, which doesn't know
    about it). At head the ORM models match the schema, so that is safe here.

    The interleaving is simulated by making the initial lookup miss exactly
    once — precisely the state of a request that read before a concurrent
    request inserted the default cookbook. Everything after that is the real
    thing: a genuine unique violation on the INSERT, swallowed by
    ``ON CONFLICT DO NOTHING``, then the re-SELECT that finds the live row.
    """
    _upgrade(migration_engine, "head")

    from sqlalchemy.orm import Session

    from recipe_normalizer.cookbook import service as cookbook_service
    from recipe_normalizer.cookbook.models import Cookbook

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

    with Session(migration_engine) as session:
        winner = cookbook_service.ensure_default_cookbook(session, ALICE)
        session.commit()
        winner_id = winner.id

        real_lookup = cookbook_service._select_default_cookbook
        calls: list[int] = []

        def lookup_blind_once(db: Session, user_id: uuid.UUID) -> Cookbook | None:
            calls.append(1)
            return None if len(calls) == 1 else real_lookup(db, user_id)

        monkeypatch.setattr(cookbook_service, "_select_default_cookbook", lookup_blind_once)

        loser = cookbook_service.ensure_default_cookbook(session, ALICE)

        assert len(calls) == 2, "expected a missed lookup then a re-read after the conflict"
        assert loser.id == winner_id
        session.commit()

    # Still exactly one default cookbook — the conflicting INSERT wrote nothing.
    with migration_engine.connect() as conn:
        assert (
            conn.execute(
                text("select count(*) from cookbooks where owner_id = :o and is_default"),
                {"o": ALICE},
            ).scalar_one()
            == 1
        )


def test_upgrade_downgrade_upgrade_round_trips(migration_engine: Engine) -> None:
    """Reversible on an empty DB: head → PREV_HEAD → head leaves no debris.

    Walks BOTH pivot revisions down and back up. A leaked enum type or index
    would make the second upgrade fail, so the round trip completing at all
    is most of the assertion.
    """
    _upgrade(migration_engine, "head")
    _downgrade(migration_engine, PREV_HEAD)

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
            conn.execute(text("select version_num from alembic_version")).scalar_one() == BOARDS_REV
        )
        assert conn.execute(text("select to_regclass('cookbooks')")).scalar_one() is not None


# ---------------------------------------------------------------------------
# The FINALIZE revision: sweep stragglers → NOT NULL → drop the old tables
# ---------------------------------------------------------------------------

#: A user who registers AFTER the additive migration ran, so nothing ever
#: created a default cookbook for them.
ERIN = uuid.UUID("00000000-0000-0000-0000-0000000000e0")
#: Erin's recipe and one of Alice's, both written with a NULL cookbook_id
#: between the two migrations (what copy_recipe/extraction used to do).
R_ERIN_STRAY = uuid.UUID("00000000-0000-0000-0000-0000000000f1")
R_ALICE_STRAY = uuid.UUID("00000000-0000-0000-0000-0000000000f2")


def _seed_stragglers_after_the_additive_migration(engine: Engine) -> None:
    """Two NULL-cookbook recipes written between the two migrations.

    One belongs to Alice, who already has a default cookbook (the additive
    migration made her one); the other to Erin, who registered afterwards and
    therefore has no cookbook at all. The finalize sweep has to handle both,
    or the ``SET NOT NULL`` right after it fails and takes the deploy with it.
    """
    with engine.begin() as conn:
        conn.execute(
            sa.insert(users_t),
            [
                {
                    "id": ERIN,
                    "email": "erin@example.com",
                    "password_hash": "x",
                    "display_name": "Erin",
                }
            ],
        )
        conn.execute(
            sa.insert(recipes_t),
            [
                {
                    "id": R_ERIN_STRAY,
                    "owner_id": ERIN,
                    "title": "Erin stray",
                    "source_type": "manual",
                    "cookbook_id": None,
                },
                {
                    "id": R_ALICE_STRAY,
                    "owner_id": ALICE,
                    "title": "Alice stray",
                    "source_type": "manual",
                    "cookbook_id": None,
                },
            ],
        )


def test_finalize_sweeps_stragglers_then_enforces_not_null(migration_engine: Engine) -> None:
    """Every NULL-cookbook recipe written since the additive migration lands."""
    _upgrade(migration_engine, PREV_HEAD)
    _seed_pre_migration_state(migration_engine)
    _upgrade(migration_engine, PIVOT_REV)
    _seed_stragglers_after_the_additive_migration(migration_engine)

    # Stops at FINALIZE_REV, not head: BOARDS_REV drops `recipes.cookbook_id`
    # outright, and this is that column's contract.
    _upgrade(migration_engine, FINALIZE_REV)

    with migration_engine.connect() as conn:
        assert (
            conn.execute(
                text("select count(*) from recipes where cookbook_id is null")
            ).scalar_one()
            == 0
        )

        # Alice's straggler went to the default she already had...
        placement = dict(
            conn.execute(text("select id, cookbook_id from recipes")).all()  # type: ignore[arg-type]
        )
        alice_default = conn.execute(
            text("select id from cookbooks where is_default and owner_id = :o"), {"o": ALICE}
        ).scalar_one()
        assert placement[R_ALICE_STRAY] == alice_default

        # ...and Erin, who had none, got one created for her.
        erin_default = conn.execute(
            text(
                "select id, name, visibility::text as visibility from cookbooks "
                "where is_default and owner_id = :o"
            ),
            {"o": ERIN},
        ).one()
        assert placement[R_ERIN_STRAY] == erin_default.id
        assert (erin_default.name, erin_default.visibility) == ("My Cookbook", "private")

        # Nothing that was already placed by the additive migration moved.
        family = conn.execute(text("select id from cookbooks where name = 'Family'")).scalar_one()
        assert placement[R_ALICE_SHARED] == family

        # The column is genuinely NOT NULL now, not merely empty of NULLs.
        nullable = conn.execute(
            text(
                "select is_nullable from information_schema.columns "
                "where table_name = 'recipes' and column_name = 'cookbook_id'"
            )
        ).scalar_one()
        assert nullable == "NO"

        # Invariant #8 again, AFTER the flip: no recipe sits in a cookbook
        # whose owner is neither the recipe's owner nor a member — otherwise
        # cookbook.service._recipe_access returns None and the owner 404s on
        # their own recipe.
        stranded = conn.execute(
            text(
                "select r.id from recipes r "
                "join cookbooks c on c.id = r.cookbook_id "
                "left join cookbook_members m "
                "  on m.cookbook_id = c.id and m.user_id = r.owner_id "
                "where c.owner_id <> r.owner_id and m.user_id is null"
            )
        ).all()
        assert stranded == [], "recipe owners must keep access to their own recipes"

    with pytest.raises(IntegrityError), migration_engine.begin() as conn:
        conn.execute(
            sa.insert(recipes_t),
            [
                {
                    "id": uuid.uuid4(),
                    "owner_id": ALICE,
                    "title": "Post-flip NULL",
                    "source_type": "manual",
                    "cookbook_id": None,
                }
            ],
        )


def test_finalize_drops_the_shared_cookbook_tables(migration_engine: Engine) -> None:
    """The old co-ownership tables are gone once nothing reads them."""
    _upgrade(migration_engine, PREV_HEAD)
    _seed_pre_migration_state(migration_engine)
    _upgrade(migration_engine, FINALIZE_REV)

    with migration_engine.connect() as conn:
        for table in ("shared_cookbook_recipes", "shared_cookbook_members", "shared_cookbooks"):
            assert (
                conn.execute(text("select to_regclass(:t)"), {"t": table}).scalar_one() is None
            ), f"{table} should be gone after the finalize migration"


def test_finalize_downgrade_restores_the_schema_but_not_the_data(migration_engine: Engine) -> None:
    """down(finalize) is a SCHEMA reverse only — the old rows are not restored.

    The shared_cookbook* tables come back empty and cookbook_id goes nullable
    again, which is exactly enough for the previous revision's code to boot;
    the co-ownership rows themselves were converted into cookbooks/members by
    the additive migration and are not re-derivable.
    """
    _upgrade(migration_engine, PREV_HEAD)
    _seed_pre_migration_state(migration_engine)
    _upgrade(migration_engine, "head")

    _downgrade(migration_engine, PIVOT_REV)

    with migration_engine.connect() as conn:
        for table in ("shared_cookbooks", "shared_cookbook_members", "shared_cookbook_recipes"):
            assert (
                conn.execute(text("select to_regclass(:t)"), {"t": table}).scalar_one() is not None
            ), f"{table} should be back after the downgrade"
            assert conn.execute(text(f"select count(*) from {table}")).scalar_one() == 0, (
                f"{table} comes back EMPTY — the data is not restored"
            )

        nullable = conn.execute(
            text(
                "select is_nullable from information_schema.columns "
                "where table_name = 'recipes' and column_name = 'cookbook_id'"
            )
        ).scalar_one()
        assert nullable == "YES"

        # The cookbooks the additive migration built are untouched by this
        # downgrade — only the finalize step was reversed.
        assert conn.execute(text("select count(*) from cookbooks")).scalar_one() > 0

    # ...and it goes straight back up again.
    _upgrade(migration_engine, "head")
    with migration_engine.connect() as conn:
        assert (
            conn.execute(text("select version_num from alembic_version")).scalar_one() == BOARDS_REV
        )


# ---------------------------------------------------------------------------
# The BOARDS finalize revision: create the join → backfill it → fold
# collections in → drop `recipes.cookbook_id` + the collections tables
# ---------------------------------------------------------------------------

collections_t = sa.table(
    "collections",
    sa.column("id", sa.Uuid()),
    sa.column("owner_id", sa.Uuid()),
    sa.column("name", sa.String()),
)
collection_recipes_t = sa.table(
    "collection_recipes",
    sa.column("collection_id", sa.Uuid()),
    sa.column("recipe_id", sa.Uuid()),
)
cookbook_recipes_t = sa.table(
    "cookbook_recipes",
    sa.column("cookbook_id", sa.Uuid()),
    sa.column("recipe_id", sa.Uuid()),
    sa.column("added_by", sa.Uuid()),
    sa.column("added_at", sa.DateTime(timezone=True)),
)

#: Alice's collection, holding one of her OWN recipes and one of BOB's — the
#: latter must NOT be folded in (collections are owner-scoped; see the
#: migration's docstring).
ALICE_WEEKNIGHTS = uuid.UUID("00000000-0000-0000-0000-0000000000c1")
#: Carol's collection, empty — it still becomes a cookbook.
CAROL_EMPTY = uuid.UUID("00000000-0000-0000-0000-0000000000c2")
#: An `added_at` deliberately older than the migration's own transaction clock,
#: so a placement stamped with it is unambiguously the oldest one.
ANCIENT = datetime(2025, 1, 1, tzinfo=UTC)


def _seed_collections(engine: Engine) -> None:
    """Two collections as they look just before BOARDS_REV.

    * ``ALICE_WEEKNIGHTS`` lists ``R_ALICE_SOLO`` (hers) and ``R_BOB_SHARED``
      (Bob's) — the fold must take the first and skip the second, since
      collections were owner-scoped personal organization and a cross-owner
      link would hand Alice an editor-grade placement on Bob's recipe.
    * ``CAROL_EMPTY`` lists nothing — it still has to become a cookbook.
    """
    with engine.begin() as conn:
        conn.execute(
            sa.insert(collections_t),
            [
                {"id": ALICE_WEEKNIGHTS, "owner_id": ALICE, "name": "Weeknights"},
                {"id": CAROL_EMPTY, "owner_id": CAROL, "name": "Someday"},
            ],
        )
        conn.execute(
            sa.insert(collection_recipes_t),
            [
                {"collection_id": ALICE_WEEKNIGHTS, "recipe_id": R_ALICE_SOLO},
                {"collection_id": ALICE_WEEKNIGHTS, "recipe_id": R_BOB_SHARED},
            ],
        )


def _placements(conn: sa.Connection) -> dict[uuid.UUID, set[uuid.UUID]]:
    """recipe_id -> the set of cookbook ids it is placed in."""
    out: dict[uuid.UUID, set[uuid.UUID]] = {}
    for row in conn.execute(text("select recipe_id, cookbook_id from cookbook_recipes")):
        out.setdefault(row.recipe_id, set()).add(row.cookbook_id)
    return out


def test_boards_creates_and_backfills_the_join_table(migration_engine: Engine) -> None:
    """`cookbook_recipes` is created here and gets one row per recipe.

    Task 1 added the join to the ORM models only (the suite builds its schema
    with ``create_all``), so a migrated database arrives at this revision with
    no join table at all — creating it and seeding it from the legacy primary
    placement is a single atomic step, and the backfill is where a real
    database's every existing recipe gets its first placement.
    """
    _upgrade(migration_engine, PREV_HEAD)
    _seed_pre_migration_state(migration_engine)
    _upgrade(migration_engine, FINALIZE_REV)
    _seed_collections(migration_engine)

    with migration_engine.connect() as conn:
        assert conn.execute(text("select to_regclass('cookbook_recipes')")).scalar_one() is None
        legacy = {
            row.id: (row.cookbook_id, row.owner_id)
            for row in conn.execute(text("select id, cookbook_id, owner_id from recipes"))
        }
    assert len(legacy) == 5

    _upgrade(migration_engine, "head")

    with migration_engine.connect() as conn:
        placements = _placements(conn)
        for recipe_id, (cookbook_id, _owner_id) in legacy.items():
            assert cookbook_id in placements[recipe_id], (
                f"recipe {recipe_id} lost its primary placement {cookbook_id}"
            )

        # `added_by` is the recipe's own owner — the closest thing to "who put
        # this here" the pre-boards schema recorded.
        rows = conn.execute(
            text("select cookbook_id, recipe_id, added_by from cookbook_recipes")
        ).all()
        for row in rows:
            if row.cookbook_id == legacy[row.recipe_id][0]:
                assert row.added_by == legacy[row.recipe_id][1]

        # No duplicates anywhere (the composite PK forbids them, and
        # ON CONFLICT DO NOTHING is what keeps the step re-runnable).
        assert len(rows) == len({(row.cookbook_id, row.recipe_id) for row in rows})

        # The standalone recipe_id index exists — THE access path of the model.
        assert (
            conn.execute(
                text(
                    "select count(*) from pg_indexes where tablename = 'cookbook_recipes' "
                    "and indexname = 'ix_cookbook_recipes_recipe_id'"
                )
            ).scalar_one()
            == 1
        )


def test_boards_folds_collections_into_owner_scoped_cookbooks(migration_engine: Engine) -> None:
    """Each collection becomes a private, non-default cookbook with its OWNER's recipes."""
    _upgrade(migration_engine, PREV_HEAD)
    _seed_pre_migration_state(migration_engine)
    _upgrade(migration_engine, FINALIZE_REV)
    _seed_collections(migration_engine)

    _upgrade(migration_engine, "head")

    with migration_engine.connect() as conn:
        weeknights = conn.execute(
            text(
                "select id, owner_id, is_default, visibility::text as visibility "
                "from cookbooks where name = 'Weeknights'"
            )
        ).one()
        assert (weeknights.owner_id, weeknights.is_default, weeknights.visibility) == (
            ALICE,
            False,
            "private",
        )

        # Carol's empty collection is a cookbook too — no recipes, but her
        # organization survives the fold.
        someday = conn.execute(
            text("select id, owner_id, is_default from cookbooks where name = 'Someday'")
        ).one()
        assert (someday.owner_id, someday.is_default) == (CAROL, False)

        folded = conn.execute(
            text(
                "select recipe_id, added_by from cookbook_recipes where cookbook_id = :cb "
                "order by recipe_id"
            ),
            {"cb": weeknights.id},
        ).all()
        # ONLY Alice's own recipe — R_BOB_SHARED was in her collection but is
        # Bob's row, so folding it in would hand her a placement on his recipe.
        assert [(row.recipe_id, row.added_by) for row in folded] == [(R_ALICE_SOLO, ALICE)]
        assert (
            conn.execute(
                text("select count(*) from cookbook_recipes where cookbook_id = :cb"),
                {"cb": someday.id},
            ).scalar_one()
            == 0
        )

        # The fold ADDS a placement — R_ALICE_SOLO is still in its original
        # cookbook as well. That is the many-to-many win.
        assert _placements(conn)[R_ALICE_SOLO] == {
            weeknights.id,
            conn.execute(
                text("select id from cookbooks where is_default and owner_id = :o"), {"o": ALICE}
            ).scalar_one(),
        }


def test_boards_logs_the_fold_and_the_skipped_link(
    migration_engine: Engine, capfd: pytest.CaptureFixture[str]
) -> None:
    """Counts are reported, and the skipped cross-owner link is accounted for."""
    _upgrade(migration_engine, PREV_HEAD)
    _seed_pre_migration_state(migration_engine)
    _upgrade(migration_engine, FINALIZE_REV)
    _seed_collections(migration_engine)
    capfd.readouterr()  # drop everything the earlier upgrades emitted

    _upgrade(migration_engine, "head")

    err = capfd.readouterr().err
    assert "5 join row(s) backfilled from recipes.cookbook_id" in err
    assert "2 collection(s) folded into cookbooks" in err
    assert "1 placement(s) written from 2 collection_recipes link(s) (1 skipped" in err


def test_boards_drops_the_transition_scaffolding(migration_engine: Engine) -> None:
    """`recipes.cookbook_id` and both collections tables are gone at head."""
    _upgrade(migration_engine, PREV_HEAD)
    _seed_pre_migration_state(migration_engine)
    _upgrade(migration_engine, FINALIZE_REV)
    _seed_collections(migration_engine)

    _upgrade(migration_engine, "head")

    with migration_engine.connect() as conn:
        assert (
            conn.execute(
                text(
                    "select count(*) from information_schema.columns "
                    "where table_name = 'recipes' and column_name = 'cookbook_id'"
                )
            ).scalar_one()
            == 0
        )
        for table in ("collection_recipes", "collections"):
            assert (
                conn.execute(text("select to_regclass(:t)"), {"t": table}).scalar_one() is None
            ), f"{table} should be gone after the boards migration"

        # Nothing was destroyed on the way: every recipe still exists and every
        # one of them is still in at least one cookbook.
        assert conn.execute(text("select count(*) from recipes")).scalar_one() == 5
        assert (
            conn.execute(
                text(
                    "select count(*) from recipes r where not exists ("
                    "  select 1 from cookbook_recipes cr where cr.recipe_id = r.id)"
                )
            ).scalar_one()
            == 0
        ), "every recipe must keep >= 1 placement"


def test_boards_downgrade_restores_the_column_from_the_oldest_placement(
    migration_engine: Engine,
) -> None:
    """down(boards) rebuilds `recipes.cookbook_id` (oldest placement) + EMPTY collections."""
    _upgrade(migration_engine, PREV_HEAD)
    _seed_pre_migration_state(migration_engine)
    _upgrade(migration_engine, FINALIZE_REV)
    _seed_collections(migration_engine)
    _upgrade(migration_engine, "head")

    # A second placement for Alice's solo recipe, on Bob's "Bakers" board and
    # stamped OLDER than anything the migration wrote — what
    # `add_recipe_to_cookbook` produces once the boards model is live. The
    # downgrade has to collapse the three placements down to this one.
    # NB the cookbook's id is NOT ``BAKERS_CB`` (that was the old
    # `shared_cookbooks` row's id); the pivot minted a fresh one.
    with migration_engine.begin() as conn:
        bakers_cb = conn.execute(
            text("select id from cookbooks where name = 'Bakers'")
        ).scalar_one()
        conn.execute(
            sa.insert(cookbook_recipes_t),
            [
                {
                    "cookbook_id": bakers_cb,
                    "recipe_id": R_ALICE_SOLO,
                    "added_by": BOB,
                    "added_at": ANCIENT,
                }
            ],
        )

    _downgrade(migration_engine, FINALIZE_REV)

    with migration_engine.connect() as conn:
        # The column is back, NOT NULL, and every recipe has one.
        nullable = conn.execute(
            text(
                "select is_nullable from information_schema.columns "
                "where table_name = 'recipes' and column_name = 'cookbook_id'"
            )
        ).scalar_one()
        assert nullable == "NO"
        assert (
            conn.execute(
                text("select count(*) from recipes where cookbook_id is null")
            ).scalar_one()
            == 0
        )

        # R_ALICE_SOLO sat on three boards; the hand-added Bakers placement is
        # the oldest (2025 vs the migration's own now()), so it wins.
        placement = dict(
            conn.execute(text("select id, cookbook_id from recipes")).all()  # type: ignore[arg-type]
        )
        assert placement[R_ALICE_SOLO] == bakers_cb
        # Everyone else keeps the cookbook they had before the upgrade.
        family = conn.execute(text("select id from cookbooks where name = 'Family'")).scalar_one()
        assert placement[R_ALICE_SHARED] == family

        # The join table itself is gone, and the collections tables are back
        # but EMPTY — documented, not restored.
        assert conn.execute(text("select to_regclass('cookbook_recipes')")).scalar_one() is None
        for table in ("collections", "collection_recipes"):
            assert (
                conn.execute(text("select to_regclass(:t)"), {"t": table}).scalar_one() is not None
            ), f"{table} should be back after the downgrade"
            assert conn.execute(text(f"select count(*) from {table}")).scalar_one() == 0, (
                f"{table} comes back EMPTY — the data is not restored"
            )

        # The collection-derived cookbooks stay: deleting them would destroy
        # placements the user may have curated since.
        assert (
            conn.execute(
                text("select count(*) from cookbooks where name in ('Weeknights', 'Someday')")
            ).scalar_one()
            == 2
        )

    # ...and it goes straight back up again.
    _upgrade(migration_engine, "head")
    with migration_engine.connect() as conn:
        assert (
            conn.execute(text("select version_num from alembic_version")).scalar_one() == BOARDS_REV
        )
        assert conn.execute(text("select to_regclass('collections')")).scalar_one() is None
        assert conn.execute(text("select to_regclass('cookbook_recipes')")).scalar_one() is not None


# ---------------------------------------------------------------------------
# The ORM models and the migration chain must not drift apart
# ---------------------------------------------------------------------------

#: Indexes created by RAW SQL in a migration and therefore deliberately absent
#: from the ORM models, so ``compare_metadata`` always proposes dropping them.
#: Every one is intentional — see the migration that creates it:
#:
#: * ``uq_cookbooks_default_per_owner`` — a PARTIAL unique index
#:   (``cookbooks (owner_id) WHERE is_default``) from 4e1b7c9a52d8; it is the DB
#:   backstop behind ``ensure_default_cookbook``'s read-then-insert race and is
#:   kept out of the models on purpose.
#: * ``ix_recipes_title_description_fts`` / ``ix_recipes_title_trgm`` —
#:   expression/GIN indexes from 6b6111bf2366 that back ``list_recipes``' search;
#:   several later revisions carry a comment about autogenerate proposing their
#:   removal as a false positive.
#:
#: Anything NOT on this list is real drift and fails the test below.
RAW_SQL_ONLY_INDEXES = frozenset(
    {
        "uq_cookbooks_default_per_owner",
        "ix_recipes_title_description_fts",
        "ix_recipes_title_trgm",
    }
)


def _import_every_model_module() -> None:
    """Populate ``Base.metadata`` with every table the app declares.

    The same import list ``alembic/env.py`` carries, and for the same reason: a
    model module nobody imports contributes nothing to ``Base.metadata``, so
    both autogenerate and the test below would silently miss its tables. Keep
    the two lists in sync when adding a module.
    """
    import recipe_normalizer.ai.models  # noqa: F401
    import recipe_normalizer.catalog.models  # noqa: F401
    import recipe_normalizer.cookbook.models  # noqa: F401
    import recipe_normalizer.ingestion.models  # noqa: F401
    import recipe_normalizer.llm.models  # noqa: F401
    import recipe_normalizer.sharing.models  # noqa: F401
    import recipe_normalizer.users.models  # noqa: F401


def _is_known_raw_sql_index(entry: object) -> bool:
    """True for a ``remove_index`` diff naming one of RAW_SQL_ONLY_INDEXES."""
    if not (isinstance(entry, tuple) and len(entry) == 2 and entry[0] == "remove_index"):
        return False
    return getattr(entry[1], "name", None) in RAW_SQL_ONLY_INDEXES


def _flatten_diff(diff: object) -> list[object]:
    """One level of flattening — column-level diffs arrive as nested lists."""
    out: list[object] = []
    if isinstance(diff, list):
        for entry in diff:
            out.extend(_flatten_diff(entry))
    else:
        out.append(diff)
    return out


def test_orm_models_match_the_migration_chain(migration_engine: Engine) -> None:
    """``upgrade head`` must produce exactly the schema the ORM models describe.

    THE guard for the failure this phase actually hit: ``cookbook_recipes`` was
    added to ``cookbook/models.py`` in Task 1 with no accompanying revision, and
    nothing noticed for three tasks — every other test in the suite builds its
    schema with ``Base.metadata.create_all``, so the models are trivially
    self-consistent there and the alembic chain is never compared against them.

    Runs ``alembic.autogenerate.compare_metadata`` against a real migrated
    database and asserts the only differences are the three raw-SQL indexes that
    are deliberately not in the models. A new/renamed/retyped table or column on
    either side shows up here as an unexpected diff entry.
    """
    _upgrade(migration_engine, "head")

    _import_every_model_module()

    with migration_engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)

    unexpected = [entry for entry in _flatten_diff(diff) if not _is_known_raw_sql_index(entry)]
    assert unexpected == [], (
        "ORM models and the alembic chain have drifted — either a migration is "
        f"missing or a model changed without one:\n{unexpected}"
    )
