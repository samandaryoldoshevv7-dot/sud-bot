"""Tests that run INSIDE a Telegram group.

Design (chosen for reliability and exact per-user accounting):
* The bot posts a header message with ▶️ TESTNI BOSHLASH and then one shared message per question.
  Each question message shows the full option texts and only ``A`` ``B`` ``C`` ``D`` buttons.
* Native Telegram polls are NOT used: they cannot enforce "first answer is final", hide the correct
  answer per user, or be tied to our attempts. Custom inline keyboards are handled server-side.
* The answering user is ALWAYS ``callback.from_user`` (callback data only carries the message id and
  the letter), so nobody can answer on behalf of someone else.
* One answer per (user, test, question) is enforced by a unique constraint plus a row lock on the
  attempt; answers are immutable (DB trigger). Feedback is shown in a private callback alert, so other
  group members never see someone else's answer.
* Every employee has their own attempt row; nothing is kept in process memory, so hundreds of people
  can answer at the same time.
* Question message counters ("👥 Javob berdi: N") are refreshed by a background loop with throttling
  (Telegram limits edits per group). When the test ends, each message is turned into a poll-like
  final result (distribution per option, correct answer marked if the admin allows it).
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardMarkup
from aiogram.types import User as TgUser
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.keyboards.callbacks import GroupAnsCB, GroupStartCB
from app.keyboards.common import kb
from app.locales import t
from app.models import (
    AnswerReveal,
    AttemptStatus,
    DeliveryMode,
    Group,
    GroupQuestionMessage,
    GroupTestPost,
    QuestionOption,
    Test,
    TestAttempt,
    TestQuestion,
    TestStatus,
    User,
    UserAnswer,
    UserStatus,
)
from app.schemas.ai import LETTERS
from app.services import groups as group_service
from app.services import settings_service
from app.services.attempts import (
    answered_question_ids,
    finalize_attempt,
    get_in_progress,
    next_unanswered_index,
    start_attempt,
)
from app.services.users import upsert_user
from app.utils.text import esc, progress_bar, truncate
from app.utils.time import fmt_dt, fmt_hours, utcnow

logger = logging.getLogger(__name__)

SEPARATOR = "━━━━━━━━━━━━━━━━"
ALERT_LIMIT = 200
REFRESH_MIN_INTERVAL = timedelta(seconds=20)
_sending: set[int] = set()  # post ids currently being sent by THIS process (prevents double sending)


class GroupAnswerCode(str, Enum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    NOT_STARTED = "not_started"
    EXPIRED = "expired"
    BLOCKED = "blocked"
    PENDING = "pending"
    INVALID = "invalid"


@dataclass
class GroupAnswerResult:
    code: GroupAnswerCode
    selected: str | None = None
    correct: str | None = None
    is_correct: bool | None = None
    reveal: AnswerReveal | None = None
    finished: bool = False
    attempt: TestAttempt | None = None
    explanation: str = ""


# ------------------------------------------------------------------------------ rendering


def answer_keyboard(gqm: GroupQuestionMessage) -> InlineKeyboardMarkup:
    return kb([(LETTERS[i], GroupAnsCB(m=gqm.id, o=LETTERS[i])) for i in range(len(gqm.opts))])


def render_question(tq: TestQuestion, gqm: GroupQuestionMessage, total: int, answered: int) -> str:
    lines = [SEPARATOR, f"📝 <b>{gqm.position + 1} / {total}</b>", "", f"<b>{esc(tq.question_text)}</b>", ""]
    for i, original in enumerate(gqm.opts):
        lines.append(f"<b>{LETTERS[i]})</b> {esc(tq.options[original])}")
    lines += ["", t("gt.choose"), t("gt.answered_count", n=answered), SEPARATOR]
    return "\n".join(lines)


def render_question_final(
    tq: TestQuestion, gqm: GroupQuestionMessage, total: int, distribution: dict[str, int], reveal: bool
) -> str:
    answered = sum(distribution.values())
    lines = [SEPARATOR, f"📝 <b>{gqm.position + 1} / {total}</b> — {t('gt.final_results')}", "",
             f"<b>{esc(tq.question_text)}</b>", ""]  # fmt: skip
    for i, original in enumerate(gqm.opts):
        n = distribution.get(original, 0)
        share = n / answered * 100 if answered else 0
        mark = " ✅" if reveal and original == tq.correct_option else ""
        lines.append(f"<b>{LETTERS[i]})</b> {esc(tq.options[original])}{mark}")
        lines.append(f"    {progress_bar(share, 8)} {share:.0f}% ({n})")
    lines += ["", t("gt.total_answered", n=answered), SEPARATOR]
    return "\n".join(lines)


def render_header(test: Test, started: int | None = None, finished: bool = False) -> str:
    hours = max(1, round((test.deadline_at - test.starts_at).total_seconds() / 3600))
    key = "gt.header_finished" if finished else "gt.header"
    text = t(
        key,
        title=esc(test.title),
        n=test.question_count,
        duration=fmt_hours(hours),
        start=fmt_dt(test.starts_at),
        end=fmt_dt(test.deadline_at),
        passing=test.passing_percent,
    )
    if test.description and not finished:
        text += "\n\n" + esc(truncate(test.description, 500))
    if started is not None:
        text += "\n\n" + t("gt.participants", n=started)
    return text


def header_keyboard(post: GroupTestPost) -> InlineKeyboardMarkup:
    return kb([(t("btn.start_test_caps"), GroupStartCB(p=post.id))])


# ------------------------------------------------------------------------------ Telegram helpers


async def _call(coro_factory, *, what: str):
    """Run a Telegram call, waiting on flood control (bots may post ~20 messages/minute per group)."""
    for _ in range(5):
        try:
            return await coro_factory()
        except TelegramRetryAfter as exc:
            await asyncio.sleep(exc.retry_after + 1)
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc):
                return None
            logger.warning("Telegram rejected group call", extra={"what": what, "error": str(exc)[:200]})
            return None
    logger.warning("Telegram flood control: giving up", extra={"what": what})
    return None


# ------------------------------------------------------------------------------ publishing


async def ensure_post(
    session: AsyncSession, test: Test, group: Group, rng: random.Random | None = None
) -> GroupTestPost:
    """Create (once) the post and its question-message rows with the option order for the group."""
    existing = (
        await session.execute(
            select(GroupTestPost).where(GroupTestPost.test_id == test.id, GroupTestPost.group_id == group.id)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    rng = rng or random.SystemRandom()
    questions = list(
        (
            await session.execute(
                select(TestQuestion).where(TestQuestion.test_id == test.id).order_by(TestQuestion.position)
            )
        )
        .scalars()
        .all()
    )
    if test.randomize_questions:
        rng.shuffle(questions)
    post = GroupTestPost(test_id=test.id, group_id=group.id, chat_id=group.chat_id)
    session.add(post)
    await session.flush()
    for position, tq in enumerate(questions):
        opts = list(tq.options.keys())
        if test.randomize_options:
            rng.shuffle(opts)  # one shuffle per group post: everybody sees the same shared message
        session.add(GroupQuestionMessage(post_id=post.id, test_question_id=tq.id, position=position, opts=opts))
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        return (
            await session.execute(
                select(GroupTestPost).where(GroupTestPost.test_id == test.id, GroupTestPost.group_id == group.id)
            )
        ).scalar_one()
    return post


async def send_post(bot: Bot, session_maker: async_sessionmaker[AsyncSession], post_id: int) -> bool:
    """Send the header and every question message not sent yet (idempotent, resumable after restart)."""
    if post_id in _sending:
        return True  # already being posted by this process
    _sending.add(post_id)
    try:
        async with session_maker() as session:
            post = await session.get(GroupTestPost, post_id)
            if post is None:
                return False
            test = await session.get(Test, post.test_id)
            assert test is not None
            if post.header_message_id is None:
                try:
                    msg = await _call(
                        lambda: bot.send_message(post.chat_id, render_header(test), reply_markup=header_keyboard(post)),
                        what="header",
                    )
                except TelegramForbiddenError:
                    logger.error("Bot cannot write to the group", extra={"chat_id": post.chat_id})
                    return False
                if msg is None:
                    return False
                post.header_message_id = msg.message_id
                await session.commit()
            messages = list(
                (
                    await session.execute(
                        select(GroupQuestionMessage)
                        .where(GroupQuestionMessage.post_id == post.id)
                        .order_by(GroupQuestionMessage.position)
                    )
                )
                .scalars()
                .all()
            )
            total = len(messages)
            for gqm in messages:
                if gqm.message_id is not None:
                    continue
                tq = await session.get(TestQuestion, gqm.test_question_id)
                assert tq is not None
                try:
                    msg = await _call(
                        lambda tq=tq, gqm=gqm: bot.send_message(
                            post.chat_id, render_question(tq, gqm, total, 0), reply_markup=answer_keyboard(gqm)
                        ),
                        what="question",
                    )
                except TelegramForbiddenError:
                    logger.error("Bot cannot write to the group", extra={"chat_id": post.chat_id})
                    return False
                if msg is None:
                    return False
                gqm.message_id = msg.message_id
                gqm.rendered_at = utcnow()
                await session.commit()
            post.posted_all = True
            await session.commit()
            logger.info("Group test posted", extra={"post_id": post.id, "test_id": test.id, "questions": total})
            return True
    finally:
        _sending.discard(post_id)


async def start_in_group(
    bot: Bot, session_maker: async_sessionmaker[AsyncSession], test_id: int, group_id: int | None = None
) -> tuple[int | None, bool]:
    """Post a test into a group (its own group by default). Returns ``(post_id, fully_posted)``."""
    async with session_maker() as session:
        test = await session.get(Test, test_id)
        if test is None:
            return None, False
        group = await session.get(Group, group_id or test.group_id) if (group_id or test.group_id) else None
        if group is None or not group.is_active:
            logger.error("Group for test is missing or inactive", extra={"test_id": test_id})
            return None, False
        post = await ensure_post(session, test, group)
        post_id = post.id
    ok = await send_post(bot, session_maker, post_id)
    return post_id, ok


async def post_to_group_and_report(
    bot: Bot, session_maker: async_sessionmaker[AsyncSession], test_id: int, group_id: int, admin_chat_id: int
) -> bool:
    """Post a test into a group and tell the admin whether it worked (and why not)."""
    _, ok = await start_in_group(bot, session_maker, test_id, group_id)
    async with session_maker() as session:
        group = await session.get(Group, group_id)
        test = await session.get(Test, test_id)
        title = esc(test.title) if test else "?"
        group_title = esc(group.title) if group else "?"
    key = "gt.admin_posted_ok" if ok else "gt.admin_post_failed"
    try:
        await bot.send_message(admin_chat_id, t(key, title=title, group=group_title))
    except Exception as exc:
        logger.warning("Could not report group posting to admin", extra={"error": str(exc)[:200]})
    return ok


async def posted_group_ids(session: AsyncSession, test_id: int) -> set[int]:
    rows = await session.execute(select(GroupTestPost.group_id).where(GroupTestPost.test_id == test_id))
    return set(rows.scalars().all())


# ------------------------------------------------------------------------------ answering


async def _authorise(
    session: AsyncSession, tg_user: TgUser, group: Group
) -> tuple[User | None, GroupAnswerCode | None]:
    user = await upsert_user(session, tg_user)
    await group_service.mark_membership(session, group, user, True)
    if user.status == UserStatus.INACTIVE:
        return user, GroupAnswerCode.BLOCKED
    if user.status == UserStatus.PENDING:
        if await settings_service.get_value(session, "auto_approve_group_members"):
            user.status = UserStatus.ACTIVE
            await session.commit()
        else:
            return user, GroupAnswerCode.PENDING
    return user, None


def _time_state(test: Test, now: datetime) -> GroupAnswerCode | None:
    if now < test.starts_at or test.status in (TestStatus.DRAFT, TestStatus.READY):
        return GroupAnswerCode.NOT_STARTED
    if now >= test.deadline_at or test.status in (TestStatus.EXPIRED, TestStatus.CLOSED):
        return GroupAnswerCode.EXPIRED
    return None


async def _layout(session: AsyncSession, post_id: int) -> list[dict]:
    rows = (
        await session.execute(
            select(GroupQuestionMessage.test_question_id, GroupQuestionMessage.opts)
            .where(GroupQuestionMessage.post_id == post_id)
            .order_by(GroupQuestionMessage.position)
        )
    ).all()
    return [{"tq": tq_id, "opts": list(opts)} for tq_id, opts in rows]


async def _get_or_start_attempt(
    session: AsyncSession, user: User, test: Test, post: GroupTestPost, now: datetime
) -> TestAttempt | None:
    attempt = await get_in_progress(session, test.id, user.id)
    if attempt is not None:
        return attempt
    result = await start_attempt(session, user, test.id, chat_id=post.chat_id, now=now,
                                 layout=await _layout(session, post.id))  # fmt: skip
    return result.attempt


async def group_start(
    session: AsyncSession, tg_user: TgUser, post_id: int, now: datetime | None = None
) -> tuple[GroupAnswerCode, TestAttempt | None]:
    """▶️ TESTNI BOSHLASH pressed in the group: create the employee's attempt (idempotent)."""
    now = now or utcnow()
    post = await session.get(GroupTestPost, post_id)
    if post is None:
        return GroupAnswerCode.INVALID, None
    test = await session.get(Test, post.test_id)
    group = await session.get(Group, post.group_id)
    if test is None or group is None:
        return GroupAnswerCode.INVALID, None
    state = _time_state(test, now)
    if state is not None:
        return state, None
    user, problem = await _authorise(session, tg_user, group)
    if problem is not None or user is None:
        return problem or GroupAnswerCode.INVALID, None
    attempt = await _get_or_start_attempt(session, user, test, post, now)
    if attempt is None:
        # Already finished earlier.
        finished = (
            await session.execute(
                select(TestAttempt).where(TestAttempt.test_id == test.id, TestAttempt.user_id == user.id)
                .order_by(TestAttempt.attempt_no.desc()).limit(1)
            )
        ).scalar_one_or_none()  # fmt: skip
        return GroupAnswerCode.DUPLICATE, finished
    return GroupAnswerCode.ACCEPTED, attempt


