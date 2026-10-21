"""Regressions for access checks and retryable employee buttons."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramNetworkError

from app.handlers import employee
from app.keyboards.callbacks import EmpCB
from app.models import AttemptStatus, Test, TestAttempt, UserStatus
from app.services import attempts
from app.utils.time import utcnow
from tests.telegram_mock import RecordingSession, callback_update


@pytest.mark.parametrize("status", [UserStatus.BLOCKED, UserStatus.INACTIVE, UserStatus.PENDING])
async def test_disallowed_user_cannot_submit_old_answer(status):
    session = AsyncMock()
    user = SimpleNamespace(status=status)
    result = await attempts.submit_answer(session, user, 1, 0, "A")
    assert result.outcome == attempts.AnswerOutcome.BLOCKED
    session.execute.assert_not_awaited()


async def test_next_question_failure_preserves_retry_button(monkeypatch):
    from datetime import timedelta

    transport = RecordingSession()
    bot = Bot(token="123456:TEST-TOKEN-not-real", session=transport)
    callback = callback_update(2000, EmpCB(a="next", id=1).pack()).callback_query.as_(bot)
    user = SimpleNamespace(id=2, status=UserStatus.ACTIVE)
    attempt = SimpleNamespace(id=1, user_id=2, test_id=3, status=AttemptStatus.IN_PROGRESS,
                              deadline_at=utcnow() + timedelta(hours=1))
    test = SimpleNamespace(paused=False)
    session = AsyncMock()
    session.get.side_effect = lambda model, _id: attempt if model is TestAttempt else test if model is Test else None
    monkeypatch.setattr(attempts, "current_question", AsyncMock(return_value=(object(), {})))
    monkeypatch.setattr(employee, "send_current_question", AsyncMock(
        side_effect=TelegramNetworkError(method=None, message="temporary network failure")))
    with pytest.raises(TelegramNetworkError):
        await employee.cb_close_result(callback, EmpCB(a="next", id=1), session, bot, user)
    from aiogram.methods import EditMessageReplyMarkup

    assert not any(isinstance(request, EditMessageReplyMarkup) for request in transport.requests)
    await bot.session.close()


async def test_paused_and_missed_tests_remain_visible(session_maker, session):
    from app.services import test_builder
    from tests.factories import make_employee
    from tests.test_test_flow import _ready_active_test

    user = await make_employee(session, 7812, "Test Xodim")
    test = await _ready_active_test(session_maker, session)
    test.paused = True
    await session.commit()
    item = next(x for x in await attempts.my_tests(session, user) if x.test.id == test.id)
    assert item.state == attempts.MyTestState.PAUSED and not item.can_start
    await test_builder.close_test(session, test.id)
    item = next(x for x in await attempts.my_tests(session, user) if x.test.id == test.id)
    assert item.state == attempts.MyTestState.EXPIRED and not item.can_start


async def test_old_start_button_cannot_resume_paused_attempt(session_maker, session):
    from tests.factories import make_employee
    from tests.test_test_flow import _ready_active_test

    user = await make_employee(session, 7813, "Test Xodim")
    test = await _ready_active_test(session_maker, session)
    assert (await attempts.start_attempt(session, user, test.id)).attempt is not None
    test.paused = True
    await session.commit()
    result = await attempts.start_attempt(session, user, test.id)
    assert result.error == attempts.StartError.PAUSED


async def test_group_callback_injects_clicking_employee(monkeypatch):
    from app.middlewares import user as middleware

    user = SimpleNamespace(id=2)
    group = SimpleNamespace(is_active=True)
    monkeypatch.setattr(middleware.group_service, "get_by_chat", AsyncMock(return_value=group))
    monkeypatch.setattr(middleware, "upsert_user", AsyncMock(return_value=user))
    membership = AsyncMock()
    monkeypatch.setattr(middleware.group_service, "mark_membership", membership)
    handler = AsyncMock()
    data = {"session": AsyncMock()}
    update = callback_update(2000, EmpCB(a="card", id=1).pack(), chat_id=-100123)
    await middleware.UserMiddleware()(handler, update, data)
    assert data["user"] is user
    assert data["group"] is group
    assert middleware.upsert_user.await_args.args[1].id == 2000
    membership.assert_awaited_once()
    handler.assert_awaited_once()
