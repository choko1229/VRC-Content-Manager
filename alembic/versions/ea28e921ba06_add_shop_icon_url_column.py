"""add shop icon_url column

Revision ID: ea28e921ba06
Revises: 9f7e2f32894c
Create Date: 2026-08-01 23:35:15.095154

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'ea28e921ba06'
down_revision: Union[str, None] = '9f7e2f32894c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('shops', sa.Column('icon_url', sa.String(length=1024), nullable=True))


def downgrade() -> None:
    op.drop_column('shops', 'icon_url')
