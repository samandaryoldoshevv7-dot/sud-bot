"""Questions as native Telegram quiz polls (the "Quiz Bot" look).

Telegram limits: question 300 characters, each option 100, explanation 200 (at most 2 line
breaks). A question that does not fit is still a poll: its full text and options are sent in a
message right above it. The explanation may be shortened; the full one is in 📋 Xatolarni ko'rish.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.locales import t
from app.models import AnswerReveal, QuizPoll, TestAttempt, TestQuestion
from app.utils.text import esc
from app.utils.time import utcnow

QUESTION_MAX = 300
OPTION_MAX = 100
EXPLANATION_MAX = 200
MAX_OPTIONS = 10


def tg_len(text: str) -> int:
    """Length as Telegram counts it (UTF-16 code units): an emoji such as 📚 counts as 2."""
    return len(text.encode("utf-16-le")) // 2


def _cut(text: str, limit: int) -> str:
    """Shorten to ``limit`` Telegram characters, ending with "…"."""
    while text and tg_len(text) > limit - 1:
        text = text[:-1]
    return text.rstrip() + "…"


@dataclass
class PollSpec:
    question: str
    options: list[str]
    quiz: bool  # True: Telegram shows right/wrong at once; False: a plain poll (answers revealed later)
    correct_option_id: int | None
    explanation: str | None
    explanation_cut: bool
    # A question or option too long for a poll: the full text goes in a message right above it.
    preface: str | None = None


def _short(text: str, limit: int) -> str:
    """A long explanation made short enough to be read to the end: whole sentences that fit, or
    (one very long sentence) cut at a word."""
    sentences = re.split(r"(?<=[.!?])\s+", text)
    kept = ""
    for sentence in sentences:
        candidate = f"{kept} {sentence}".strip()
        if tg_len(candidate) > limit:
            break
        kept = candidate
    if kept:
        return kept
    cut = _cut(text, limit)[:-1].rstrip()
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0].rstrip(" ,;:—-")
    return cut + "…"


def _explanation(tq: TestQuestion) -> tuple[str | None, bool]:
    """Text under the 💡 of the poll (Telegram shows at most 200 characters): the explanation, then
    the source. The explanation is always readable to the end: when the source line does not fit too
    it is left out (it is in the results), and a long explanation is shortened to its first whole
    sentences."""
    source = t("quiz.poll_explanation_source", s=" ".join(tq.source_name.split())) if tq.source_name else None
    if not tq.explanation:
        return (_cut(source, EXPLANATION_MAX) if source and tg_len(source) > EXPLANATION_MAX else source), False
    full = t("quiz.poll_explanation", text=" ".join(tq.explanation.split()))
    if source and tg_len(f"{full}\n{source}") <= EXPLANATION_MAX:
        return f"{full}\n{source}", False
    if tg_len(full) <= EXPLANATION_MAX:
        return full, False
    prefix = t("quiz.poll_explanation", text="")
    short = prefix + _short(" ".join(tq.explanation.split()), EXPLANATION_MAX - tg_len(prefix))
    return short, True


SOURCE_HEAD_MAX = 90  # the material title in the poll question is shortened beyond this


def source_line(tq: TestQuestion) -> str | None:
    """"📚 Manba: <material title the admin gave>" — shown at the start of every question."""
    name = " ".join((tq.source_name or "").split())
    if not name:
        return None
    line = t("quiz.source_head", s=name)
    return _cut(line, SOURCE_HEAD_MAX) if tg_len(line) > SOURCE_HEAD_MAX else line


def question_head(position: int, total: int) -> str:
    return t("quiz.question_head", n=position + 1, total=total)


def build(attempt: TestAttempt, tq: TestQuestion, item: dict, position: int, reveal: AnswerReveal) -> PollSpec | None:
    """The poll for this employee's question, or None when it does not fit Telegram's limits."""
    return build_for(tq, list(item["opts"]), position, attempt.total_questions, reveal)


def build_for(tq: TestQuestion, opts: list[str], position: int, total: int, reveal: AnswerReveal) -> PollSpec | None:
    """``opts`` = original option letters in the order they are shown (A, B, C...).

    Every question becomes a poll. When the question (300) or an option (100) is longer than
    Telegram allows, the full question and options are sent as a message right above the poll
    (``preface``) and the poll carries the heading with "⬆️ …" and the shortened options. Options are
    always lettered "A) …", "B) …". Every question starts with "📚 Manba: …" and "❓ Savol n/total"."""
    if not all(o in tq.options for o in opts) or not 2 <= len(opts) <= MAX_OPTIONS:
        return None
    source = source_line(tq)
    head = "\n".join(x for x in (source, question_head(position, total)) if x)
    text = " ".join(tq.question_text.split())
    full_options = [" ".join(tq.options[o].split()) for o in opts]
    question = f"{head}\n{text}"
    letters = [chr(ord("A") + i) for i in range(len(opts))]
    # Options always carry their letter (A, B, C, D), so a question reads the same everywhere.
    options = [f"{L}) {o}" for L, o in zip(letters, full_options, strict=True)]
    preface = None
    if tg_len(question) > QUESTION_MAX or any(not o or tg_len(o) > OPTION_MAX for o in options):
        preface = "\n".join(
            [f"<b>{esc(head)}</b>", "", f"<b>{esc(text)}</b>", ""]
            + [f"<b>{L})</b> {esc(o)}" for L, o in zip(letters, full_options, strict=True)]
        )
        question = t("quiz.poll_see_above", head=head)
        options = [o if tg_len(o) <= OPTION_MAX else _cut(o, OPTION_MAX) for o in options]
    if reveal != AnswerReveal.IMMEDIATE:
        return PollSpec(question, options, False, None, None, False, preface)
    if tq.correct_option not in opts:
        return None
    explanation, cut = _explanation(tq)
    return PollSpec(question, options, True, opts.index(tq.correct_option), explanation, cut, preface)


def send_kwargs(spec: PollSpec) -> dict:
    """Arguments for ``bot.send_poll`` shared by the private chat and the group."""
    return dict(
        question=spec.question,
        options=spec.options,
        type="quiz" if spec.quiz else "regular",
        correct_option_ids=[spec.correct_option_id] if spec.correct_option_id is not None else None,
        explanation=spec.explanation,
        explanation_parse_mode=None,
        question_parse_mode=None,
        is_anonymous=False,  # answers must reach the bot to be stored per employee
        allows_multiple_answers=False,
        allows_revoting=False,  # the first answer is final, as with the buttons
        shuffle_options=False,  # the order is fixed by the bot (and stored)
    )


async def remember(
    session: AsyncSession, poll_id: str, attempt_id: int, position: int, chat_id: int, message_id: int, cut: bool
) -> None:
    session.add(QuizPoll(poll_id=poll_id, attempt_id=attempt_id, position=position, chat_id=chat_id,
                         message_id=message_id, explanation_cut=cut, created_at=utcnow()))  # fmt: skip
    await session.commit()


async def find(session: AsyncSession, poll_id: str) -> QuizPoll | None:
    return await session.get(QuizPoll, poll_id)


async def take_open(session: AsyncSession, attempt_id: int, position: int) -> list[QuizPoll]:
    """Polls already sent for this (still unanswered) question; they are replaced by a new one."""
    rows = list(
        (
            await session.execute(
                select(QuizPoll).where(QuizPoll.attempt_id == attempt_id, QuizPoll.position == position)
            )
        ).scalars()
    )
    if rows:
        await session.execute(delete(QuizPoll).where(QuizPoll.poll_id.in_([r.poll_id for r in rows])))
        await session.commit()
    return rows


async def by_message(session: AsyncSession, chat_id: int, message_id: int) -> QuizPoll | None:
    return (
        await session.execute(select(QuizPoll).where(QuizPoll.chat_id == chat_id, QuizPoll.message_id == message_id))
    ).scalar_one_or_none()
