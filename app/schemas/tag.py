from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class TagRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    item_count: int = 0
