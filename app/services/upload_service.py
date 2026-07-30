"""Streams an incoming multipart upload to a local temp file while validating it.

Content-Length is never trusted alone -- size is enforced against a running
byte counter while streaming, so a client that lies about Content-Length
(or omits it) still can't exceed the configured limit.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from pathlib import Path

from fastapi import UploadFile

from app.core.validation import (
    DEFAULT_ALLOWED_EXTENSIONS,
    UploadValidationError,
    sniff_and_verify,
    validate_extension,
)

logger = logging.getLogger(__name__)

_CHUNK_SIZE = 1024 * 1024
_SNIFF_HEADER_SIZE = 261  # enough for every signature filetype recognizes


@dataclass(slots=True)
class ValidatedUpload:
    path: Path
    original_filename: str
    size_bytes: int
    content_type: str | None
    extension: str


async def stream_and_validate_upload(
    file: UploadFile,
    *,
    dest_dir: Path,
    max_size_mb: int,
    allowed_extensions: frozenset[str] = DEFAULT_ALLOWED_EXTENSIONS,
) -> ValidatedUpload:
    extension = validate_extension(file.filename or "", allowed_extensions=allowed_extensions)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / f"{uuid.uuid4().hex}{extension}"
    max_bytes = max_size_mb * 1024 * 1024
    size = 0
    head_bytes = b""

    try:
        with open(dest_path, "wb") as fh:
            while True:
                chunk = await file.read(_CHUNK_SIZE)
                if not chunk:
                    break
                if len(head_bytes) < _SNIFF_HEADER_SIZE:
                    head_bytes += chunk[: _SNIFF_HEADER_SIZE - len(head_bytes)]
                size += len(chunk)
                if size > max_bytes:
                    raise UploadValidationError(f"ファイルサイズが上限({max_size_mb}MB)を超えています")
                fh.write(chunk)
    except Exception:
        dest_path.unlink(missing_ok=True)
        raise
    finally:
        await file.close()

    if size == 0:
        dest_path.unlink(missing_ok=True)
        raise UploadValidationError("空のファイルはアップロードできません")

    try:
        verified = sniff_and_verify(head_bytes, extension)
    except UploadValidationError:
        dest_path.unlink(missing_ok=True)
        raise
    if not verified:
        logger.warning("could not verify file signature for '%s' (extension %s); trusting extension", file.filename, extension)

    return ValidatedUpload(
        path=dest_path,
        original_filename=file.filename or dest_path.name,
        size_bytes=size,
        content_type=file.content_type,
        extension=extension,
    )


def cleanup_upload(upload: ValidatedUpload | None) -> None:
    if upload is not None:
        upload.path.unlink(missing_ok=True)
