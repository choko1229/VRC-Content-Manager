from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict, Field

from app.models.license import TriState


class ItemCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=255)
    shop_name: str = Field(min_length=1, max_length=255)
    shop_url: str | None = Field(default=None, max_length=1024)
    product_url: str | None = Field(default=None, max_length=1024)
    download_source_url: str | None = Field(default=None, max_length=1024)
    purchase_date: date | None = None
    download_date: date | None = None
    price: int | None = Field(default=None, ge=0)
    status_code: str | None = None
    memo: str | None = None
    is_favorite: bool = False
    tags: list[str] = Field(default_factory=list)
    avatars: list[str] = Field(default_factory=list)
    commercial_use: TriState = TriState.UNKNOWN
    modification_allowed: TriState = TriState.UNKNOWN
    redistribution_allowed: TriState = TriState.UNKNOWN
    credit_required: TriState = TriState.UNKNOWN
    license_note: str | None = None


class ItemRead(BaseModel):
    id: int
    name: str
    shop_name: str | None
    status_label: str | None
    file_format: str | None
    is_favorite: bool
    has_thumbnail: bool
    tags: list[str]
    avatars: list[str]
