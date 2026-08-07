from __future__ import annotations

import pytest
from starlette.requests import ClientDisconnect

from app.core.validation import UploadValidationError
from app.services import chunked_upload_service


@pytest.fixture(autouse=True)
def _isolated_data_dir(configured_settings):
    yield configured_settings


def test_init_session_creates_a_session_directory() -> None:
    upload_id = chunked_upload_service.init_session(filename="asset.zip", total_size=100, max_size_mb=500)

    assert upload_id
    session = chunked_upload_service._get_session(upload_id)
    assert session.dir_path.is_dir()
    assert session.original_filename == "asset.zip"
    assert session.extension == ".zip"


def test_init_session_rejects_disallowed_extension() -> None:
    with pytest.raises(UploadValidationError):
        chunked_upload_service.init_session(filename="virus.exe", total_size=100, max_size_mb=500)


def test_init_session_rejects_oversized_declared_total() -> None:
    with pytest.raises(UploadValidationError):
        chunked_upload_service.init_session(filename="asset.zip", total_size=600 * 1024 * 1024, max_size_mb=500)


def test_complete_session_reassembles_chunks_in_order() -> None:
    upload_id = chunked_upload_service.init_session(filename="asset.zip", total_size=8, max_size_mb=500)
    session = chunked_upload_service._get_session(upload_id)
    # Written out of order on purpose -- reassembly must sort by the
    # zero-padded chunk index in the filename, not directory listing order.
    (session.dir_path / "000001.part").write_bytes(b"BB")
    (session.dir_path / "000000.part").write_bytes(b"PK\x03\x04")
    (session.dir_path / "000002.part").write_bytes(b"CC")

    upload = chunked_upload_service.complete_session(upload_id)
    try:
        assert upload.path.read_bytes() == b"PK\x03\x04BBCC"
        assert upload.original_filename == "asset.zip"
        assert upload.extension == ".zip"
        assert upload.size_bytes == 8
    finally:
        upload.path.unlink(missing_ok=True)


def test_complete_session_raises_and_cleans_up_when_bytes_dont_match_declared_total() -> None:
    upload_id = chunked_upload_service.init_session(filename="asset.zip", total_size=100, max_size_mb=500)
    session = chunked_upload_service._get_session(upload_id)
    (session.dir_path / "000000.part").write_bytes(b"short")

    with pytest.raises(UploadValidationError):
        chunked_upload_service.complete_session(upload_id)

    assert not session.dir_path.exists()
    with pytest.raises(UploadValidationError):
        chunked_upload_service._get_session(upload_id)


def test_complete_session_removes_the_session_on_success() -> None:
    upload_id = chunked_upload_service.init_session(filename="asset.zip", total_size=4, max_size_mb=500)
    session = chunked_upload_service._get_session(upload_id)
    (session.dir_path / "000000.part").write_bytes(b"PK\x03\x04")

    upload = chunked_upload_service.complete_session(upload_id)
    upload.path.unlink(missing_ok=True)

    assert not session.dir_path.exists()
    with pytest.raises(UploadValidationError):
        chunked_upload_service._get_session(upload_id)


def test_get_session_raises_for_unknown_upload_id() -> None:
    with pytest.raises(UploadValidationError):
        chunked_upload_service._get_session("does-not-exist")


def test_abort_session_cleans_up_directory() -> None:
    upload_id = chunked_upload_service.init_session(filename="asset.zip", total_size=4, max_size_mb=500)
    session = chunked_upload_service._get_session(upload_id)

    chunked_upload_service.abort_session(upload_id)

    assert not session.dir_path.exists()
    with pytest.raises(UploadValidationError):
        chunked_upload_service._get_session(upload_id)


class _DisconnectingRequest:
    """Minimal stand-in for starlette.requests.Request whose body stream
    drops partway through, like a real browser tab closing mid-upload."""

    async def stream(self):
        yield b"partial data"
        raise ClientDisconnect()


async def test_write_chunk_swallows_client_disconnect_and_cleans_up_partial_file() -> None:
    upload_id = chunked_upload_service.init_session(filename="asset.zip", total_size=100, max_size_mb=500)
    session = chunked_upload_service._get_session(upload_id)

    await chunked_upload_service.write_chunk(upload_id, 0, _DisconnectingRequest())  # must not raise

    assert list(session.dir_path.glob("*.part")) == []


def test_purge_stale_sessions_removes_only_old_sessions() -> None:
    fresh_id = chunked_upload_service.init_session(filename="fresh.zip", total_size=4, max_size_mb=500)
    stale_id = chunked_upload_service.init_session(filename="stale.zip", total_size=4, max_size_mb=500)
    chunked_upload_service._sessions[stale_id].created_at -= chunked_upload_service._SESSION_TTL_SECONDS + 1

    removed = chunked_upload_service.purge_stale_sessions()

    assert removed == 1
    chunked_upload_service._get_session(fresh_id)  # still present, no raise
    with pytest.raises(UploadValidationError):
        chunked_upload_service._get_session(stale_id)
