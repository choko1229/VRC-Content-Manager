from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"


@dataclass(frozen=True, slots=True)
class DriveFile:
    id: str
    name: str
    mime_type: str
    parent_id: str | None
    modified_time: datetime | None
    size_bytes: int | None


@dataclass(frozen=True, slots=True)
class StorageQuota:
    """Account-wide Drive usage (not scoped to this app's own folder) -- see
    DriveClient.get_storage_quota. limit_bytes is None for accounts with
    unlimited storage (Drive's about.get omits the field in that case)."""

    usage_bytes: int
    limit_bytes: int | None
