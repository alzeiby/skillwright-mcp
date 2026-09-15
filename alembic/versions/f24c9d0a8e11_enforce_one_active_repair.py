"""enforce one active repair per run

Revision ID: f24c9d0a8e11
Revises: c8a0d15e4b72
Create Date: 2026-09-15 03:36:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f24c9d0a8e11"
down_revision: str | Sequence[str] | None = "c8a0d15e4b72"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""

    # The previous schema allowed two repair proposals to win a read-then-insert race. Preserve
    # the oldest active proposal per run and close any duplicates before adding the invariant so
    # an upgrade cannot fail on data produced by that old behavior.
    op.execute(
        sa.text(
            """
            WITH ranked AS (
                SELECT id,
                       ROW_NUMBER() OVER (
                           PARTITION BY run_id
                           ORDER BY created_at, id
                       ) AS active_rank
                FROM repairs
                WHERE status IN ('pending', 'applying')
            )
            UPDATE repairs
            SET status = 'cancelled', completed_at = CURRENT_TIMESTAMP
            WHERE id IN (SELECT id FROM ranked WHERE active_rank > 1)
            """
        )
    )
    op.create_index(
        "uq_repairs_active_run",
        "repairs",
        ["run_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending', 'applying')"),
        sqlite_where=sa.text("status IN ('pending', 'applying')"),
    )


def downgrade() -> None:
    """Downgrade schema."""

    op.drop_index("uq_repairs_active_run", table_name="repairs")
