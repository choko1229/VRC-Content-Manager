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
