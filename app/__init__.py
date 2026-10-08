"""Court employee training & testing Telegram bot."""

__version__ = "1.0.0"

# Installed before any app module creates its logger: a log call can never crash the bot.
from app.utils import logging as _safe_logging  # noqa: F401
