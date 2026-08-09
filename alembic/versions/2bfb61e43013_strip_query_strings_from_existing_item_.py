"""strip query strings from existing item product_url values

Revision ID: 2bfb61e43013
Revises: 3da8b98848db
Create Date: 2026-08-09 13:00:01.007516

"""
from __future__ import annotations

from typing import Sequence, Union
from urllib.parse import urlsplit, urlunsplit

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2bfb61e43013'
down_revision: Union[str, None] = '3da8b98848db'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

items_table = sa.table(
    "items",
    sa.column("id", sa.Integer),
    sa.column("product_url", sa.String),
)


def _strip_query(value: str) -> str:
    parts = urlsplit(value)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def upgrade() -> None:
    # One-time cleanup to match the new save-time normalization (see
    # app/schemas/item.py: strip_url_query) -- existing rows saved before
    # that validator was added may carry tracking query strings (?utm_...)
    # pasted in from search results or shared links.
    conn = op.get_bind()
    rows = conn.execute(
        sa.select(items_table.c.id, items_table.c.product_url).where(
            items_table.c.product_url.is_not(None)
        )
    ).fetchall()
    for row in rows:
        cleaned = _strip_query(row.product_url)
        if cleaned != row.product_url:
            conn.execute(
                items_table.update().where(items_table.c.id == row.id).values(product_url=cleaned)
            )


def downgrade() -> None:
    # Query strings are noise, not meaningful data -- not worth restoring.
    pass
