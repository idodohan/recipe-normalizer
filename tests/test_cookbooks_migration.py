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
from datetime import UTC, datetime
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
#: The ADDITIVE cookbooks-pivot revision (creates cookbooks, backfills,
#: absorbs the shared-cookbook data; leaves cookbook_id nullable and the old
#: tables in place).
PIVOT_REV = "4e1b7c9a52d8"
#: The FINALIZE revision: sweeps stragglers, flips cookbook_id NOT NULL, and
#: drops the shared_cookbook* tables. Also the current head.
FINALIZE_REV = "b7d3f0c11a94"


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
            conn.execute(text("select version_num from alembic_version")).scalar_one()
            == FINALIZE_REV
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

    _upgrade(migration_engine, "head")

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
    _upgrade(migration_engine, "head")

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
            conn.execute(text("select version_num from alembic_version")).scalar_one()
            == FINALIZE_REV
        )
