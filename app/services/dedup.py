"""Server-side duplicate detection for bank questions (never left to the AI).

A new question is a duplicate of an existing one when it tests the SAME rule with the SAME answer,
however it is worded. Layers, cheapest first:

1. ``text_hash`` — the normalized question text is identical (also a unique DB index).
2. Same rule — the normalized ``fact`` (the rule the question tests, one sentence) is identical
   (``fact_hash``, a unique DB index for live questions), or nearly identical (fuzzy ≥ 90) while
   the correct answers agree.
3. Same question — the question texts are nearly identical (fuzzy ≥ 88) AND the answers agree.
4. Same meaning — the meaning vectors of "question + correct answer" are very close (cosine ≥ 0.92)
   AND the answers agree. Used only when the embedding model is available.

"The answers agree" and "nothing that changes the meaning differs" (numbers, another law/organ/
action) are required on every fuzzy layer (3–4 and the near-identical rule): two questions that share legal terms but
ask about different things (different answers) are NOT duplicates and both stay.

The check runs over ALL live questions (every material), so the same rule coming from two files is
caught too. Concurrent generation jobs serialize the final check + insert with a transaction-level
advisory lock; the unique indexes are the last line of defence.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
from dataclasses import dataclass, field

from rapidfuzz import fuzz
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Question, QuestionStatus
from app.services.question_validation import normalize_for_match

logger = logging.getLogger(__name__)

FACT_SIMILAR = 90
QUESTION_SIMILAR = 88
ANSWER_AGREE = 80
VECTOR_SIMILAR = 0.92
# One process-wide key for "check duplicates, then insert" (pg_advisory_xact_lock).
INSERT_LOCK_KEY = 0x5D_B0_7E_57

_STOPWORDS = {
    "va", "yoki", "bilan", "uchun", "bu", "shu", "ham", "qaysi", "qanday", "nima", "kim", "necha", "qachon",
    "manbaga", "ko'ra", "binoan", "muvofiq", "belgilangan", "hisoblanadi", "mumkin", "kerak", "etib",
}  # fmt: skip


def norm(value: str | None) -> str:
    """Lower-case, unified apostrophes/quotes/dashes, words only, common filler words removed."""
    words = re.findall(r"\w+(?:'\w+)?", normalize_for_match(value or ""))
    return " ".join(w for w in words if w not in _STOPWORDS)


def key_hash(value: str | None) -> str | None:
    n = norm(value)
    return hashlib.sha256(n.encode("utf-8")).hexdigest() if n else None


def answers_agree(a: str, b: str) -> bool:
    if not a or not b:
        return False
    if a == b or (len(a) > 8 and a in b) or (len(b) > 8 and b in a):
        return True
    return fuzz.token_set_ratio(a, b) >= ANSWER_AGREE


def _words(value: str) -> set[str]:
    return {w for w in value.split() if len(w) > 2 or w.isdigit()}


def different_substance(a: str, b: str) -> bool:
    """True when two similar texts still differ in something that changes the meaning: a number
    (deadline, age, article) or a content word with no counterpart in the other text (another law,
    organ, action). Word forms of the same stem ("muddati" / "muddatda") count as the same word."""
    if set(re.findall(r"\d+", a)) != set(re.findall(r"\d+", b)):
        return True
    wa, wb = _words(a), _words(b)
    for word in wa ^ wb:
        if word.isdigit():
            return True
        others = wb if word in wa else wa
        if not any(fuzz.ratio(word, o) >= 80 or (len(o) > 4 and (word.startswith(o[:5]) or o.startswith(word[:5])))
                   for o in others):  # fmt: skip
            return True
    return False


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


@dataclass
class Entry:
    question_id: int | None
    question: str
    answer: str
    fact: str
    fact_hash: str | None
    text_hash: str
    vec: list[float] | None = None


@dataclass
class Match:
    question_id: int | None
    reason: str  # same_text | same_rule | same_question | same_meaning


@dataclass
class DedupIndex:
    entries: list[Entry] = field(default_factory=list)
    max_id: int = 0
    by_text: dict[str, Entry] = field(default_factory=dict)
    by_fact: dict[str, Entry] = field(default_factory=dict)

    def add(self, entry: Entry) -> None:
        self.entries.append(entry)
        self.by_text[entry.text_hash] = entry
        if entry.fact_hash:
            self.by_fact[entry.fact_hash] = entry
        if entry.question_id:
            self.max_id = max(self.max_id, entry.question_id)

    def find(self, candidate: Entry) -> Match | None:
        hit = self.by_text.get(candidate.text_hash)
        if hit is not None:
            return Match(hit.question_id, "same_text")
        if candidate.fact_hash and candidate.fact_hash in self.by_fact:
            # Exactly the same rule (after normalization): one question per rule.
            return Match(self.by_fact[candidate.fact_hash].question_id, "same_rule")
        cand_full = f"{candidate.question} {candidate.answer}"
        for entry in self.entries:
            if not answers_agree(candidate.answer, entry.answer):
                continue  # same words, different thing being asked: not a duplicate
            if (
                candidate.fact
                and entry.fact
                and fuzz.token_sort_ratio(candidate.fact, entry.fact) >= FACT_SIMILAR
                and not different_substance(candidate.fact, entry.fact)
            ):
                return Match(entry.question_id, "same_rule")
            full = f"{entry.question} {entry.answer}"
            if fuzz.token_sort_ratio(candidate.question, entry.question) >= QUESTION_SIMILAR and not (
                different_substance(cand_full, full)
            ):
                return Match(entry.question_id, "same_question")
            if (
                candidate.vec
                and entry.vec
                and cosine(candidate.vec, entry.vec) >= VECTOR_SIMILAR
                and set(re.findall(r"\d+", cand_full)) == set(re.findall(r"\d+", full))
            ):
                return Match(entry.question_id, "same_meaning")
        return None


def entry_for(
    question: str, answer: str, fact: str | None, text_hash: str, vec: list[float] | None = None, qid: int | None = None
) -> Entry:
    return Entry(qid, norm(question), norm(answer), norm(fact), key_hash(fact), text_hash, vec)


def _entry_from_row(q: Question) -> Entry:
    answer = (q.options or {}).get(q.correct_option, "")
    return Entry(q.id, norm(q.question_text), norm(answer), norm(q.fact), q.fact_hash, q.text_hash, q.dedup_vec)


async def load_index(session: AsyncSession, after_id: int = 0, index: DedupIndex | None = None) -> DedupIndex:
    """Every live (not rejected) question of the whole bank, or only those added after ``after_id``."""
    index = index or DedupIndex()
    stmt = select(Question).where(Question.status != QuestionStatus.REJECTED, Question.id > after_id)
    for q in (await session.execute(stmt.order_by(Question.id))).unique().scalars():
        index.add(_entry_from_row(q))
    index.max_id = max(index.max_id, after_id)
    return index


async def lock_inserts(session: AsyncSession) -> None:
    """Serialize "check, then insert" across concurrent jobs until this transaction ends."""
    await session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": INSERT_LOCK_KEY})
