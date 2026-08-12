"""Logging setup, with a redaction filter as the last line of defence.

Nothing in this project deliberately logs a token. The filter exists because
"deliberately" is not a guarantee: a stray exception message or a URL containing
a bot token would otherwise land in the journal in plain text.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from typing import Any

#: Env vars whose *values* must never reach a log line.
SECRET_ENV_VARS = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")

#: Telegram bot tokens look like `123456789:AAF...`. Redact them wherever they
#: appear, including inside an API URL, even if the value is not in our env.
_TOKEN_PATTERNS = (
    re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}"),
    re.compile(r"(?i)(bot)\d{6,12}:[A-Za-z0-9_-]{30,}"),
)

REDACTED = "***REDACTED***"


def redact(text: str) -> str:
    """Strip known secret values and token-shaped strings from ``text``."""
    for name in SECRET_ENV_VARS:
        value = os.environ.get(name)
        if value and len(value) >= 6 and value in text:
            text = text.replace(value, REDACTED)
    for pattern in _TOKEN_PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text


class RedactionFilter(logging.Filter):
    """Applies :func:`redact` to every formatted message and its arguments."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    key: redact(value) if isinstance(value, str) else value
                    for key, value in record.args.items()
                }
            else:
                record.args = tuple(
                    redact(arg) if isinstance(arg, str) else arg for arg in record.args
                )
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line, for shipping into a log aggregator later."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage()),
        }
        if record.exc_info:
            payload["exception"] = redact(self.formatException(record.exc_info))
        for key, value in getattr(record, "extra_fields", {}).items():
            payload[key] = value
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(level: str = "INFO", fmt: str = "text") -> None:
    """Configure the root logger. Safe to call more than once."""
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(RedactionFilter())
    if fmt == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter(
                fmt="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level.upper())

    # These are chatty at DEBUG and can echo request URLs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
