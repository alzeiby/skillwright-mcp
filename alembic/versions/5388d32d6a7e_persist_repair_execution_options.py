"""persist repair execution options

Revision ID: 5388d32d6a7e
Revises: 3426d99f3843
Create Date: 2026-09-14 20:58:01.611840

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5388d32d6a7e"
down_revision: str | Sequence[str] | None = "3426d99f3843"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "repairs",
        sa.Column("persist_version", sa.Boolean(), server_default=sa.true(), nullable=False),
    )
    op.alter_column("repairs", "persist_version", server_default=None)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("repairs", "persist_version")
