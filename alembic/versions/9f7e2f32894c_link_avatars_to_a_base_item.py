"""link avatars to a base item

Revision ID: 9f7e2f32894c
Revises: 52d6e4637e46
Create Date: 2026-08-01 22:47:48.180674

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9f7e2f32894c'
down_revision: Union[str, None] = '52d6e4637e46'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # SQLite can't ALTER TABLE ADD CONSTRAINT directly -- batch mode recreates
    # the table instead.
    with op.batch_alter_table("avatars", schema=None) as batch_op:
        batch_op.add_column(sa.Column("item_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("memo", sa.Text(), nullable=True))
        batch_op.create_unique_constraint("uq_avatars_item_id", ["item_id"])
        batch_op.create_foreign_key(
            "fk_avatars_item_id_items", "items", ["item_id"], ["id"], ondelete="CASCADE"
        )


def downgrade() -> None:
    with op.batch_alter_table("avatars", schema=None) as batch_op:
        batch_op.drop_constraint("fk_avatars_item_id_items", type_="foreignkey")
        batch_op.drop_constraint("uq_avatars_item_id", type_="unique")
        batch_op.drop_column("memo")
        batch_op.drop_column("item_id")
