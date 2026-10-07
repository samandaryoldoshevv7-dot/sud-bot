"""Text cleaning: unicode normalisation, de-hyphenation, header/footer and page-number removal."""

from __future__ import annotations

import re
import unicodedata
from collections import Counter

from app.documents.extractors import ExtractedPage

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_PAGE_NUMBER = re.compile(r"^\s*(?:-\s*)?(?:\d{1,4}|[ivxlc]{1,6})(?:\s*-)?\s*$", re.IGNORECASE)
_PAGE_OF = re.compile(r"^\s*(?:bet|sahifa|page|стр\.?|страница)\s*\d+(?:\s*(?:/|of|из)\s*\d+)?\s*$", re.I)
_HYPHEN_BREAK = re.compile(r"(\w)-\n(\w)")
_SPACES = re.compile(r"[ \t  -​]+")
_MANY_NEWLINES = re.compile(r"\n{3,}")


def _normalise(text: str) -> str:
    text = unicodedata.normalize("NFC", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("­", "")
    text = _CONTROL.sub(" ", text)
    text = _HYPHEN_BREAK.sub(r"\1\2", text)
    text = "\n".join(_SPACES.sub(" ", line).strip() for line in text.split("\n"))
    return _MANY_NEWLINES.sub("\n\n", text).strip()


def _repeated_lines(pages: list[str]) -> set[str]:
    """Lines that appear on more than half of the pages (running headers/footers)."""
    if len(pages) < 4:
        return set()
    counter: Counter[str] = Counter()
    for page in pages:
        lines = [ln.strip() for ln in page.split("\n") if ln.strip()]
        edge = set(lines[:3] + lines[-3:])
        counter.update(edge)
    threshold = len(pages) / 2
    return {line for line, count in counter.items() if count > threshold and len(line) < 150}


def clean_pages(pages: list[ExtractedPage]) -> list[ExtractedPage]:
    normalised = [_normalise(p.text) for p in pages]
    repeated = _repeated_lines(normalised)
    cleaned: list[ExtractedPage] = []
    for page, text in zip(pages, normalised, strict=True):
        lines = []
        for line in text.split("\n"):
            stripped = line.strip()
            if stripped in repeated or _PAGE_NUMBER.match(stripped) or _PAGE_OF.match(stripped):
                continue
            lines.append(line)
        result = _MANY_NEWLINES.sub("\n\n", "\n".join(lines)).strip()
        cleaned.append(ExtractedPage(page_no=page.page_no, text=result))
    return cleaned
