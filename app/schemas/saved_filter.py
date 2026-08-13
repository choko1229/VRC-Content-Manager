from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class SavedFilterRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    query_string: str
