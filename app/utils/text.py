"""Text helpers: HTML escaping for Telegram, truncation, normalisation, hashing."""

from __future__ import annotations

import hashlib
import html
import re
import unicodedata

TELEGRAM_MESSAGE_LIMIT = 4096
TELEGRAM_CAPTION_LIMIT = 1024

_APOSTROPHES = str.maketrans({"‘": "'", "’": "'", "ʻ": "'", "ʼ": "'", "`": "'", "´": "'", "ʹ": "'"})
_QUOTES = str.maketrans({"“": '"', "”": '"', "„": '"', "«": '"', "»": '"'})
_DASHES = str.maketrans({"–": "-", "—": "-", "‑": "-", "−": "-"})
_WS = re.compile(r"\s+")


def esc(value: object) -> str:
    """Escape arbitrary (user/AI provided) content for Telegram HTML parse mode."""
    return html.escape("" if value is None else str(value), quote=False)


def truncate(value: str, limit: int, suffix: str = "…") -> str:
    if len(value) <= limit:
        return value
    return value[: max(0, limit - len(suffix))].rstrip() + suffix


def normalize_for_match(value: str) -> str:
    """Aggressive normalisation used to compare AI excerpts with source text."""
    value = unicodedata.normalize("NFKC", value or "")
    value = value.translate(_APOSTROPHES).translate(_QUOTES).translate(_DASHES)
    value = value.replace("­", "")  # soft hyphen
    value = _WS.sub(" ", value)
    return value.strip().casefold()


def normalize_topic(value: str) -> str:
    value = normalize_for_match(value)
    value = re.sub(r"[^\w\s'-]", "", value)
    return _WS.sub(" ", value).strip()[:255]


def text_hash(value: str) -> str:
    norm = re.sub(r"[^\w]", "", normalize_for_match(value))
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def split_message(value: str, limit: int = TELEGRAM_MESSAGE_LIMIT) -> list[str]:
    """Split long text into Telegram-sized parts on line boundaries."""
    if len(value) <= limit:
        return [value]
    parts: list[str] = []
    current = ""
    for line in value.split("\n"):
        while len(line) > limit:
            if current:
                parts.append(current)
                current = ""
            parts.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            parts.append(current)
            current = line
        else:
            current = candidate
    if current:
        parts.append(current)
    return parts


def progress_bar(percent: float, width: int = 10) -> str:
    percent = max(0.0, min(100.0, float(percent)))
    filled = round(percent / 100 * width)
    return "▰" * filled + "▱" * (width - filled)


def pct(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{float(value):.1f}%".replace(".0%", "%")
