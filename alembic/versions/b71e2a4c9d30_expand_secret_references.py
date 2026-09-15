"""expand secret references for managed providers

Revision ID: b71e2a4c9d30
Revises: f24c9d0a8e11
Create Date: 2026-09-15 04:45:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import context, op

# revision identifiers, used by Alembic.
revision: str = "b71e2a4c9d30"
down_revision: str | Sequence[str] | None = "f24c9d0a8e11"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Allow full AWS Secrets Manager and SSM names/ARNs."""

    op.alter_column(
        "skill_secret_bindings",
        "secret_ref",
        existing_type=sa.String(length=128),
        type_=sa.String(length=2048),
        existing_nullable=False,
    )


def downgrade() -> None:
    """Restore the legacy logical-reference width when all data fits it."""

    if not context.is_offline_mode():
        bind = op.get_bind()
        oversized = bind.scalar(
            sa.text(
                "SELECT count(*) FROM skill_secret_bindings "
                "WHERE char_length(secret_ref) > 128"
            )
        )
        if oversized:
            raise RuntimeError(
                "cannot downgrade secret_ref to 128 characters while longer references exist"
            )

    op.alter_column(
        "skill_secret_bindings",
        "secret_ref",
        existing_type=sa.String(length=2048),
        type_=sa.String(length=128),
        existing_nullable=False,
    )