async def submit_group_answer(
    session: AsyncSession, tg_user: TgUser, gqm_id: int, display_letter: str, now: datetime | None = None
) -> GroupAnswerResult:
    now = now or utcnow()
    gqm = await session.get(GroupQuestionMessage, gqm_id)
    if gqm is None or display_letter not in LETTERS[: len(gqm.opts)]:
        return GroupAnswerResult(GroupAnswerCode.INVALID)
    post = await session.get(GroupTestPost, gqm.post_id)
    assert post is not None
    test = await session.get(Test, post.test_id)
    group = await session.get(Group, post.group_id)
    if test is None or group is None:
        return GroupAnswerResult(GroupAnswerCode.INVALID)
    state = _time_state(test, now)
    if state is not None:
        return GroupAnswerResult(state)
    user, problem = await _authorise(session, tg_user, group)
    if problem is not None or user is None:
        return GroupAnswerResult(problem or GroupAnswerCode.INVALID)
    # Plain values only from here on: a rollback inside start_attempt (concurrent double click)
    # expires every ORM object in the session.
    user_id, telegram_id = user.id, user.telegram_id
    test_id, tq_id, position, opts = test.id, gqm.test_question_id, gqm.position, list(gqm.opts)
    reveal = test.answer_reveal

    async def duplicate() -> GroupAnswerResult:
        previous = (
            await session.execute(
                select(UserAnswer.selected_display).where(
                    UserAnswer.user_id == user_id, UserAnswer.test_id == test_id, UserAnswer.test_question_id == tq_id
                )
            )
        ).scalar_one_or_none()
        return GroupAnswerResult(GroupAnswerCode.DUPLICATE, selected=previous)

    attempt = await _get_or_start_attempt(session, user, test, post, now)
    if attempt is None:
        return await duplicate()
    attempt_id = attempt.id
    # Serialise this user's clicks (other users lock their own attempt rows — no contention).
    attempt = (
        await session.execute(select(TestAttempt).where(TestAttempt.id == attempt_id).with_for_update(of=TestAttempt))
    ).scalar_one()
    if attempt.status != AttemptStatus.IN_PROGRESS:
        await session.commit()
        return await duplicate()
    exists = (
        await session.execute(
            select(UserAnswer.id).where(
                UserAnswer.user_id == user_id, UserAnswer.test_id == test_id, UserAnswer.test_question_id == tq_id
            )
        )
    ).scalar_one_or_none()
    if exists is not None:
        await session.commit()
        return await duplicate()

    tq = await session.get(TestQuestion, tq_id)
    assert tq is not None
    original = opts[LETTERS.index(display_letter)]
    correct_display = LETTERS[opts.index(tq.correct_option)]
    option_id = (
        await session.execute(
            select(QuestionOption.id).where(QuestionOption.test_question_id == tq_id, QuestionOption.letter == original)
        )
    ).scalar_one_or_none()
    is_correct = original == tq.correct_option
    explanation = tq.explanation
    session.add(
        UserAnswer(
            attempt_id=attempt_id,
            test_id=test_id,
            test_question_id=tq_id,
            user_id=user_id,
            telegram_id=telegram_id,
            position=position,
            selected_option=original,
            selected_option_id=option_id,
            selected_display=display_letter,
            correct_option=tq.correct_option,
            correct_display=correct_display,
            is_correct=is_correct,
            answered_at=now,
        )
    )
    attempt.answered_count += 1
    attempt.correct_count += int(is_correct)
    attempt.incorrect_count += int(not is_correct)
    answered = await answered_question_ids(session, attempt_id)
    answered.add(tq_id)
    attempt.current_index = next_unanswered_index(attempt.layout, answered)
    finished = attempt.answered_count >= attempt.total_questions
    if finished:
        await finalize_attempt(session, attempt, AttemptStatus.COMPLETED, now)
    try:
        await session.commit()
    except IntegrityError:  # a concurrent duplicate click won the race
        await session.rollback()
        return await duplicate()
    return GroupAnswerResult(
        GroupAnswerCode.ACCEPTED,
        selected=display_letter,
        correct=correct_display,
        is_correct=is_correct,
        reveal=reveal,
        finished=finished,
        attempt=attempt,
        explanation=explanation,
    )


