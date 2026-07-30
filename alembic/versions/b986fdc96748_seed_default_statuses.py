"""seed default statuses

Revision ID: b986fdc96748
Revises: 6a32c9c642ad
Create Date: 2026-07-31 06:40:00.000000

"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "b986fdc96748"
down_revision: Union[str, None] = "6a32c9c642ad"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

statuses_table = sa.table(
    "statuses",
    sa.column("code", sa.String),
    sa.column("label", sa.String),
    sa.column("sort_order", sa.Integer),
    sa.column("is_default", sa.Boolean),
    sa.column("created_at", sa.DateTime),
)

# code must match app.models.status.DEFAULT_STATUS_CODES; kept as literal
# strings here (not imported) since migrations must stay stable even if the
# model constant changes later.
SEED_STATUSES = [
    {"code": "unorganized", "label": "未整理", "sort_order": 0, "is_default": True},
    {"code": "imported", "label": "インポート済み", "sort_order": 1, "is_default": False},
    {"code": "in_use", "label": "使用中", "sort_order": 2, "is_default": False},
    {"code": "archived", "label": "アーカイブ", "sort_order": 3, "is_default": False},
]


def upgrade() -> None:
    now = datetime.now(timezone.utc)
    op.bulk_insert(
        statuses_table,
        [{**row, "created_at": now} for row in SEED_STATUSES],
    )


def downgrade() -> None:
    conn = op.get_bind()
    codes = [row["code"] for row in SEED_STATUSES]
    conn.execute(
        statuses_table.delete().where(statuses_table.c.code.in_(codes))
    )
