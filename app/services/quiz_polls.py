"""Questions as native Telegram quiz polls (the "Quiz Bot" look).

Telegram limits: question 300 characters, each option 100, explanation 200 (at most 2 line
breaks). A question that does not fit is shown the classic way (text + buttons) instead, so no
question is ever shortened in a way that could change its meaning. Only the explanation may be
shortened: the full text is then sent right after the answer.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.locales import t
from app.models import AnswerReveal, QuizPoll, TestAttempt, TestQuestion
from app.schemas.ai import LETTERS
from app.services.attempts import displayed_options, original_to_display
from app.utils.time import utcnow

QUESTION_MAX = 300
OPTION_MAX = 100
EXPLANATION_MAX = 200
MAX_OPTIONS = 10


@dataclass
class PollSpec:
    question: str
    options: list[str]
    quiz: bool  # True: Telegram shows right/wrong at once; False: a plain poll (answers revealed later)
    correct_option_id: int | None
    explanation: str | None
    explanation_cut: bool


def _explanation(tq: TestQuestion) -> tuple[str | None, bool]:
    parts = []
    if tq.source_name:
        parts.append(t("quiz.poll_explanation_source", s=" ".join(tq.source_name.split())))
    if tq.explanation:
        parts.append(t("quiz.poll_explanation", text=" ".join(tq.explanation.split())))
    if not parts:
        return None, False
    text = "\n".join(parts)
    if len(text) <= EXPLANATION_MAX:
        return text, False
    return text[: EXPLANATION_MAX - 1].rstrip() + "…", True


def build(attempt: TestAttempt, tq: TestQuestion, item: dict, position: int, reveal: AnswerReveal) -> PollSpec | None:
    """The poll for this question, or None when it does not fit Telegram's limits."""
    question = f"[{position + 1}/{attempt.total_questions}] {' '.join(tq.question_text.split())}"
    options = [" ".join(text.split()) for _, text in displayed_options(tq, item)]
    if len(question) > QUESTION_MAX or not 2 <= len(options) <= MAX_OPTIONS:
        return None
    if any(not o or len(o) > OPTION_MAX for o in options):
        return None
    if reveal != AnswerReveal.IMMEDIATE:
        return PollSpec(question, options, quiz=False, correct_option_id=None, explanation=None, explanation_cut=False)
    correct = original_to_display(item, tq.correct_option)
    if correct is None:
        return None
    explanation, cut = _explanation(tq)
    return PollSpec(question, options, True, LETTERS.index(correct), explanation, cut)


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
