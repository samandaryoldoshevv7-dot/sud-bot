"""Background scheduler (runs inside the bot process; designed for a single Railway replica).

Every tick (idempotent, safe to re-run):
* activate published tests whose start time has come and announce them;
* expire tests past their deadline (and their unfinished attempts) and send admins a summary;
* expire overdue in-progress attempts;
* send deadline reminders.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from aiogram import Bot
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.locales import t
from app.models import DeliveryMode, ParticipationStatus, Test, TestStatus
from app.services import group_tests, settings_service
from app.services.attempts import expire_attempts
from app.services.notifications import announce_test, notify_admins, remind_unfinished
from app.services.test_builder import activate_due, expire_due
from app.statistics.participation import participation
from app.utils.text import esc, pct
from app.utils.time import utcnow

logger = logging.getLogger(__name__)


async def claim_marker(session: AsyncSession, test_id: int, column: str) -> bool:
    """Atomically set a ``*_at`` marker so a notification is sent exactly once."""
    col = getattr(Test, column)
    claimed = (
        await session.execute(
            update(Test).where(Test.id == test_id, col.is_(None)).values({column: utcnow()}).returning(Test.id)
        )
    ).scalar_one_or_none()
    await session.commit()
    return claimed is not None


async def launch_test(bot: Bot, session_maker: async_sessionmaker[AsyncSession], test_id: int) -> None:
    """Start an ACTIVE test exactly once: post it into its group, or announce it for private chats."""
    async with session_maker() as session:
        if not await claim_marker(session, test_id, "announced_at"):
            return
        test = await session.get(Test, test_id)
        if test is None:
            return
        if test.delivery_mode == DeliveryMode.GROUP:
            mode_group = True
        else:
            mode_group = False
            await announce_test(bot, session, test)
    if mode_group:
        await group_tests.start_in_group(bot, session_maker, test_id)


async def run_tick(bot: Bot | None, session_maker: async_sessionmaker[AsyncSession]) -> dict:
    now = utcnow()
    report: dict = {}
    async with session_maker() as session:
        report["activated"] = await activate_due(session, now)
        report["expired_tests"] = await expire_due(session, now)
        report["expired_attempts"] = await expire_attempts(session, None, now)

        if bot is None:
            return report

        # Announce active tests not yet announced (covers immediate publish and scheduled start).
        to_announce = (
            (
                await session.execute(
                    select(Test.id).where(Test.status == TestStatus.ACTIVE, Test.announced_at.is_(None))
                )
            )
            .scalars()
            .all()
        )
        for test_id in to_announce:
            await launch_test(bot, session_maker, test_id)
        # Group tests: finish interrupted posting, then close ended tests in their groups.
        report["resumed_posts"] = await group_tests.resume_unsent_posts(bot, session_maker)
        report["finalized_posts"] = await group_tests.finalize_posts(bot, session_maker)

        hours = int(await settings_service.get_value(session, "reminder_hours_before"))
        if hours > 0:
            due = (
                (
                    await session.execute(
                        select(Test.id).where(
                            Test.status == TestStatus.ACTIVE,
                            Test.reminder_sent_at.is_(None),
                            Test.deadline_at <= now + timedelta(hours=hours),
                            Test.deadline_at > now,
                            Test.activated_at <= now - timedelta(minutes=30),
                        )
                    )
                )
                .scalars()
                .all()
            )
            for test_id in due:
                if await claim_marker(session, test_id, "reminder_sent_at"):
                    test = await session.get(Test, test_id)
                    if test:
                        sent = await remind_unfinished(bot, session, test)
                        logger.info("Deadline reminders sent", extra={"test_id": test_id, "sent": sent})

        ended = (
            (
                await session.execute(
                    select(Test.id).where(
                        Test.status.in_([TestStatus.EXPIRED, TestStatus.CLOSED]),
                        Test.summary_sent_at.is_(None),
                        Test.published_at.is_not(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        for test_id in ended:
            if await claim_marker(session, test_id, "summary_sent_at"):
                test = await session.get(Test, test_id)
                if test:
                    await _send_summary(bot, session, test)
    return report


async def _send_summary(bot: Bot, session: AsyncSession, test: Test) -> None:
    summary = await participation(session, test, utcnow())
    from app.keyboards.callbacks import AdminCB
    from app.keyboards.common import kb

    text = t(
        "summary.test_ended",
        title=esc(test.title),
        id=test.id,
        total=summary.total,
        completed=summary.count(ParticipationStatus.COMPLETED),
        expired=summary.count(ParticipationStatus.EXPIRED) + summary.count(ParticipationStatus.IN_PROGRESS),
        not_started=summary.count(ParticipationStatus.NOT_STARTED),
        avg=pct(summary.average_score),
        rate=pct(summary.participation_rate),
    )
    await notify_admins(
        bot,
        text,
        kb(
            [(t("tests.btn.participants"), AdminCB(s="tst_part", id=test.id, v="all"))],
            [(t("tests.btn.report_xlsx"), AdminCB(s="tst_rep", id=test.id))],
        ),
    )


async def scheduler_loop(bot: Bot, session_maker: async_sessionmaker[AsyncSession], stop: asyncio.Event) -> None:
    interval = get_settings().scheduler_interval_seconds
    logger.info("Scheduler started", extra={"interval": interval})
    while not stop.is_set():
        try:
            report = await run_tick(bot, session_maker)
            if any(report.get(k) for k in ("activated", "expired_tests", "expired_attempts")):
                logger.info("Scheduler tick", extra={k: str(v) for k, v in report.items()})
        except Exception:
            logger.exception("Scheduler tick failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            pass
    logger.info("Scheduler stopped")


async def group_refresh_loop(bot: Bot, session_maker: async_sessionmaker[AsyncSession], stop: asyncio.Event) -> None:
    """Keeps "👥 Javob berdi: N" counters on group question messages up to date (throttled)."""
    while not stop.is_set():
        try:
            await group_tests.refresh_counters(bot, session_maker)
        except Exception:
            logger.exception("Group counter refresh failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=15)
        except TimeoutError:
            pass
