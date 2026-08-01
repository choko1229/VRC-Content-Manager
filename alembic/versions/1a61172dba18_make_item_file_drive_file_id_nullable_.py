"""make item_file drive_file_id nullable, add synced_at

Revision ID: 1a61172dba18
Revises: ea28e921ba06
Create Date: 2026-08-02 02:04:33.476460

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '1a61172dba18'
down_revision: Union[str, None] = 'ea28e921ba06'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # SQLite can't ALTER COLUMN nullability directly -- batch mode recreates
    # the table instead. Existing rows already have a non-null drive_file_id
    # (every ItemFile up to now was created synchronously-synced), so
    # synced_at is backfilled to created_at for them rather than left NULL
    # (which would make drive_reconcile_service treat them as newly-pending).
    with op.batch_alter_table("item_files", schema=None) as batch_op:
        batch_op.add_column(sa.Column("synced_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.alter_column("drive_file_id", existing_type=sa.VARCHAR(length=128), nullable=True)

    op.execute("UPDATE item_files SET synced_at = created_at WHERE drive_file_id IS NOT NULL")


def downgrade() -> None:
    with op.batch_alter_table("item_files", schema=None) as batch_op:
        batch_op.alter_column("drive_file_id", existing_type=sa.VARCHAR(length=128), nullable=False)
        batch_op.drop_column("synced_at")
