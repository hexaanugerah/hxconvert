"""add progress stage

Revision ID: c3d4e5f6a7b8
Revises: ad7bf36c83cc
Create Date: 2026-10-27 19:30:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c3d4e5f6a7b8'
down_revision: Union[str, None] = 'ad7bf36c83cc'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('conversion_jobs', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('stage', sa.String(length=32), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table('conversion_jobs', schema=None) as batch_op:
        batch_op.drop_column('stage')
