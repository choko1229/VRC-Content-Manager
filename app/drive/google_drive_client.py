from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

from app.core.exceptions import DriveError
from app.drive.types import FOLDER_MIME_TYPE as _FOLDER_MIME_TYPE
from app.drive.types import DriveFile

logger = logging.getLogger(__name__)

_METADATA_FIELDS = "id, name, mimeType, parents, modifiedTime, size"

# Passed to MediaIoBaseDownload.next_chunk() below: googleapiclient's own
# retry loop backs off (randomized, roughly doubling each attempt) and
# retries a chunk on 5xx errors, 429, and 403 responses whose reason is
# userRateLimitExceeded/rateLimitExceeded -- i.e. exactly a Drive API rate
# limit, which is what a chunk failing partway through a download usually
# turns out to be. 8 retries gives a cumulative wait of several minutes in
# the worst case, which is preferable to failing the download outright.
_DOWNLOAD_NUM_RETRIES = 8


def _parse_drive_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _to_drive_file(raw: dict) -> DriveFile:
    parents = raw.get("parents") or []
    size = raw.get("size")
    return DriveFile(
        id=raw["id"],
        name=raw["name"],
        mime_type=raw.get("mimeType", ""),
        parent_id=parents[0] if parents else None,
        modified_time=_parse_drive_time(raw.get("modifiedTime")),
        size_bytes=int(size) if size is not None else None,
    )


