"""Upload validation: extension allowlist, size cap, magic-byte sniffing.

v1 deliberately never inspects zip contents (no `zipfile.extractall`, no
`namelist()` walk) -- files are stored/retrieved as opaque blobs, which
sidesteps zip-bomb and path-traversal-on-extract risk entirely. If a future
"preview zip contents" feature is added, that code must use read-only
listing with a size-ratio guard and member-name sanitization before ever
extracting anything.
"""

from __future__ import annotations

import filetype

from app.core.exceptions import ValidationError

DEFAULT_ALLOWED_EXTENSIONS = frozenset(
    {".zip", ".unitypackage", ".vrm", ".fbx", ".png", ".jpg", ".jpeg"}
)

# filetype signatures we can reliably verify. .unitypackage is gzip under the
# hood; .fbx has both binary (has a magic header) and ASCII variants -- ASCII
# FBX has no reliable signature, so it's allowed through on extension alone
# (logged as a warning by the caller).
_EXPECTED_KIND_EXTENSIONS: dict[str, frozenset[str]] = {
    "zip": frozenset({".zip", ".unitypackage"}),
    "gz": frozenset({".unitypackage"}),
    "glb": frozenset({".vrm"}),
    "png": frozenset({".png"}),
    "jpg": frozenset({".jpg", ".jpeg"}),
}

UNVERIFIABLE_EXTENSIONS = frozenset({".fbx"})


class UploadValidationError(ValidationError):
    pass


def extension_of(filename: str) -> str:
    lower = filename.lower()
    for ext in sorted(DEFAULT_ALLOWED_EXTENSIONS, key=len, reverse=True):
        if lower.endswith(ext):
            return ext
    idx = lower.rfind(".")
    return lower[idx:] if idx != -1 else ""


def validate_extension(filename: str, *, allowed_extensions: frozenset[str] = DEFAULT_ALLOWED_EXTENSIONS) -> str:
    ext = extension_of(filename)
    if ext not in allowed_extensions:
        raise UploadValidationError(
            f"許可されていないファイル形式です: {ext or '(拡張子なし)'} "
            f"(許可: {', '.join(sorted(allowed_extensions))})"
        )
    return ext


def validate_size(size_bytes: int, *, max_size_mb: int) -> None:
    max_bytes = max_size_mb * 1024 * 1024
    if size_bytes > max_bytes:
        raise UploadValidationError(f"ファイルサイズが上限({max_size_mb}MB)を超えています")
    if size_bytes == 0:
        raise UploadValidationError("空のファイルはアップロードできません")


def sniff_and_verify(data_head: bytes, extension: str) -> bool:
    """Returns True if the magic bytes matched the extension, False if unverifiable
    (extension has no reliable signature). Raises UploadValidationError on a mismatch."""
    if extension in UNVERIFIABLE_EXTENSIONS:
        return False

    kind = filetype.guess(data_head)
    if kind is None:
        # No recognizable signature at all; treat like an unverifiable format
        # rather than hard-failing, since some legitimate small/edge-case
        # files may not have enough header bytes for filetype to key off of.
        return False

    allowed_exts_for_kind = _EXPECTED_KIND_EXTENSIONS.get(kind.extension, frozenset())
    if extension not in allowed_exts_for_kind:
        raise UploadValidationError(
            f"ファイルの内容が拡張子と一致しません(拡張子: {extension}, 検出: {kind.extension})"
        )
    return True
