from __future__ import annotations

import io
from pathlib import Path

import pytest
from starlette.datastructures import Headers, UploadFile

from app.core.exceptions import ValidationError
from app.services import upload_service

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def _upload_file(content: bytes, filename: str, content_type: str) -> UploadFile:
    return UploadFile(
        file=io.BytesIO(content),
        filename=filename,
        headers=Headers({"content-type": content_type}),
    )


async def test_stream_and_validate_upload_succeeds_for_valid_png(tmp_path: Path) -> None:
    upload = await upload_service.stream_and_validate_upload(
        _upload_file(PNG_BYTES, "thumb.png", "image/png"),
        dest_dir=tmp_path,
        max_size_mb=10,
    )

    assert upload.path.exists()
    assert upload.path.read_bytes() == PNG_BYTES
    assert upload.size_bytes == len(PNG_BYTES)
    assert upload.extension == ".png"


async def test_stream_and_validate_upload_rejects_disallowed_extension(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        await upload_service.stream_and_validate_upload(
            _upload_file(b"data", "virus.exe", "application/octet-stream"),
            dest_dir=tmp_path,
            max_size_mb=10,
        )
    assert list(tmp_path.iterdir()) == []


async def test_stream_and_validate_upload_rejects_oversized_file(tmp_path: Path) -> None:
    big_content = b"\x89PNG\r\n\x1a\n" + b"a" * (2 * 1024 * 1024)

    with pytest.raises(ValidationError):
        await upload_service.stream_and_validate_upload(
            _upload_file(big_content, "big.png", "image/png"),
            dest_dir=tmp_path,
            max_size_mb=1,
        )
    assert list(tmp_path.iterdir()) == []  # temp file cleaned up on failure


async def test_stream_and_validate_upload_rejects_signature_mismatch(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        await upload_service.stream_and_validate_upload(
            _upload_file(PNG_BYTES, "fake.zip", "application/zip"),
            dest_dir=tmp_path,
            max_size_mb=10,
        )
    assert list(tmp_path.iterdir()) == []


def test_cleanup_upload_removes_file(tmp_path: Path) -> None:
    from app.services.upload_service import ValidatedUpload

    path = tmp_path / "x.png"
    path.write_bytes(b"x")
    upload = ValidatedUpload(path=path, original_filename="x.png", size_bytes=1, content_type="image/png", extension=".png")

    upload_service.cleanup_upload(upload)

    assert not path.exists()


def test_cleanup_upload_handles_none() -> None:
    upload_service.cleanup_upload(None)  # no raise
