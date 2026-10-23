"""Personal feedback on the group timeline, plus public final rankings.

Personal messages ALWAYS carry EphemeralMessageParameters. Never fall back to
an ordinary group message: only the requesting Telegram user may see details.
"""

from __future__ import annotations

import logging
import time

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import EphemeralMessageParameters
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession

from app.keyboards.callbacks import GroupExplanationCB, GroupResultCB
from app.keyboards.common import kb
from app.models import (
    AnswerReveal,
    AttemptStatus,
    GroupMember,
    GroupQuestionMessage,
    GroupTestPost,
    Test,
    TestAttempt,
    TestQuestion,
    TestStatus,
    User,
    UserAnswer,
)
from app.services.attempts import finalize_attempt
from app.utils.text import esc, pct, split_message, truncate
from app.utils.time import fmt_dt, utcnow

logger = logging.getLogger(__name__)
_admin_cache: dict[tuple[int, int], tuple[float, bool]] = {}


def result_keyboard(post_id: int):
    return kb([("📊 Mening natijam", GroupResultCB(p=post_id))])


def explanation_keyboard(message_id: int):
    return kb([("📖 Javobim va izoh", GroupExplanationCB(m=message_id))])


async def send_personal(bot: Bot, chat_id: int, telegram_id: int, text: str, callback_id: str | None = None) -> bool:
    """With a callback any bot can reply privately in the group within 15s.

    Unsolicited feedback requires the bot to be a group administrator. Offline
    users can retrieve the durable result later by pressing the result button.
    """
    try:
        if callback_id is None:
            key = (bot.id, chat_id)
            cached = _admin_cache.get(key)
            if cached is None or time.monotonic() - cached[0] > 300:
                member = await bot.get_chat_member(chat_id, bot.id)
                cached = (time.monotonic(), member.status in ("administrator", "creator"))
                _admin_cache[key] = cached
            if not cached[1]:
                return False
        # Plain text chunks avoid splitting HTML entities/tags in long legal explanations.
        for part in split_message(text, 2000):
            await bot.send_message(
                chat_id,
                part,
                parse_mode=None,
                protect_content=True,
                ephemeral_message_parameters=EphemeralMessageParameters(
                    receiver_user_id=telegram_id,
                    callback_query_id=callback_id,
                ),
            )
        return True
    except TelegramAPIError as exc:
        logger.warning("Personal group message unavailable", extra={"chat_id": chat_id, "error": str(exc)[:160]})
        return False


async def own_attempt(session: AsyncSession, test_id: int, telegram_id: int) -> TestAttempt | None:
    attempt = (
        await session.execute(
            select(TestAttempt)
            .join(User, User.id == TestAttempt.user_id)
            .where(
                TestAttempt.test_id == test_id,
                User.telegram_id == telegram_id,
                TestAttempt.status != AttemptStatus.CANCELLED,
            )
            .order_by(TestAttempt.attempt_no.desc())
            .limit(1)
            .with_for_update(of=TestAttempt)
        )
    ).scalar_one_or_none()
    if attempt and attempt.status == AttemptStatus.IN_PROGRESS:
        if attempt.deadline_at <= utcnow() or attempt.test.status in (TestStatus.EXPIRED, TestStatus.CLOSED):
            await finalize_attempt(session, attempt, AttemptStatus.EXPIRED, utcnow())
            await session.commit()
    await session.commit()  # release the row lock before any Telegram/network operation
    return attempt


def personal_result(test: Test, attempt: TestAttempt) -> str:
    active = attempt.status == AttemptStatus.IN_PROGRESS
    heading = (
        "TEST DAVOM ETMOQDA"
        if active
        else ("TEST VAQTI TUGADI" if attempt.status == AttemptStatus.EXPIRED else "TEST YAKUNLANDI")
    )
    lines = [heading, test.title, f"Javob berildi: {attempt.answered_count}/{attempt.total_questions}"]
    if not active or test.answer_reveal == AnswerReveal.IMMEDIATE:
        lines += [
            f"To'g'ri: {attempt.correct_count}",
            f"Noto'g'ri: {attempt.incorrect_count}",
            f"Javobsiz: {attempt.total_questions - attempt.answered_count}",
            f"Natija: {pct(attempt.correct_count * 100 / max(1, attempt.total_questions))}",
        ]
    if active:
        lines.append(f"Siz uchun muddat: {fmt_dt(attempt.deadline_at)}")
    else:
        lines.append(
            f"Sarflangan vaqt: {(attempt.duration_seconds or 0) // 60} daqiqa {(attempt.duration_seconds or 0) % 60} soniya"
        )
    lines.append("Bu xabar faqat sizga ko'rinadi. Natijangiz saqlangan.")
    return "\n".join(lines)


