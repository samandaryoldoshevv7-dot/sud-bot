"""Deterministic (non-AI) quality checks applied to every generated or edited question.

These run before the AI verification steps and before anything is stored.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from rapidfuzz import fuzz

from app.schemas.ai import LETTERS
from app.utils.text import normalize_for_match

_FORBIDDEN_OPTION_PATTERNS = [
    r"\ball of the above\b",
    r"\bnone of the above\b",
    r"\bboth\b.*\band\b",
    r"barcha(si)? (javob(lar)?|variant(lar)?)? ?to'g'ri",
    r"yuqoridagi(lar)?ning (hammasi|barchasi)",
    r"hech (bir)?i ham",
    r"\bhech biri\b",
    r"\bhammasi to'g'ri\b",
    r"\bbarchasi\b",
    r"все (ответы|варианты)? ?(верны|правильны)",
    r"ни один из",
    r"все перечисленное",
    r"^[a-e]\s*(va|и|and)\s*[a-e]$",
]
_FORBIDDEN_RE = [re.compile(p, re.IGNORECASE) for p in _FORBIDDEN_OPTION_PATTERNS]


@dataclass
class ExcerptMatch:
    chunk_id: int
    score: float


@dataclass
class CheckResult:
    errors: list[str] = field(default_factory=list)
    match: ExcerptMatch | None = None

    @property
    def ok(self) -> bool:
        return not self.errors


def find_excerpt(excerpt: str, sources: Sequence[tuple[int, str]], min_score: float) -> ExcerptMatch | None:
    """Locate the AI-provided excerpt in the actual source chunks.

    Exact (normalised) substring match scores 100; otherwise rapidfuzz ``partial_ratio``
    (best matching window) is used. Returns the best chunk if it reaches ``min_score``.
    """
    needle = normalize_for_match(excerpt)
    if len(needle) < 15:
        return None
    best: ExcerptMatch | None = None
    for chunk_id, source in sources:
        hay = normalize_for_match(source)
        if needle in hay:
            return ExcerptMatch(chunk_id, 100.0)
        score = fuzz.partial_ratio(needle, hay) if len(needle) <= len(hay) else 0.0
        if best is None or score > best.score:
            best = ExcerptMatch(chunk_id, float(score))
    if best and best.score >= min_score:
        return best
    return None


def check_question(
    *,
    question: str,
    options: dict[str, str],
    correct: str,
    explanation: str,
    excerpt: str,
    expected_options: int | None,
    sources: Sequence[tuple[int, str]],
    min_excerpt_score: float,
    claimed_chunk_id: int | None = None,
) -> CheckResult:
    result = CheckResult()
    errors = result.errors
    q = question.strip()

    if len(q) < 10:
        errors.append("question_too_short")
    if len(q) > 700:
        errors.append("question_too_long")

    keys = list(options.keys())
    if expected_options is not None and len(keys) != expected_options:
        errors.append(f"option_count_{len(keys)}_expected_{expected_options}")
    if keys != list(LETTERS[: len(keys)]):
        errors.append("option_keys_invalid")
    if correct not in options:
        errors.append("correct_not_in_options")

    normalised = [normalize_for_match(v) for v in options.values()]
    if any(len(v) == 0 for v in normalised):
        errors.append("empty_option")
    if len(set(normalised)) != len(normalised):
        errors.append("duplicate_options")
    else:
        # Options that differ only in punctuation/spacing are duplicates. (Fuzzy similarity is NOT
        # used: legal distractors legitimately differ by a single number, e.g. "10 kun" / "15 kun".)
        compact = [re.sub(r"[\W_]+", "", v) for v in normalised]
        if len(set(compact)) != len(compact):
            errors.append("near_duplicate_options")
    if any(len(v) > 400 for v in options.values()):
        errors.append("option_too_long")
    for value in options.values():
        if any(p.search(normalize_for_match(value)) for p in _FORBIDDEN_RE):
            errors.append("forbidden_option_pattern")
            break
    if correct in options and normalize_for_match(options[correct]) == normalize_for_match(q):
        errors.append("answer_equals_question")

    if len(explanation.strip()) < 10:
        errors.append("explanation_missing")

    if sources:
        match = find_excerpt(excerpt, sources, min_excerpt_score)
        if match is None:
            errors.append("excerpt_not_found_in_source")
        else:
            result.match = match
            if claimed_chunk_id is not None and match.chunk_id != claimed_chunk_id:
                # The excerpt exists, just in a different context chunk: trust the real location.
                pass
    return result


def is_duplicate(question: str, existing: Sequence[str], threshold: int = 90) -> bool:
    norm = normalize_for_match(question)
    return any(fuzz.token_sort_ratio(norm, normalize_for_match(e)) >= threshold for e in existing)
