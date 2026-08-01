from __future__ import annotations

from pydantic import BaseModel


class AvatarRead(BaseModel):
    id: int
    item_id: int
    name: str
    memo: str | None
    has_thumbnail: bool
    compatible_item_count: int
