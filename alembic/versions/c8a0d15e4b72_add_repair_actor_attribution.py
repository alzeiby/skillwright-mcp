"""add repair actor attribution

Revision ID: c8a0d15e4b72
Revises: 7df3217a9212
Create Date: 2026-09-15 02:55:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c8a0d15e4b72"
down_revision: str | Sequence[str] | None = "7df3217a9212"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""

    with op.batch_alter_table("repairs") as batch:
        batch.add_column(
            sa.Column("requested_by_principal_id", sa.String(length=36), nullable=True)
        )
        batch.create_index(
            "ix_repairs_requested_by_principal_id",
            ["requested_by_principal_id"],
            unique=False,
        )
        batch.create_foreign_key(
            "fk_repairs_requested_by_principal_id",
            "principals",
            ["requested_by_principal_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    """Downgrade schema."""

    with op.batch_alter_table("repairs") as batch:
        batch.drop_constraint("fk_repairs_requested_by_principal_id", type_="foreignkey")
        batch.drop_index("ix_repairs_requested_by_principal_id")
        batch.drop_column("requested_by_principal_id")
