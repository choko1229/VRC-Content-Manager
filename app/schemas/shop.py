from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ShopCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=255)
    url: str | None = Field(default=None, max_length=1024)
    memo: str | None = None


class ShopUpdate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=255)
    url: str | None = Field(default=None, max_length=1024)
    memo: str | None = None


class ShopRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    url: str | None
    memo: str | None
    item_count: int = 0
