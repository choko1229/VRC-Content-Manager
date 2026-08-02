"""add item category

Revision ID: 77bf64c210d6
Revises: 1a61172dba18
Create Date: 2026-08-02 20:55:03.290752

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '77bf64c210d6'
down_revision: Union[str, None] = '1a61172dba18'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # server_default backfills existing rows to CLOTHING (the historical
    # implicit default -- most items to date are avatar clothing/
    # accessories) since this is NOT NULL; matches the enum member's stored
    # name (SQLAlchemy's Enum(native_enum=False) stores the member name,
    # not .value -- see FileRole/TriState in the initial schema migration).
    op.add_column(
        "items",
        sa.Column(
            "category",
            sa.Enum("CLOTHING", "AVATAR", "TOOL", "MA_EXTENSION", "SHADER_EXTENSION", "OTHER", name="itemcategory", native_enum=False, length=32),
            nullable=False,
            server_default="CLOTHING",
        ),
    )


def downgrade() -> None:
    op.drop_column("items", "category")