def answer_alert(result: GroupAnswerResult) -> str:
    """Private popup shown only to the employee who clicked (max 200 characters)."""
    code = result.code
    if code == GroupAnswerCode.DUPLICATE:
        text = t("gt.alert.duplicate")
        if result.selected:
            text += "\n" + t("gt.alert.your_choice", letter=result.selected)
        return text
    if code != GroupAnswerCode.ACCEPTED:
        return t(f"gt.alert.{code.value}")
    if result.reveal == AnswerReveal.IMMEDIATE:
        key = "gt.alert.correct" if result.is_correct else "gt.alert.wrong"
        text = t(key, selected=result.selected, correct=result.correct)
        if result.explanation:
            room = ALERT_LIMIT - len(text) - 3
            if room > 20:
                text += "\n💡 " + truncate(result.explanation, room)
    else:
        text = t("gt.alert.accepted", selected=result.selected)
    if result.finished and result.attempt is not None:
        a = result.attempt
        if result.reveal == AnswerReveal.NEVER:
            summary = t("gt.alert.finished_hidden")
        else:
            score = f"{float(a.score_percent):g}%"
            summary = t("gt.alert.finished", c=a.correct_count, total=a.total_questions, score=score)
        text = summary if len(text) + len(summary) + 1 > ALERT_LIMIT else text + "\n" + summary
    return truncate(text, ALERT_LIMIT)


