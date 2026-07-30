from __future__ import annotations

import pytest

from app.core.exceptions import ValidationError
from app.core.validation import (
    extension_of,
    sniff_and_verify,
    validate_extension,
    validate_size,
)

PNG_HEADER = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPG_HEADER = b"\xff\xd8\xff\xe0" + b"\x00" * 32
ZIP_HEADER = b"PK\x03\x04" + b"\x00" * 32


def test_extension_of_recognizes_multi_part_extension() -> None:
    assert extension_of("MyAvatar.unitypackage") == ".unitypackage"
    assert extension_of("photo.PNG") == ".png"


def test_validate_extension_rejects_disallowed() -> None:
    with pytest.raises(ValidationError):
        validate_extension("virus.exe")


def test_validate_extension_accepts_allowed() -> None:
    assert validate_extension("model.vrm") == ".vrm"


def test_validate_size_rejects_over_limit() -> None:
    with pytest.raises(ValidationError):
        validate_size(11 * 1024 * 1024, max_size_mb=10)


def test_validate_size_rejects_empty() -> None:
    with pytest.raises(ValidationError):
        validate_size(0, max_size_mb=10)


def test_validate_size_accepts_within_limit() -> None:
    validate_size(5 * 1024 * 1024, max_size_mb=10)  # no raise


@pytest.mark.parametrize(
    "header,extension",
    [(PNG_HEADER, ".png"), (JPG_HEADER, ".jpg"), (ZIP_HEADER, ".zip"), (ZIP_HEADER, ".unitypackage")],
)
def test_sniff_and_verify_accepts_matching_signature(header: bytes, extension: str) -> None:
    assert sniff_and_verify(header, extension) is True


def test_sniff_and_verify_rejects_mismatched_signature() -> None:
    with pytest.raises(ValidationError):
        sniff_and_verify(PNG_HEADER, ".zip")


def test_sniff_and_verify_returns_false_for_unverifiable_extension() -> None:
    assert sniff_and_verify(b"arbitrary ascii fbx content", ".fbx") is False
