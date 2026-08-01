"""add item description column

Revision ID: 52d6e4637e46
Revises: b986fdc96748
Create Date: 2026-08-01 22:10:18.172510

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '52d6e4637e46'
down_revision: Union[str, None] = 'b986fdc96748'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('items', sa.Column('description', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('items', 'description')
