"""Chunked upload support, so a single file can exceed whatever per-request
body-size limit sits in front of the app.

Cloudflare (and similar proxies/CDNs) enforce a hard, plan-based cap on
proxied request bodies (100MB on Free/Pro) that no app- or origin-side
setting can raise -- a single-request upload of a file bigger than that cap
is rejected before it ever reaches this app. The browser instead splits the
file into fixed-size chunks and POSTs them one at a time, each comfortably
under that cap; this module buffers them to disk under a per-upload session
id and reassembles them into a single file once every chunk has arrived,
producing the same ValidatedUpload the existing single-request path
(app/services/upload_service.py) already produces -- item_service and
everything downstream of it don't need to know a file arrived in pieces.

Sessions live in memory (single-process app, same assumption
upload_sync_service's claim set already relies on) and are swept if
abandoned -- browser closed mid-upload, network dropped -- by purge_loop.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from starlette.requests import ClientDisconnect, Request

from app.config import get_settings
from app.core.validation import (
    DEFAULT_ALLOWED_EXTENSIONS,
    UploadValidationError,
    sniff_and_verify,
    validate_extension,
    validate_size,
)
from app.services.upload_service import ValidatedUpload

logger = logging.getLogger(__name__)

CHUNK_SIZE_MB = 20
_SESSION_TTL_SECONDS = 6 * 60 * 60  # abandoned sessions are swept after 6h
_PURGE_INTERVAL_SECONDS = 30 * 60
_SNIFF_HEADER_SIZE = 261  # enough for every signature filetype recognizes


@dataclass(slots=True)
class _Session:
    original_filename: str
    extension: str
    total_size: int
    max_size_mb: int
    dir_path: Path
    created_at: float = field(default_factory=time.monotonic)


_lock = threading.Lock()
_sessions: dict[str, _Session] = {}


def _sessions_root() -> Path:
    root = get_settings().data_dir / "tmp" / "chunked_uploads"
    root.mkdir(parents=True, exist_ok=True)
    return root


def init_session(*, filename: str, total_size: int, max_size_mb: int) -> str:
    """Validates the declared filename/size up front (same checks the
    single-request path applies) and opens a session directory for chunks
    to land in. Raises UploadValidationError on an invalid extension or a
    declared size already over the limit."""
    extension = validate_extension(filename, allowed_extensions=DEFAULT_ALLOWED_EXTENSIONS)
    validate_size(total_size, max_size_mb=max_size_mb)

    upload_id = uuid.uuid4().hex
    dir_path = _sessions_root() / upload_id
    dir_path.mkdir(parents=True, exist_ok=True)

    with _lock:
        _sessions[upload_id] = _Session(
            original_filename=filename,
            extension=extension,
            total_size=total_size,
            max_size_mb=max_size_mb,
            dir_path=dir_path,
        )
    return upload_id


def _get_session(upload_id: str) -> _Session:
    with _lock:
        session = _sessions.get(upload_id)
    if session is None:
        raise UploadValidationError("アップロードセッションが見つかりません。最初からやり直してください。")
    return session


def _received_bytes(session: _Session) -> int:
    return sum(p.stat().st_size for p in session.dir_path.glob("*.part"))


async def write_chunk(upload_id: str, chunk_index: int, request: Request) -> None:
    """Streams one chunk's body straight to disk (never buffered fully in
    memory) -- the declared total size is trusted for the session-level cap
    check below, but the actual bytes written are what's fed back into
    complete_session's own byte-count verification, so a client that lies
    about size still can't produce a file that isn't what it appears to be."""
    session = _get_session(upload_id)
    chunk_path = session.dir_path / f"{chunk_index:06d}.part"
    max_bytes = session.max_size_mb * 1024 * 1024
    already_received = _received_bytes(session)

    size = 0
    try:
        with open(chunk_path, "wb") as fh:
            async for piece in request.stream():
                size += len(piece)
                if already_received + size > max_bytes:
                    raise UploadValidationError(f"ファイルサイズが上限({session.max_size_mb}MB)を超えています")
                fh.write(piece)
    except ClientDisconnect:
        # The browser tab closed, network dropped, etc. mid-chunk -- expected
        # and unremarkable (there's no client left to see a response either
        # way), not a server bug, so this doesn't propagate to the generic
        # unhandled-exception handler's ERROR-level traceback log. The
        # abandoned session itself is swept later by purge_loop.
        chunk_path.unlink(missing_ok=True)
        logger.info(
            "chunked_upload_service: client disconnected mid-chunk (upload_id=%s chunk_index=%s)",
            upload_id,
            chunk_index,
        )
    except Exception:
        chunk_path.unlink(missing_ok=True)
        raise


def complete_session(upload_id: str) -> ValidatedUpload:
    """Concatenates every chunk (in index order, via the zero-padded
    filename) into a single file in the same tmp dir the single-request
    path uses, then runs it through the same size/signature verification
    before handing back a ValidatedUpload -- from here on, a chunked upload
    is indistinguishable from a single-request one."""
    session = _get_session(upload_id)
    received = _received_bytes(session)
    if received != session.total_size:
        abort_session(upload_id)
        raise UploadValidationError(
            f"アップロードが不完全です(受信 {received} / {session.total_size} バイト)。もう一度お試しください。"
        )

    chunk_paths = sorted(session.dir_path.glob("*.part"))
    settings = get_settings()
    settings.upload_tmp_dir.mkdir(parents=True, exist_ok=True)
    dest_path = settings.upload_tmp_dir / f"{uuid.uuid4().hex}{session.extension}"

    head_bytes = b""
    try:
        with open(dest_path, "wb") as out:
            for chunk_path in chunk_paths:
                with open(chunk_path, "rb") as part:
                    while True:
                        piece = part.read(1024 * 1024)
                        if not piece:
                            break
                        if len(head_bytes) < _SNIFF_HEADER_SIZE:
                            head_bytes += piece[: _SNIFF_HEADER_SIZE - len(head_bytes)]
                        out.write(piece)
    except Exception:
        dest_path.unlink(missing_ok=True)
        abort_session(upload_id)
        raise

    try:
        verified = sniff_and_verify(head_bytes, session.extension)
    except UploadValidationError:
        dest_path.unlink(missing_ok=True)
        abort_session(upload_id)
        raise
    if not verified:
        logger.warning(
            "could not verify file signature for '%s' (extension %s); trusting extension",
            session.original_filename,
            session.extension,
        )

    upload = ValidatedUpload(
        path=dest_path,
        original_filename=session.original_filename,
        size_bytes=session.total_size,
        content_type=None,
        extension=session.extension,
    )
    abort_session(upload_id)  # chunk dir is no longer needed now that reassembly succeeded
    return upload


def abort_session(upload_id: str) -> None:
    with _lock:
        session = _sessions.pop(upload_id, None)
    if session is not None:
        shutil.rmtree(session.dir_path, ignore_errors=True)


def purge_stale_sessions() -> int:
    now = time.monotonic()
    with _lock:
        stale_ids = [uid for uid, s in _sessions.items() if now - s.created_at > _SESSION_TTL_SECONDS]
    for upload_id in stale_ids:
        logger.info("chunked_upload_service: purging abandoned upload session %s", upload_id)
        abort_session(upload_id)
    return len(stale_ids)


async def purge_loop(stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=_PURGE_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass
        if stop_event.is_set():
            break
        try:
            await asyncio.to_thread(purge_stale_sessions)
        except Exception:
            logger.exception("chunked_upload_service: purge sweep failed")