# ------------------------------------------------------------------------------ live counters & final results


async def _answer_counts(session: AsyncSession, test_id: int) -> dict[int, int]:
    rows = await session.execute(
        select(UserAnswer.test_question_id, func.count(UserAnswer.id)).where(UserAnswer.test_id == test_id)
        .group_by(UserAnswer.test_question_id)
    )  # fmt: skip
    return {tq_id: n for tq_id, n in rows}


async def refresh_counters(bot: Bot, session_maker: async_sessionmaker[AsyncSession], max_edits: int = 6) -> int:
    """Update "👥 Javob berdi: N" on question messages whose count changed (throttled)."""
    edits = 0
    now = utcnow()
    async with session_maker() as session:
        posts = list(
            (
                await session.execute(
                    select(GroupTestPost)
                    .join(Test, Test.id == GroupTestPost.test_id)
                    .where(GroupTestPost.finalized_at.is_(None), Test.status == TestStatus.ACTIVE)
                )
            )
            .scalars()
            .all()
        )
        for post in posts:
            counts = await _answer_counts(session, post.test_id)
            messages = list(
                (
                    await session.execute(
                        select(GroupQuestionMessage)
                        .where(GroupQuestionMessage.post_id == post.id, GroupQuestionMessage.message_id.is_not(None))
                        .order_by(GroupQuestionMessage.position)
                    )
                )
                .scalars()
                .all()
            )
            total = (
                await session.execute(
                    select(func.count(GroupQuestionMessage.id)).where(GroupQuestionMessage.post_id == post.id)
                )
            ).scalar_one()
            per_post = 0
            for gqm in messages:
                n = counts.get(gqm.test_question_id, 0)
                if n == gqm.rendered_count or (gqm.rendered_at and now - gqm.rendered_at < REFRESH_MIN_INTERVAL):
                    continue
                if per_post >= max_edits:
                    break
                tq = await session.get(TestQuestion, gqm.test_question_id)
                assert tq is not None
                await _call(
                    lambda tq=tq, gqm=gqm, n=n: bot.edit_message_text(
                        render_question(tq, gqm, total, n), chat_id=post.chat_id, message_id=gqm.message_id,
                        reply_markup=answer_keyboard(gqm),
                    ),
                    what="refresh",
                )  # fmt: skip
                gqm.rendered_count = n
                gqm.rendered_at = now
                per_post += 1
                edits += 1
            started = (
                await session.execute(select(func.count(TestAttempt.id)).where(TestAttempt.test_id == post.test_id))
            ).scalar_one()
            await session.commit()
            if post.header_message_id and per_post:
                test = await session.get(Test, post.test_id)
                assert test is not None
                await _call(
                    lambda test=test, post=post, started=started: bot.edit_message_text(
                        render_header(test, started), chat_id=post.chat_id, message_id=post.header_message_id,
                        reply_markup=header_keyboard(post),
                    ),
                    what="header",
                )  # fmt: skip
    return edits


