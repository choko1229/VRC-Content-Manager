"""DriveClient abstraction.

All Drive access in the app goes through this Protocol so tests can run
against FakeDriveClient (app/drive/fake_drive_client.py) with no network or
live Google account involved. GoogleDriveClient (real implementation) and
FakeDriveClient must both satisfy this interface exactly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from app.drive.types import DriveFile


class DriveClient(Protocol):
    def get_or_create_folder(self, name: str, parent_id: str | None = None) -> str:
        """Return the id of a folder named `name` under `parent_id` (root if None), creating it if absent."""
        ...

    def list_folder(self, parent_id: str) -> list[DriveFile]:
        """List the immediate (non-recursive) children of a folder, files and subfolders alike.

        Subfolders have mime_type == app.drive.types.FOLDER_MIME_TYPE; trashed
        items are excluded. Used for reconciliation against files added/moved/
        removed directly in Drive, outside the app.
        """
        ...

    def upload_file(
        self,
        *,
        local_path: Path,
        name: str,
        parent_id: str,
        mime_type: str | None = None,
    ) -> DriveFile:
        """Upload a new file. Returns the created DriveFile."""
        ...

    def update_file_content(self, *, file_id: str, local_path: Path, mime_type: str | None = None) -> DriveFile:
        """Replace the content of an existing file in place."""
        ...

    def download_file(self, *, file_id: str, dest_path: Path) -> None:
        """Download a file's content to a local path."""
        ...

    def get_metadata(self, file_id: str) -> DriveFile:
        """Fetch current metadata (including modified_time) for a file."""
        ...

    def delete_file(self, file_id: str) -> None:
        """Delete a file from Drive. Used for ingest-failure compensation."""
        ...

    def move_file(self, *, file_id: str, new_parent_id: str, old_parent_id: str) -> None:
        """Move a file to a new parent folder (re-parent, not copy). Used to
        relocate a file into place after intake, and to migrate legacy files
        into the current folder layout."""
        ...
