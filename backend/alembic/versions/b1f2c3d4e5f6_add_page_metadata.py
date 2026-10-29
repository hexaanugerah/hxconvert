"""add page metadata to conversion jobs

Revision ID: b1f2c3d4e5f6
Revises: 07220c70f223
Create Date: 2026-10-23 10:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b1f2c3d4e5f6"
down_revision: Union[str, None] = "07220c70f223"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("conversion_jobs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("page_count", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("pages_json", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("conversion_jobs", schema=None) as batch_op:
        batch_op.drop_column("pages_json")
        batch_op.drop_column("page_count")