async def finalize_posts(bot: Bot, session_maker: async_sessionmaker[AsyncSession]) -> int:
    """For ended tests: lock answering, show poll-like final results and a closing message."""
    done = 0
    async with session_maker() as session:
        ids = list(
            (
                await session.execute(
                    update(GroupTestPost)
                    .where(
                        GroupTestPost.finalized_at.is_(None),
                        GroupTestPost.test_id.in_(
                            select(Test.id).where(Test.status.in_([TestStatus.EXPIRED, TestStatus.CLOSED]))
                        ),
                    )
                    .values(finalized_at=utcnow())
                    .returning(GroupTestPost.id)
                )
            )
            .scalars()
            .all()
        )
        await session.commit()
        for post_id in ids:
            post = await session.get(GroupTestPost, post_id)
            assert post is not None
            test = await session.get(Test, post.test_id)
            assert test is not None
            reveal = test.answer_reveal != AnswerReveal.NEVER
            dist_rows = await session.execute(
                select(UserAnswer.test_question_id, UserAnswer.selected_option, func.count(UserAnswer.id))
                .where(UserAnswer.test_id == test.id)
                .group_by(UserAnswer.test_question_id, UserAnswer.selected_option)
            )
            distribution: dict[int, dict[str, int]] = {}
            for tq_id, letter, n in dist_rows:
                distribution.setdefault(tq_id, {})[letter] = n
            messages = list(
                (
                    await session.execute(
                        select(GroupQuestionMessage).where(GroupQuestionMessage.post_id == post.id)
                        .order_by(GroupQuestionMessage.position)
                    )
                ).scalars().all()
            )  # fmt: skip
            for gqm in messages:
                if gqm.message_id is None:
                    continue
                tq = await session.get(TestQuestion, gqm.test_question_id)
                assert tq is not None
                await _call(
                    lambda tq=tq, gqm=gqm: bot.edit_message_text(
                        render_question_final(tq, gqm, len(messages), distribution.get(tq.id, {}), reveal),
                        chat_id=post.chat_id, message_id=gqm.message_id, reply_markup=None,
                    ),
                    what="final",
                )  # fmt: skip
            started, completed = (
                await session.execute(
                    select(
                        func.count(TestAttempt.id),
                        func.count(TestAttempt.id).filter(TestAttempt.status == AttemptStatus.COMPLETED),
                    ).where(TestAttempt.test_id == test.id)
                )
            ).one()
            if post.header_message_id:
                await _call(
                    lambda: bot.edit_message_text(
                        render_header(test, started, finished=True), chat_id=post.chat_id,
                        message_id=post.header_message_id, reply_markup=None,
                    ),
                    what="header-final",
                )  # fmt: skip
            await _call(
                lambda: bot.send_message(
                    post.chat_id, t("gt.closed_summary", title=esc(test.title), started=started, completed=completed)
                ),
                what="summary",
            )
            done += 1
            logger.info("Group test finalized", extra={"post_id": post.id, "test_id": test.id})
    return done


