"""add distributed run state

Revision ID: 3426d99f3843
Revises: 69a5859eb2dc
Create Date: 2026-09-14 20:45:51.987643

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3426d99f3843"
down_revision: str | Sequence[str] | None = "69a5859eb2dc"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("runs") as batch:
        batch.add_column(sa.Column("idempotency_key", sa.String(length=160), nullable=True))
        batch.add_column(sa.Column("worker_id", sa.String(length=160), nullable=True))
        batch.add_column(
            sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False)
        )
        batch.add_column(
            sa.Column("cancel_requested", sa.Boolean(), server_default=sa.false(), nullable=False)
        )
        batch.add_column(
            sa.Column(
                "queued_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            )
        )
        batch.add_column(sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True))
        batch.create_index("ix_runs_heartbeat_at", ["heartbeat_at"], unique=False)
        batch.create_index("ix_runs_worker_id", ["worker_id"], unique=False)
        batch.create_unique_constraint(
            "uq_runs_skill_idempotency_key", ["skill_id", "idempotency_key"]
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("runs") as batch:
        batch.drop_constraint("uq_runs_skill_idempotency_key", type_="unique")
        batch.drop_index("ix_runs_worker_id")
        batch.drop_index("ix_runs_heartbeat_at")
        batch.drop_column("heartbeat_at")
        batch.drop_column("queued_at")
        batch.drop_column("cancel_requested")
        batch.drop_column("attempt_count")
        batch.drop_column("worker_id")
        batch.drop_column("idempotency_key")
