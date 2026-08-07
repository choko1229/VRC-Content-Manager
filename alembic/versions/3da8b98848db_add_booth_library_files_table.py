"""add booth library files table

Revision ID: 3da8b98848db
Revises: 77bf64c210d6
Create Date: 2026-08-07 23:29:43.713661

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3da8b98848db'
down_revision: Union[str, None] = '77bf64c210d6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('booth_library_files',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('filename', sa.String(length=512), nullable=False),
    sa.Column('product_url', sa.String(length=1024), nullable=False),
    sa.Column('product_name', sa.String(length=512), nullable=False),
    sa.Column('shop_name', sa.String(length=255), nullable=True),
    sa.Column('shop_url', sa.String(length=1024), nullable=True),
    sa.Column('thumbnail_url', sa.String(length=1024), nullable=True),
    sa.Column('synced_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_booth_library_files_filename'), 'booth_library_files', ['filename'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_booth_library_files_filename'), table_name='booth_library_files')
    op.drop_table('booth_library_files')