async def resume_unsent_posts(bot: Bot, session_maker: async_sessionmaker[AsyncSession]) -> int:
    """Finish posting interrupted posts (e.g. after a restart during flood-control waits)."""
    async with session_maker() as session:
        ids = list(
            (
                await session.execute(
                    select(GroupTestPost.id)
                    .join(Test, Test.id == GroupTestPost.test_id)
                    .where(GroupTestPost.posted_all.is_(False), Test.status == TestStatus.ACTIVE)
                )
            )
            .scalars()
            .all()
        )
    for post_id in ids:
        await send_post(bot, session_maker, post_id)
    return len(ids)


async def group_layout_for_test(session: AsyncSession, test_id: int) -> list[dict] | None:
    """Layout of the group post (same order and letters as in the group), if the test was posted."""
    post_id = (
        await session.execute(select(GroupTestPost.id).where(GroupTestPost.test_id == test_id).limit(1))
    ).scalar_one_or_none()
    return await _layout(session, post_id) if post_id is not None else None


async def active_group_tests(session: AsyncSession, chat_id: int) -> list[Test]:
    """ACTIVE tests (inside their time window) that members of this group can take.

    Includes tests posted in this group, tests targeted at this group, and tests for all employees
    (announced in private chats), so new members always learn about every running test.
    """
    now = utcnow()
    group_id = (await session.execute(select(Group.id).where(Group.chat_id == chat_id))).scalar_one_or_none()
    posted = select(GroupTestPost.test_id).where(GroupTestPost.chat_id == chat_id)
    audience = Test.id.in_(posted) | Test.group_id.is_(None)
    if group_id is not None:
        audience = audience | (Test.group_id == group_id)
    stmt = (
        select(Test)
        .where(audience, Test.status == TestStatus.ACTIVE, Test.starts_at <= now, Test.deadline_at > now)
        .order_by(Test.deadline_at)
    )
    return list((await session.execute(stmt)).scalars().all())


