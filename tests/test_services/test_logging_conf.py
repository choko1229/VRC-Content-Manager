from __future__ import annotations

import logging

import pytest

from app.logging_conf import SecretRedactionFilter


def _make_record(message: str) -> logging.LogRecord:
    return logging.LogRecord(
        name="test", level=logging.INFO, pathname=__file__, lineno=1, msg=message, args=(), exc_info=None
    )


@pytest.mark.parametrize(
    "message",
    [
        "token response: access_token=ya29.abc123XYZ",
        'credentials: {"access_token": "ya29.abc123XYZ", "refresh_token": "1//abc-def"}',
        "client_secret=GOCSPX-verysecretvalue in request",
    ],
)
def test_redaction_filter_masks_known_secret_fields(message: str) -> None:
    record = _make_record(message)

    SecretRedactionFilter().filter(record)

    rendered = record.getMessage()
    assert "REDACTED" in rendered
    assert "ya29" not in rendered or "abc123XYZ" not in rendered
    assert "GOCSPX-verysecretvalue" not in rendered
    assert "1//abc-def" not in rendered or "refresh_token" not in rendered.split("REDACTED")[0]


def test_redaction_filter_leaves_normal_messages_untouched() -> None:
    record = _make_record("item created id=5 name=Cool Avatar")

    SecretRedactionFilter().filter(record)

    assert record.getMessage() == "item created id=5 name=Cool Avatar"


def test_redaction_filter_always_returns_true_to_keep_the_record() -> None:
    record = _make_record("access_token=secret")

    assert SecretRedactionFilter().filter(record) is True
