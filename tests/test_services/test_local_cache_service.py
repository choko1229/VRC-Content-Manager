from __future__ import annotations

import os
import time

import pytest

from app.services import local_cache_service


@pytest.fixture(autouse=True)
def _isolated_data_dir(configured_settings):
    yield configured_settings


def test_pending_upload_path_creates_directory(configured_settings) -> None:
    path = local_cache_service.pending_upload_path("abc123.zip")

    assert path.parent.is_dir()
    assert path == configured_settings.data_dir / "cache" / "uploads" / "abc123.zip"


def test_download_cache_path_creates_directory(configured_settings) -> None:
    path = local_cache_service.download_cache_path("drive-file-id")

    assert path.parent.is_dir()
    assert path == configured_settings.data_dir / "cache" / "downloads" / "drive-file-id"


def test_is_download_cached_false_when_missing() -> None:
    assert local_cache_service.is_download_cached("nonexistent") is False


def test_is_download_cached_true_for_fresh_file() -> None:
    path = local_cache_service.download_cache_path("fresh-file")
    path.write_bytes(b"data")

    assert local_cache_service.is_download_cached("fresh-file") is True


def test_is_download_cached_false_for_expired_file() -> None:
    path = local_cache_service.download_cache_path("stale-file")
    path.write_bytes(b"data")
    old_time = time.time() - local_cache_service.DOWNLOAD_CACHE_TTL_SECONDS - 1
    os.utime(path, (old_time, old_time))

    assert local_cache_service.is_download_cached("stale-file") is False


def test_purge_expired_downloads_removes_only_stale_entries() -> None:
    fresh = local_cache_service.download_cache_path("fresh")
    fresh.write_bytes(b"data")
    stale = local_cache_service.download_cache_path("stale")
    stale.write_bytes(b"data")
    old_time = time.time() - local_cache_service.DOWNLOAD_CACHE_TTL_SECONDS - 1
    os.utime(stale, (old_time, old_time))

    removed = local_cache_service.purge_expired_downloads()

    assert removed == 1
    assert fresh.exists()
    assert not stale.exists()


def test_purge_expired_downloads_is_a_noop_when_cache_dir_missing() -> None:
    assert local_cache_service.purge_expired_downloads() == 0
