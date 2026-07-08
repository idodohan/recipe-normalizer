"""transform input/source types

Adds the 'transform' value to the native Postgres 'inputtype' and 'sourcetype'
enums, for Phase 3 Task 6 (ai.service.transform_recipe): a transform's draft
recipe is stamped source_type=transform, and the Job that lands it in the
review gate (ingestion.service.create_transform_job) is input_type=transform.

Revision ID: 1f3c9a4e2b71
Revises: 6320bfff5c8d
Create Date: 2026-07-09 00:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "1f3c9a4e2b71"
down_revision: str | Sequence[str] | None = "6320bfff5c8d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # ALTER TYPE ... ADD VALUE cannot run inside the transaction alembic normally
    # wraps migrations in (PostgreSQL forbids using the new value in the same
    # transaction that adds it, and disallows the ADD VALUE itself in some server
    # versions when already inside a transaction block) — run each in its own
    # autocommit block.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE inputtype ADD VALUE IF NOT EXISTS 'transform'")
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE sourcetype ADD VALUE IF NOT EXISTS 'transform'")


def downgrade() -> None:
    """Downgrade schema.

    PostgreSQL has no DROP VALUE for enums — removing one requires rebuilding
    the type (create new type, cast every column, drop the old type), which
    is unsafe to do blindly in a downgrade (any row already using 'transform'
    would fail the cast). Left as a no-op, matching the project's convention
    for additive-only enum changes; a manual data migration would be required
    to actually remove the value.
    """
