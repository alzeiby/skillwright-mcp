"""remove role-based access control

Revision ID: 6f4c2a1d9e8b
Revises: b71e2a4c9d30
Create Date: 2026-09-15 15:05:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "6f4c2a1d9e8b"
down_revision: str | Sequence[str] | None = "b71e2a4c9d30"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Drop obsolete roles and per-skill grants while retaining identity attribution."""

    op.drop_index(op.f("ix_skill_permissions_skill_id"), table_name="skill_permissions")
    op.drop_index(op.f("ix_skill_permissions_principal_id"), table_name="skill_permissions")
    op.drop_table("skill_permissions")
    with op.batch_alter_table("principals") as batch:
        batch.drop_column("disabled")
        batch.drop_column("role")


def downgrade() -> None:
    """Restore the former access-control schema shape without reconstructing historical grants."""

    with op.batch_alter_table("principals") as batch:
        batch.add_column(
            sa.Column("role", sa.String(length=32), server_default="viewer", nullable=False)
        )
        batch.add_column(
            sa.Column("disabled", sa.Boolean(), server_default=sa.false(), nullable=False)
        )
    with op.batch_alter_table("principals") as batch:
        batch.alter_column("role", server_default=None)
        batch.alter_column("disabled", server_default=None)

    op.create_table(
        "skill_permissions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("skill_id", sa.String(length=36), nullable=False),
        sa.Column("principal_id", sa.String(length=36), nullable=False),
        sa.Column("permission", sa.String(length=24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["principal_id"], ["principals.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["skill_id"], ["skills.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("skill_id", "principal_id", "permission", name="uq_skill_permission"),
    )
    op.create_index(
        op.f("ix_skill_permissions_principal_id"),
        "skill_permissions",
        ["principal_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_skill_permissions_skill_id"),
        "skill_permissions",
        ["skill_id"],
        unique=False,
    )
