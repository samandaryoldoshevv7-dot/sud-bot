"""Paragraph/sentence aware chunking that keeps page numbers and section headings."""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.documents.extractors import ExtractedPage

_SENTENCE_END = re.compile(r"(?<=[.!?;:…])\s+(?=[\"«(]?[A-ZА-ЯЁЎҚҒҲO‘G‘0-9])")
_HEADING = re.compile(
    r"^(?:\d+(?:\.\d+)*[.)]?\s+\S|"  # 1. / 1.2 Title
    r"\d+\s*-\s*(?:modda|bob|bo'lim|bo‘lim|band|paragraf)\b|"  # 5-modda
    r"(?:modda|bob|bo'lim|bo‘lim|статья|глава|раздел|article|chapter|section)\s+\d+|"
    r"[IVXLC]+\.\s+\S)",
    re.IGNORECASE,
)


@dataclass
class ChunkData:
    text: str
    page_start: int | None
    page_end: int | None
    heading: str | None

    @property
    def char_count(self) -> int:
        return len(self.text)


@dataclass
class _Unit:
    text: str
    page: int | None
    heading: str | None


def _is_heading(line: str) -> bool:
    line = line.strip()
    if not line or len(line) > 140:
        return False
    if _HEADING.match(line):
        return True
    letters = [c for c in line if c.isalpha()]
    return len(letters) >= 4 and all(c.isupper() for c in letters) and not line.endswith(".")


def _paragraphs(text: str) -> list[str]:
    blocks = [b.strip() for b in re.split(r"\n\s*\n", text) if b.strip()]
    paragraphs: list[str] = []
    for block in blocks:
        lines = [ln.strip() for ln in block.split("\n") if ln.strip()]
        current = ""
        for line in lines:
            if _is_heading(line):
                if current:
                    paragraphs.append(current)
                paragraphs.append(line)
                current = ""
                continue
            if line.startswith(("•", "-", "–")) or re.match(r"^\d+\)\s", line):  # list item
                if current:
                    paragraphs.append(current)
                current = line
                continue
            if current and re.search(r"[.!?;:…]$", current):
                paragraphs.append(current)
                current = line
            else:
                current = f"{current} {line}".strip()
        if current:
            paragraphs.append(current)
    return paragraphs


def _split_long(text: str, max_len: int) -> list[str]:
    if len(text) <= max_len:
        return [text]
    sentences = _SENTENCE_END.split(text)
    parts: list[str] = []
    current = ""
    for sentence in sentences:
        while len(sentence) > max_len:  # pathological sentence: hard split on whitespace
            cut = sentence.rfind(" ", 0, max_len)
            cut = cut if cut > max_len // 2 else max_len
            if current:
                parts.append(current)
                current = ""
            parts.append(sentence[:cut].strip())
            sentence = sentence[cut:].strip()
        candidate = f"{current} {sentence}".strip()
        if len(candidate) > max_len and current:
            parts.append(current)
            current = sentence
        else:
            current = candidate
    if current:
        parts.append(current)
    return parts


def chunk_pages(
    pages: list[ExtractedPage], chunk_size: int = 1200, overlap: int = 200, min_chars: int = 200
) -> list[ChunkData]:
    units: list[_Unit] = []
    heading: str | None = None
    for page in pages:
        for paragraph in _paragraphs(page.text):
            if _is_heading(paragraph):
                heading = paragraph[:500]
                units.append(_Unit(paragraph, page.page_no, heading))
                continue
            for piece in _split_long(paragraph, chunk_size):
                units.append(_Unit(piece, page.page_no, heading))

    chunks: list[ChunkData] = []
    current: list[_Unit] = []

    def length(items: list[_Unit]) -> int:
        return sum(len(u.text) for u in items) + max(0, len(items) - 1) * 2

    def emit(items: list[_Unit]) -> None:
        text = "\n\n".join(u.text for u in items).strip()
        if not text:
            return
        pages_ = [u.page for u in items if u.page is not None]
        heading_ = next((u.heading for u in items if u.heading), None)
        chunks.append(
            ChunkData(
                text=text,
                page_start=min(pages_) if pages_ else None,
                page_end=max(pages_) if pages_ else None,
                heading=heading_,
            )
        )

    for unit in units:
        if current and length(current + [unit]) > chunk_size:
            emit(current)
            # Overlap: carry trailing units (whole paragraphs/sentences) up to `overlap` chars.
            carry: list[_Unit] = []
            for prev in reversed(current):
                if length([prev] + carry) > overlap:
                    break
                carry.insert(0, prev)
            current = carry if length(carry + [unit]) <= chunk_size else []
        current.append(unit)
    if current:
        emit(current)

    # Merge a too-small trailing chunk into its predecessor.
    if len(chunks) >= 2 and chunks[-1].char_count < min_chars:
        last = chunks.pop()
        prev = chunks[-1]
        prev.text = f"{prev.text}\n\n{last.text}"
        prev.page_end = last.page_end or prev.page_end
    return chunks
