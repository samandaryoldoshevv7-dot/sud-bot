"""Questions as native Telegram quiz polls (the "Quiz Bot" look).

Telegram limits: question 300 characters, each option 100, explanation 200 (at most 2 line
breaks). A question that does not fit is still a poll: its full text and options are sent in a
message right above it. The explanation may be shortened; the full one is in 📋 Xatolarni ko'rish.
"""

from __future__ import annotations

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


def _cut_words(text: str, limit: int) -> str:
    """Shorten to ``limit`` Telegram characters at a word boundary, ending with "…"."""
    cut = _cut(text, limit)
    head = cut[:-1].rstrip()
    if " " in head and len(head) > limit // 2:
        head = head.rsplit(" ", 1)[0].rstrip(" ,;:—-")
    return head + "…"


def _explanation(tq: TestQuestion) -> tuple[str | None, bool]:
    """Text under the 💡 of the poll (Telegram allows 200 characters). The explanation comes first:
    when it and the source do not fit together, the source line is left out (it is in the results);
    an explanation longer than 200 is cut at a word and sent in full after the answer."""
    source = t("quiz.poll_explanation_source", s=" ".join(tq.source_name.split())) if tq.source_name else None
    expl = t("quiz.poll_explanation", text=" ".join(tq.explanation.split())) if tq.explanation else None
    parts = [p for p in (source, expl) if p]
    if not parts:
        return None, False
    text = "\n".join(parts)
    if tg_len(text) <= EXPLANATION_MAX:
        return text, False
    if expl is None:
        return _cut(text, EXPLANATION_MAX), False
    if tg_len(expl) <= EXPLANATION_MAX:
        return expl, False
    return _cut_words(expl, EXPLANATION_MAX), True


def full_explanation(tq: TestQuestion) -> str:
    """The whole explanation as a message (when the poll could show only part of it)."""
    lines = [t("quiz.full_explanation", text=esc(" ".join((tq.explanation or "").split())))]
    if tq.source_name:
        lines.append(t("quiz.full_explanation_source", s=esc(" ".join(tq.source_name.split()))))
    return "\n".join(lines)


def build(attempt: TestAttempt, tq: TestQuestion, item: dict, position: int, reveal: AnswerReveal) -> PollSpec | None:
    """The poll for this employee's question, or None when it does not fit Telegram's limits."""
    return build_for(tq, list(item["opts"]), position, attempt.total_questions, reveal)


def build_for(tq: TestQuestion, opts: list[str], position: int, total: int, reveal: AnswerReveal) -> PollSpec | None:
    """``opts`` = original option letters in the order they are shown (A, B, C...).

    Every question becomes a poll. When the question (300) or an option (100) is longer than
    Telegram allows, the full question and options are sent as a message right above the poll
    (``preface``) and the poll carries "[n/total] ⬆️ …" and the shortened options. Options are always
    lettered "A) …", "B) …"."""
    if not all(o in tq.options for o in opts) or not 2 <= len(opts) <= MAX_OPTIONS:
        return None
    head = f"[{position + 1}/{total}]"
    text = " ".join(tq.question_text.split())
    full_options = [" ".join(tq.options[o].split()) for o in opts]
    question = f"{head} {text}"
    letters = [chr(ord("A") + i) for i in range(len(opts))]
    # Options always carry their letter (A, B, C, D), so a question reads the same everywhere.
    options = [f"{L}) {o}" for L, o in zip(letters, full_options, strict=True)]
    preface = None
    if tg_len(question) > QUESTION_MAX or any(not o or tg_len(o) > OPTION_MAX for o in options):
        preface = "\n".join(
            [f"<b>{head}</b> <b>{esc(text)}</b>", ""]
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
