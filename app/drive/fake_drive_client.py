from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from app.core.exceptions import DriveError
from app.drive.types import FOLDER_MIME_TYPE as _FOLDER_MIME_TYPE
from app.drive.types import DriveFile, StorageQuota


@dataclass
class _StoredFile:
    id: str
    name: str
    mime_type: str
    parent_id: str | None
    content: bytes
    modified_time: datetime


class FakeDriveClient:
    """In-memory stand-in for GoogleDriveClient, used in all automated tests."""

    def __init__(self, *, storage_limit_bytes: int | None = 15 * 1024**3) -> None:
        self._files: dict[str, _StoredFile] = {}
        self._storage_limit_bytes = storage_limit_bytes

    def _new_id(self) -> str:
        return uuid.uuid4().hex

    def _to_drive_file(self, stored: _StoredFile) -> DriveFile:
        return DriveFile(
            id=stored.id,
            name=stored.name,
            mime_type=stored.mime_type,
            parent_id=stored.parent_id,
            modified_time=stored.modified_time,
            size_bytes=len(stored.content),
        )

    def get_or_create_folder(self, name: str, parent_id: str | None = None) -> str:
        for stored in self._files.values():
            if stored.mime_type == _FOLDER_MIME_TYPE and stored.name == name and stored.parent_id == parent_id:
                return stored.id

        folder_id = self._new_id()
        self._files[folder_id] = _StoredFile(
            id=folder_id,
            name=name,
            mime_type=_FOLDER_MIME_TYPE,
            parent_id=parent_id,
            content=b"",
            modified_time=datetime.now(timezone.utc),
        )
        return folder_id

    def list_folder(self, parent_id: str) -> list[DriveFile]:
        return [self._to_drive_file(stored) for stored in self._files.values() if stored.parent_id == parent_id]

    def upload_file(
        self,
        *,
        local_path: Path,
        name: str,
        parent_id: str,
        mime_type: str | None = None,
    ) -> DriveFile:
        if not local_path.exists():
            raise DriveError(f"local file does not exist: {local_path}")
        file_id = self._new_id()
        stored = _StoredFile(
            id=file_id,
            name=name,
            mime_type=mime_type or "application/octet-stream",
            parent_id=parent_id,
            content=local_path.read_bytes(),
            modified_time=datetime.now(timezone.utc),
        )
        self._files[file_id] = stored
        return self._to_drive_file(stored)

    def update_file_content(self, *, file_id: str, local_path: Path, mime_type: str | None = None) -> DriveFile:
        stored = self._files.get(file_id)
        if stored is None:
            raise DriveError(f"file not found: {file_id}")
        stored.content = local_path.read_bytes()
        stored.modified_time = datetime.now(timezone.utc)
        if mime_type:
            stored.mime_type = mime_type
        return self._to_drive_file(stored)

    def download_file(self, *, file_id: str, dest_path: Path) -> None:
        stored = self._files.get(file_id)
        if stored is None:
            raise DriveError(f"file not found: {file_id}")
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(stored.content)

    def get_metadata(self, file_id: str) -> DriveFile:
        stored = self._files.get(file_id)
        if stored is None:
            raise DriveError(f"file not found: {file_id}")
        return self._to_drive_file(stored)

    def delete_file(self, file_id: str) -> None:
        if file_id not in self._files:
            raise DriveError(f"file not found: {file_id}")
        del self._files[file_id]

    def move_file(self, *, file_id: str, new_parent_id: str, old_parent_id: str) -> None:
        stored = self._files.get(file_id)
        if stored is None:
            raise DriveError(f"file not found: {file_id}")
        stored.parent_id = new_parent_id

    def get_storage_quota(self) -> StorageQuota:
        usage = sum(len(stored.content) for stored in self._files.values() if stored.mime_type != _FOLDER_MIME_TYPE)
        return StorageQuota(usage_bytes=usage, limit_bytes=self._storage_limit_bytes)

    # --- test-only helpers ---
    def _debug_content(self, file_id: str) -> bytes:
        return self._files[file_id].content