def short_result(test: Test, attempt: TestAttempt) -> str:
    """The result for a popup (at most ~190 characters), seen only by the one who pressed."""
    active = attempt.status == AttemptStatus.IN_PROGRESS
    lines = [f"Javob berildi: {attempt.answered_count}/{attempt.total_questions}"]
    if not active or test.answer_reveal == AnswerReveal.IMMEDIATE:
        lines.append(
            f"To'g'ri: {attempt.correct_count}, noto'g'ri: {attempt.incorrect_count} "
            f"({pct(attempt.correct_count * 100 / max(1, attempt.total_questions))})"
        )
    lines.append("Test davom etmoqda" if active else "Test yakunlangan")
    return "\n".join(lines)


async def own_explanation(session: AsyncSession, gqm: GroupQuestionMessage, telegram_id: int) -> str:
    post = await session.get(GroupTestPost, gqm.post_id)
    test = await session.get(Test, post.test_id)
    attempt = await own_attempt(session, test.id, telegram_id)
    if attempt is None:
        return "Avval shu testdagi savolga javob bering."
    answer = (
        await session.execute(
            select(UserAnswer).where(
                UserAnswer.attempt_id == attempt.id,
                UserAnswer.test_question_id == gqm.test_question_id,
            )
        )
    ).scalar_one_or_none()
    if test.answer_reveal == AnswerReveal.NEVER:
        return "Bu testda to'g'ri javoblar va izohlar yashirilgan. Natijangizni ko'rishingiz mumkin."
    if test.answer_reveal == AnswerReveal.AFTER_COMPLETION and attempt.status == AttemptStatus.IN_PROGRESS:
        return "Javob va izoh testni tugatganingizdan keyin ochiladi."
    if answer is None and attempt.status == AttemptStatus.IN_PROGRESS:
        return "Avval shu savolga javob bering."
    tq = await session.get(TestQuestion, gqm.test_question_id)
    shown = {original: chr(65 + i) for i, original in enumerate(gqm.opts)}
    lines = [f"{gqm.position + 1}-savol", tq.question_text, ""]
    if answer:
        lines += [
            "TO'G'RI JAVOB" if answer.is_correct else "NOTO'G'RI JAVOB",
            f"Siz tanladingiz: {shown[answer.selected_option]}) {tq.options[answer.selected_option]}",
        ]
    else:
        lines.append("Bu savolga javob bermagansiz.")
    lines += [
        f"To'g'ri javob: {shown[tq.correct_option]}) {tq.options[tq.correct_option]}",
        "",
        "Nima uchun:",
        tq.explanation or "Manbadagi qoida:",
        "",
        f"Manba: {tq.source_name or tq.source_reference}",
    ]
    if tq.source_excerpt:
        lines += ["Manbadan parcha:", tq.source_excerpt]
    return "\n".join(lines)


async def ranking_rows(session: AsyncSession, post: GroupTestPost):
    rows = (
        await session.execute(
            select(TestAttempt, User)
            .join(User, User.id == TestAttempt.user_id)
            .join(GroupMember, GroupMember.user_id == User.id)
            .where(
                TestAttempt.test_id == post.test_id,
                GroupMember.group_id == post.group_id,
                TestAttempt.status.in_([AttemptStatus.COMPLETED, AttemptStatus.EXPIRED]),
            )
            .order_by(TestAttempt.user_id, TestAttempt.attempt_no.desc())
            .ext(distinct_on(TestAttempt.user_id))
        )
    ).all()
    # Same number of correct answers means the same rank; names only stabilize display order.
    return sorted(rows, key=lambda r: (-r[0].correct_count, r[1].display_name.casefold(), r[1].id))


async def ranking_pages(session: AsyncSession, post: GroupTestPost, test: Test) -> list[str]:
    rows = await ranking_rows(session, post)
    lines = [
        "⏰ <b>TEST MUDDATI TUGADI</b>" if test.status == TestStatus.EXPIRED else "🏁 <b>TEST YAKUNLANDI</b>",
        esc(test.title),
        f"Ishtirokchilar: {len(rows)}",
        "",
        "🏆 <b>Yakuniy reyting</b>",
    ]
    previous, rank = None, 0
    for index, (attempt, user) in enumerate(rows, 1):
        if attempt.correct_count != previous:
            rank = index
            previous = attempt.correct_count
        lines.append(
            f"{rank}. {esc(truncate(user.display_name, 60))} — "
            f"{attempt.correct_count}/{attempt.total_questions} ({pct(attempt.score_percent)})"
        )
    if not rows:
        lines.append("Hali qatnashganlar yo'q.")
    lines += [
        "",
        "Ko'p to'g'ri javob — yuqori o'rin. Teng natija — teng o'rin.",
        "Batafsil natija uchun «Mening natijam»ni bosing: faqat o'zingizga ko'rinadi.",
    ]
    return split_message("\n".join(lines), 3000)


async def notify_result_in_groups(bot: Bot, session: AsyncSession, attempt: TestAttempt, user: User) -> None:
    posts = (
        (
            await session.execute(
                select(GroupTestPost)
                .join(GroupMember, GroupMember.group_id == GroupTestPost.group_id)
                .where(
                    GroupTestPost.test_id == attempt.test_id,
                    GroupMember.user_id == user.id,
                    GroupMember.is_member.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    for post in posts:
        await send_personal(bot, post.chat_id, user.telegram_id, personal_result(attempt.test, attempt))
