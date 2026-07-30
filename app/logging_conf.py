"""Logging setup.

Logs go to stdout only (12-factor style) since Pterodactyl captures container
stdout — writing log files inside the container would be lost/unbounded.
A redaction filter is applied as defense-in-depth so OAuth tokens can never
leak into logs even if a future call site accidentally logs a credentials
object instead of a summary of it.
"""

from __future__ import annotations

import logging
import logging.config
import re

_REDACT_PATTERNS = [
    re.compile(r"(access_token['\"]?\s*[:=]\s*['\"]?)[^'\"\s,}]+", re.IGNORECASE),
    re.compile(r"(refresh_token['\"]?\s*[:=]\s*['\"]?)[^'\"\s,}]+", re.IGNORECASE),
    re.compile(r"(client_secret['\"]?\s*[:=]\s*['\"]?)[^'\"\s,}]+", re.IGNORECASE),
]
_REDACTED = r"\1***REDACTED***"


class SecretRedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = message
        for pattern in _REDACT_PATTERNS:
            redacted = pattern.sub(_REDACTED, redacted)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


def configure_logging(level: str = "INFO") -> None:
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "filters": {
                "redact_secrets": {"()": SecretRedactionFilter},
            },
            "formatters": {
                "default": {
                    "format": "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
                },
            },
            "handlers": {
                "stdout": {
                    "class": "logging.StreamHandler",
                    "formatter": "default",
                    "filters": ["redact_secrets"],
                    "stream": "ext://sys.stdout",
                },
            },
            "root": {
                "handlers": ["stdout"],
                "level": level,
            },
            "loggers": {
                "googleapiclient": {"level": "WARNING"},
                "google_auth_httplib2": {"level": "WARNING"},
                "urllib3": {"level": "WARNING"},
                "uvicorn.access": {"level": "INFO"},
            },
        }
    )