async def group_tests_due(session: AsyncSession) -> list[int]:
    return list(
        (
            await session.execute(
                select(Test.id).where(Test.status == TestStatus.ACTIVE, Test.delivery_mode == DeliveryMode.GROUP)
            )
        )
        .scalars()
        .all()
    )


@dataclass
class QuestionAnswers:
    question: TestQuestion
    opts: list[str]  # snapshot letters in the order shown (group post order, or snapshot order)
    group_mode: bool
    answers: list[tuple[User, UserAnswer]]

    def display_of(self, original: str) -> str:
        return LETTERS[self.opts.index(original)] if original in self.opts else original


async def answers_by_question(session: AsyncSession, test_id: int) -> list[QuestionAnswers]:
    """Every employee's answer to every question (admin view: who chose what).

    For group tests the questions follow the order and A/B/C/D letters shown in the group.
    """
    questions = {
        q.id: q for q in (await session.execute(select(TestQuestion).where(TestQuestion.test_id == test_id))).scalars()
    }
    post = (
        await session.execute(select(GroupTestPost).where(GroupTestPost.test_id == test_id).limit(1))
    ).scalar_one_or_none()
    if post is not None:
        order = [
            (tq_id, list(opts))
            for tq_id, opts in await session.execute(
                select(GroupQuestionMessage.test_question_id, GroupQuestionMessage.opts)
                .where(GroupQuestionMessage.post_id == post.id)
                .order_by(GroupQuestionMessage.position)
            )
        ]
    else:
        order = [(q.id, list(q.options)) for q in sorted(questions.values(), key=lambda q: q.position)]
    rows = await session.execute(
        select(UserAnswer, User).join(User, User.id == UserAnswer.user_id).where(UserAnswer.test_id == test_id)
        .order_by(User.full_name, User.first_name)
    )  # fmt: skip
    by_tq: dict[int, list[tuple[User, UserAnswer]]] = {}
    for answer, user in rows:
        by_tq.setdefault(answer.test_question_id, []).append((user, answer))
    return [
        QuestionAnswers(questions[tq_id], opts, post is not None, by_tq.get(tq_id, []))
        for tq_id, opts in order
        if tq_id in questions
    ]


async def option_distribution(session: AsyncSession, test_id: int) -> dict[int, dict[str, int]]:
    """``{test_question_id: {snapshot_letter: count}}``."""
    rows = await session.execute(
        select(UserAnswer.test_question_id, UserAnswer.selected_option, func.count(UserAnswer.id))
        .where(UserAnswer.test_id == test_id)
        .group_by(UserAnswer.test_question_id, UserAnswer.selected_option)
    )
    result: dict[int, dict[str, int]] = {}
    for tq_id, letter, n in rows:
        result.setdefault(tq_id, {})[letter] = n
    return result
