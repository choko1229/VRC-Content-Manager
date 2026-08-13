"""add saved filters table

Revision ID: 9697c0375f56
Revises: 2bfb61e43013
Create Date: 2026-08-13 13:08:09.255419

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9697c0375f56'
down_revision: Union[str, None] = '2bfb61e43013'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Autogenerate also proposed dropping ix_item_avatars_avatar_id /
    # ix_item_tags_tag_id / ix_items_shop_id / ix_items_status_id -- pre-
    # existing schema drift unrelated to this table, left alone here.
    op.create_table('saved_filters',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('query_string', sa.String(length=2048), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('name')
    )


def downgrade() -> None:
    op.drop_table('saved_filters')