class GoogleDriveClient:
    """Real Google Drive v3 implementation.

    Credentials are refreshed lazily before each Drive call. If a refresh
    rotates the access token, `on_credentials_refreshed` is invoked so the
    caller (oauth_service) can persist the new token immediately -- tokens
    are never left stale in storage after a successful refresh.
    """

    def __init__(
        self,
        credentials: Credentials,
        on_credentials_refreshed: Callable[[Credentials], None] | None = None,
    ) -> None:
        self._credentials = credentials
        self._on_credentials_refreshed = on_credentials_refreshed
        self._service = None

    def _get_service(self):
        if self._credentials.expired and self._credentials.refresh_token:
            logger.info("refreshing Drive OAuth token")
            self._credentials.refresh(GoogleAuthRequest())
            if self._on_credentials_refreshed:
                self._on_credentials_refreshed(self._credentials)
            self._service = None
        if self._service is None:
            self._service = build("drive", "v3", credentials=self._credentials, cache_discovery=False)
        return self._service

    def get_or_create_folder(self, name: str, parent_id: str | None = None) -> str:
        service = self._get_service()
        escaped_name = name.replace("'", "\\'")
        query_parts = [
            f"name = '{escaped_name}'",
            f"mimeType = '{_FOLDER_MIME_TYPE}'",
            "trashed = false",
        ]
        if parent_id:
            query_parts.append(f"'{parent_id}' in parents")
        query = " and ".join(query_parts)

        try:
            results = service.files().list(q=query, fields="files(id, name)", spaces="drive").execute()
            existing = results.get("files", [])
            if existing:
                return existing[0]["id"]

            metadata: dict = {"name": name, "mimeType": _FOLDER_MIME_TYPE}
            if parent_id:
                metadata["parents"] = [parent_id]
            created = service.files().create(body=metadata, fields="id").execute()
            logger.info("created Drive folder %s (id=%s)", name, created["id"])
            return created["id"]
        except HttpError as exc:
            raise DriveError(f"failed to get/create Drive folder '{name}'") from exc

    def list_folder(self, parent_id: str) -> list[DriveFile]:
        service = self._get_service()
        query = f"'{parent_id}' in parents and trashed = false"
        files: list[DriveFile] = []
        page_token: str | None = None
        try:
            while True:
                results = (
                    service.files()
                    .list(
                        q=query,
                        fields=f"nextPageToken, files({_METADATA_FIELDS})",
                        spaces="drive",
                        pageToken=page_token,
                    )
                    .execute()
                )
                files.extend(_to_drive_file(raw) for raw in results.get("files", []))
                page_token = results.get("nextPageToken")
                if not page_token:
                    break
        except HttpError as exc:
            raise DriveError(f"failed to list Drive folder {parent_id}") from exc
        return files

    def upload_file(
        self,
        *,
        local_path: Path,
        name: str,
        parent_id: str,
        mime_type: str | None = None,
    ) -> DriveFile:
        service = self._get_service()
        media = MediaFileUpload(str(local_path), mimetype=mime_type, resumable=True)
        try:
            raw = (
                service.files()
                .create(
                    body={"name": name, "parents": [parent_id]},
                    media_body=media,
                    fields=_METADATA_FIELDS,
                )
                .execute()
            )
            return _to_drive_file(raw)
        except HttpError as exc:
            raise DriveError(f"failed to upload '{name}' to Drive") from exc

    def update_file_content(self, *, file_id: str, local_path: Path, mime_type: str | None = None) -> DriveFile:
        service = self._get_service()
        media = MediaFileUpload(str(local_path), mimetype=mime_type, resumable=True)
        try:
            raw = service.files().update(fileId=file_id, media_body=media, fields=_METADATA_FIELDS).execute()
            return _to_drive_file(raw)
        except HttpError as exc:
            raise DriveError(f"failed to update Drive file {file_id}") from exc

    def download_file(self, *, file_id: str, dest_path: Path) -> None:
        """Downloads to a sibling temp file first, only replacing dest_path
        on full success. Callers cache purely by dest_path existing (see
        local_cache_service.is_download_cached) -- writing directly to it
        chunk-by-chunk meant a chunk failing partway through (a rate limit
        that outlasted its own retries, a dropped connection) left a
        truncated/empty file sitting exactly where it'd be served from on
        every subsequent request, indefinitely, since nothing ever checked
        that the "cached" copy was actually complete.
        """
        service = self._get_service()
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = dest_path.with_name(f"{dest_path.name}.{uuid.uuid4().hex}.part")
        try:
            request = service.files().get_media(fileId=file_id)
            with open(tmp_path, "wb") as fh:
                downloader = MediaIoBaseDownload(fh, request)
                done = False
                while not done:
                    _status, done = downloader.next_chunk(num_retries=_DOWNLOAD_NUM_RETRIES)
            tmp_path.replace(dest_path)
        except HttpError as exc:
            raise DriveError(f"failed to download Drive file {file_id}") from exc
        finally:
            tmp_path.unlink(missing_ok=True)

    def get_metadata(self, file_id: str) -> DriveFile:
        service = self._get_service()
        try:
            raw = service.files().get(fileId=file_id, fields=_METADATA_FIELDS).execute()
            return _to_drive_file(raw)
        except HttpError as exc:
            raise DriveError(f"failed to fetch metadata for Drive file {file_id}") from exc

    def delete_file(self, file_id: str) -> None:
        service = self._get_service()
        try:
            service.files().delete(fileId=file_id).execute()
        except HttpError as exc:
            raise DriveError(f"failed to delete Drive file {file_id}") from exc

    def move_file(self, *, file_id: str, new_parent_id: str, old_parent_id: str) -> None:
        service = self._get_service()
        try:
            result = (
                service.files()
                .update(fileId=file_id, addParents=new_parent_id, removeParents=old_parent_id, fields="id, parents")
                .execute()
            )
        except HttpError as exc:
            raise DriveError(f"failed to move Drive file {file_id}") from exc

        # Google's addParents/removeParents can each partially apply (e.g. the
        # OAuth grant covers writing the new parent but not detaching the old
        # one for a file the app didn't create) without the API call itself
        # raising -- verify the file actually ended up parented where
        # intended before reporting success, so a partial move is treated the
        # same as a failed one (caller retries later) instead of silently
        # leaving the file re-appearing in its old location forever.
        result_parents = result.get("parents") or []
        if new_parent_id not in result_parents or old_parent_id in result_parents:
            raise DriveError(
                f"move of Drive file {file_id} into {new_parent_id} did not fully apply "
                f"(parents now: {result_parents})"
            )
