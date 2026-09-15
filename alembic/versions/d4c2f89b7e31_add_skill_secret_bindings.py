"""add skill secret bindings

Revision ID: d4c2f89b7e31
Revises: a6f585d77dc1
Create Date: 2026-09-14 21:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d4c2f89b7e31"
down_revision: str | Sequence[str] | None = "a6f585d77dc1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "skill_secret_bindings",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("skill_id", sa.String(length=36), nullable=False),
        sa.Column("input_name", sa.String(length=160), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("secret_ref", sa.String(length=128), nullable=False),
        sa.Column("updated_by_principal_id", sa.String(length=36), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["skill_id"], ["skills.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["updated_by_principal_id"],
            ["principals.id"],
            name="fk_skill_secret_bindings_updated_by_principal_id",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("skill_id", "input_name", name="uq_skill_secret_binding"),
    )
    op.create_index(
        op.f("ix_skill_secret_bindings_skill_id"),
        "skill_secret_bindings",
        ["skill_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_skill_secret_bindings_updated_by_principal_id"),
        "skill_secret_bindings",
        ["updated_by_principal_id"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        op.f("ix_skill_secret_bindings_updated_by_principal_id"),
        table_name="skill_secret_bindings",
    )
    op.drop_index(
        op.f("ix_skill_secret_bindings_skill_id"),
        table_name="skill_secret_bindings",
    )
    op.drop_table("skill_secret_bindings")
