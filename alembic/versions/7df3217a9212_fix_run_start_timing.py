"""fix run start timing

Revision ID: 7df3217a9212
Revises: d4c2f89b7e31
Create Date: 2026-09-14 21:37:02.518120

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7df3217a9212"
down_revision: str | Sequence[str] | None = "d4c2f89b7e31"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("runs") as batch_op:
        batch_op.alter_column(
            "started_at",
            existing_type=sa.DateTime(timezone=True),
            nullable=True,
        )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute(sa.text("UPDATE runs SET started_at = queued_at WHERE started_at IS NULL"))
    with op.batch_alter_table("runs") as batch_op:
        batch_op.alter_column(
            "started_at",
            existing_type=sa.DateTime(timezone=True),
            nullable=False,
        )
