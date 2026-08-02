from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models.item import ItemCategory
from app.models.license import TriState

# pending: primary file cached locally, not yet pushed to Drive (red)
# synced: pushed to Drive, no local download-cache copy right now (green)
# cached: pushed to Drive and currently has a fresh local download-cache copy (blue)
FileStatus = Literal["pending", "synced", "cached"]


class ItemCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=255)
    category: ItemCategory = ItemCategory.CLOTHING
    shop_name: str = Field(min_length=1, max_length=255)
    shop_url: str | None = Field(default=None, max_length=1024)
    product_url: str | None = Field(default=None, max_length=1024)
    download_source_url: str | None = Field(default=None, max_length=1024)
    purchase_date: date | None = None
    download_date: date | None = None
    price: int | None = Field(default=None, ge=0)
    status_code: str | None = None
    description: str | None = None
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
    category: ItemCategory
    shop_name: str | None
    status_label: str | None
    file_format: str | None
    is_favorite: bool
    has_thumbnail: bool
    tags: list[str]
    avatars: list[str]


class ItemUpdate(BaseModel):
    """Metadata-only edit -- the underlying file is not replaced here."""

    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=255)
    category: ItemCategory = ItemCategory.CLOTHING
    shop_name: str = Field(min_length=1, max_length=255)
    shop_url: str | None = Field(default=None, max_length=1024)
    product_url: str | None = Field(default=None, max_length=1024)
    download_source_url: str | None = Field(default=None, max_length=1024)
    purchase_date: date | None = None
    download_date: date | None = None
    price: int | None = Field(default=None, ge=0)
    status_code: str | None = None
    description: str | None = None
    memo: str | None = None
    is_favorite: bool = False
    tags: list[str] = Field(default_factory=list)
    avatars: list[str] = Field(default_factory=list)
    commercial_use: TriState = TriState.UNKNOWN
    modification_allowed: TriState = TriState.UNKNOWN
    redistribution_allowed: TriState = TriState.UNKNOWN
    credit_required: TriState = TriState.UNKNOWN
    license_note: str | None = None


class ItemSearchFilters(BaseModel):
    keyword: str | None = None
    tags: list[str] = Field(default_factory=list)
    avatars: list[str] = Field(default_factory=list)
    shop_id: int | None = None
    status_code: str | None = None
    category: ItemCategory | None = None
    favorites_only: bool = False


class ItemFileRead(BaseModel):
    id: int
    original_filename: str
    size_bytes: int


class ItemListRow(BaseModel):
    id: int
    name: str
    category: ItemCategory
    category_label: str
    shop_name: str | None
    status_label: str | None
    file_format: str | None
    price: int | None
    purchase_date: date | None
    is_favorite: bool
    has_thumbnail: bool
    tags: list[str]
    avatars: list[str]
    file_status: FileStatus | None
    primary_file_name: str | None
    attachment_files: list[ItemFileRead]


class UpdateHistoryRead(BaseModel):
    id: int
    checked_at: datetime
    note: str | None


class ItemDetail(BaseModel):
    id: int
    name: str
    category: ItemCategory
    category_label: str
    shop_id: int | None
    shop_name: str | None
    shop_url: str | None
    product_url: str | None
    download_source_url: str | None
    purchase_date: date | None
    download_date: date | None
    price: int | None
    file_format: str | None
    status_code: str | None
    status_label: str | None
    description: str | None
    memo: str | None
    is_favorite: bool
    has_thumbnail: bool
    tags: list[str]
    avatars: list[str]
    avatar_registration_name: str | None
    file_status: FileStatus | None
    primary_file_name: str | None
    attachment_files: list[ItemFileRead]
    commercial_use: TriState
    modification_allowed: TriState
    redistribution_allowed: TriState
    credit_required: TriState
    license_note: str | None
    update_history: list[UpdateHistoryRead]
