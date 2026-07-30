"""Import every model so `Base.metadata` is fully populated for Alembic autogenerate."""

from app.db.base import Base
from app.models.app_setting import AppSetting
from app.models.avatar import Avatar, item_avatars
from app.models.item import Item
from app.models.item_dependency import ItemDependency
from app.models.item_file import FileRole, ItemFile
from app.models.license import License, TriState
from app.models.oauth_credential import OAuthCredential
from app.models.shop import Shop
from app.models.status import DEFAULT_STATUS_CODES, Status
from app.models.tag import Tag, item_tags
from app.models.update_history import UpdateHistory

__all__ = [
    "Base",
    "AppSetting",
    "Avatar",
    "item_avatars",
    "Item",
    "ItemDependency",
    "FileRole",
    "ItemFile",
    "License",
    "TriState",
    "OAuthCredential",
    "Shop",
    "DEFAULT_STATUS_CODES",
    "Status",
    "Tag",
    "item_tags",
    "UpdateHistory",
]
