"""Coverage for GoogleDriveClient.download_file -- the only Drive client
method with a filesystem side effect other services rely on existing being
either fully present or absent (see local_cache_service.is_download_cached,
which checks purely by path existence). No other test file exercises the
real chunked-download path at all (everything else uses FakeDriveClient),
so this is the only place a regression here would be caught."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from googleapiclient.errors import HttpError

from app.core.exceptions import DriveError
from app.drive import google_drive_client as gdc


class _FakeDownloader:
    """Stand-in for googleapiclient.http.MediaIoBaseDownload. `chunks` is
    the sequence of byte-strings successive next_chunk() calls write; if
    `fail_at` is set, the call at that index (0-based) raises HttpError
    instead of writing, simulating a chunk failing partway through --
    e.g. a rate limit that outlasted next_chunk's own num_retries."""

    def __init__(self, fh, request, *, chunks: list[bytes], fail_at: int | None = None):
        self._fh = fh
        self._chunks = chunks
        self._fail_at = fail_at
        self._calls = 0

    def next_chunk(self, num_retries: int = 0):
        call_index = self._calls
        self._calls += 1
        if self._fail_at is not None and call_index == self._fail_at:
            response = MagicMock()
            response.status = 500
            raise HttpError(response, b"internal error")
        self._fh.write(self._chunks[call_index])
        done = call_index == len(self._chunks) - 1
        return None, done


def _make_client(monkeypatch: pytest.MonkeyPatch, downloader_cls) -> gdc.GoogleDriveClient:
    client = gdc.GoogleDriveClient(credentials=MagicMock())
    fake_service = MagicMock()
    fake_service.files.return_value.get_media.return_value = MagicMock()
    monkeypatch.setattr(client, "_get_service", lambda: fake_service)
    monkeypatch.setattr(gdc, "MediaIoBaseDownload", downloader_cls)
    return client


def test_get_storage_quota_parses_usage_and_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    client = gdc.GoogleDriveClient(credentials=MagicMock())
    fake_service = MagicMock()
    fake_service.about.return_value.get.return_value.execute.return_value = {
        "storageQuota": {"usage": "12345", "limit": "16106127360"}
    }
    monkeypatch.setattr(client, "_get_service", lambda: fake_service)

    quota = client.get_storage_quota()

    assert quota.usage_bytes == 12345
    assert quota.limit_bytes == 16106127360
    fake_service.about.return_value.get.assert_called_once_with(fields="storageQuota")


def test_get_storage_quota_treats_missing_limit_as_unlimited(monkeypatch: pytest.MonkeyPatch) -> None:
    client = gdc.GoogleDriveClient(credentials=MagicMock())
    fake_service = MagicMock()
    fake_service.about.return_value.get.return_value.execute.return_value = {"storageQuota": {"usage": "500"}}
    monkeypatch.setattr(client, "_get_service", lambda: fake_service)

    quota = client.get_storage_quota()

    assert quota.usage_bytes == 500
    assert quota.limit_bytes is None


def test_get_storage_quota_wraps_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    client = gdc.GoogleDriveClient(credentials=MagicMock())
    fake_service = MagicMock()
    response = MagicMock()
    response.status = 500
    fake_service.about.return_value.get.return_value.execute.side_effect = HttpError(response, b"boom")
    monkeypatch.setattr(client, "_get_service", lambda: fake_service)

    with pytest.raises(DriveError):
        client.get_storage_quota()


def test_download_file_writes_full_content_on_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def factory(fh, request):
        return _FakeDownloader(fh, request, chunks=[b"hello ", b"world"])

    client = _make_client(monkeypatch, factory)
    dest = tmp_path / "out.zip"

    client.download_file(file_id="f1", dest_path=dest)

    assert dest.read_bytes() == b"hello world"


def test_download_file_leaves_no_file_when_a_chunk_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Reproduces the reported bug: a chunk failing partway through (a rate
    # limit outlasting the built-in retry) must not leave a truncated/empty
    # file sitting at dest_path -- callers cache purely by that path
    # existing, so a leftover file there would be served as if it were a
    # complete, successful download on every subsequent request.
    def factory(fh, request):
        return _FakeDownloader(fh, request, chunks=[b"hello ", b"world"], fail_at=1)

    client = _make_client(monkeypatch, factory)
    dest = tmp_path / "out.zip"

    with pytest.raises(DriveError):
        client.download_file(file_id="f1", dest_path=dest)

    assert not dest.exists()
    # No stray .part temp file left behind either.
    assert list(tmp_path.iterdir()) == []


def test_download_file_does_not_clobber_an_existing_cached_file_on_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # If a retry ever re-downloads into a path that (for whatever reason)
    # already has a valid cached file, a failed attempt must leave that
    # existing file alone rather than truncating it in place.
    def factory(fh, request):
        return _FakeDownloader(fh, request, chunks=[b"new partial"], fail_at=0)

    client = _make_client(monkeypatch, factory)
    dest = tmp_path / "out.zip"
    dest.write_bytes(b"already cached, valid content")

    with pytest.raises(DriveError):
        client.download_file(file_id="f1", dest_path=dest)

    assert dest.read_bytes() == b"already cached, valid content"


def test_download_file_passes_num_retries_to_next_chunk(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen_num_retries: list[int] = []

    class _RecordingDownloader(_FakeDownloader):
        def next_chunk(self, num_retries: int = 0):
            seen_num_retries.append(num_retries)
            return super().next_chunk(num_retries=num_retries)

    def factory(fh, request):
        return _RecordingDownloader(fh, request, chunks=[b"data"])

    client = _make_client(monkeypatch, factory)

    client.download_file(file_id="f1", dest_path=tmp_path / "out.zip")

    assert seen_num_retries == [gdc._DOWNLOAD_NUM_RETRIES]
    assert gdc._DOWNLOAD_NUM_RETRIES > 0
