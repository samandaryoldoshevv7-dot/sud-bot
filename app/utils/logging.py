"""Structured logging with secret redaction."""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime

_RESERVED = set(vars(logging.makeLogRecord({})).keys()) | {"message", "asctime"}


class RedactingFilter(logging.Filter):
    """Replaces any configured secret value (bot token, API keys, DB password) with ***."""

    def __init__(self, secrets: list[str]):
        super().__init__()
        self._secrets = sorted({s for s in secrets if s}, key=len, reverse=True)

    def _redact(self, value: str) -> str:
        for secret in self._secrets:
            if secret in value:
                value = value.replace(secret, "***")
        return value

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        try:
            message = record.getMessage()
        except Exception:
            return True
        redacted = self._redact(message)
        if redacted != message:
            record.msg = redacted
            record.args = None
        if record.exc_info and record.exc_info[1] is not None:
            record.exc_text = self._redact(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        for key, value in list(vars(record).items()):
            if key not in _RESERVED and isinstance(value, str):
                setattr(record, key, self._redact(value))
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in vars(record).items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value if isinstance(value, (str, int, float, bool, type(None))) else str(value)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        elif record.exc_text:
            payload["exc"] = record.exc_text
        return json.dumps(payload, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extras = {k: v for k, v in vars(record).items() if k not in _RESERVED and not k.startswith("_")}
        if extras:
            base += " " + " ".join(f"{k}={v}" for k, v in extras.items())
        if record.exc_text and not record.exc_info and record.exc_text not in base:
            base += "\n" + record.exc_text
        return base


_RESERVED = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {"message", "asctime"}


class SafeLogger(logging.Logger):
    """A log call must never break the bot: ``extra`` keys clashing with LogRecord attributes
    (e.g. ``created``) are renamed instead of raising ``KeyError``."""

    def makeRecord(self, name, level, fn, lno, msg, args, exc_info, func=None, extra=None, sinfo=None):
        if extra:
            extra = {(f"{k}_" if k in _RESERVED else k): v for k, v in extra.items()}
        return super().makeRecord(name, level, fn, lno, msg, args, exc_info, func, extra, sinfo)


logging.setLoggerClass(SafeLogger)


def setup_logging(level: str = "INFO", fmt: str = "json", secrets: list[str] | None = None) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    handler.addFilter(RedactingFilter(secrets or []))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
    for noisy in ("aiogram.event", "httpx", "httpcore", "sqlalchemy.engine", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
