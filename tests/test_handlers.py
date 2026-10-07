"""End-to-end handler tests: real Dispatcher + routers + middlewares + DB, fake Telegram transport."""

import pytest
import pytest_asyncio
from aiogram import Bot
from sqlalchemy import select

from app.keyboards.callbacks import AdminCB, AnsCB, EmpCB
from app.models import TestAttempt, User, UserStatus
from app.services import settings_service
from app.services import test_builder as tb
from app.services.attempts import original_to_display
from tests.factories import make_employee, make_material_with_text, make_question, make_test
from tests.telegram_mock import RecordingSession, callback_update, message_update

ADMIN = 1000


_DP = None


def _dispatcher():
    """Routers are module-level singletons, so the dispatcher is built once per test session."""
    global _DP
    if _DP is None:
        from app.main import create_dispatcher

        _DP = create_dispatcher()
    return _DP


@pytest_asyncio.fixture
async def tg(session_maker):
    from aiogram.client.default import DefaultBotProperties

    session = RecordingSession()
    bot = Bot(token="123456:TEST-TOKEN-not-real", session=session, default=DefaultBotProperties(parse_mode="HTML"))
    dp = _dispatcher()
    dp.fsm.storage.storage.clear()  # fresh FSM state per test

    async def feed(update):
        await dp.feed_update(bot, update)
        return session

    feed.session = session
    yield feed


async def test_admin_start_shows_admin_panel(tg):
    s = await tg(message_update(ADMIN, "/start"))
    assert any("ADMIN PANEL" in t for t in s.texts())


async def test_non_admin_cannot_open_admin_panel(tg, session):
    await make_employee(session, 2000, "Oddiy Xodim")
    s = await tg(message_update(2000, "/admin"))
    assert any("faqat administratorlar" in t for t in s.texts())
    s.clear()
    # A forged admin callback is rejected server-side as well.
    await tg(callback_update(2000, AdminCB(s="emp").pack()))
    assert any("faqat administratorlar" in a for a in s.alerts())
    assert not any("XODIMLAR" in t for t in s.texts())


async def test_employee_registration_flow(tg, session_maker):
    s = await tg(message_update(3000, "/start"))
    assert any("Ro'yxatdan o'tish" in t for t in s.texts())
    s.clear()
    await tg(message_update(3000, "Ali"))
    assert any("to'liq ism-sharif" in t for t in s.texts())
    s.clear()
    await tg(message_update(3000, "Valiyev Ali Hasanovich"))
    assert any("ko'rib chiqilmoqda" in t for t in s.texts())
    # Admin was notified with approve buttons.
    assert any("Yangi xodim" in t for t in s.texts())
    async with session_maker() as session:
        user = (await session.execute(select(User).where(User.telegram_id == 3000))).scalar_one()
        assert user.full_name == "Valiyev Ali Hasanovich" and user.status == UserStatus.PENDING
        uid = user.id
    s.clear()
    await tg(callback_update(ADMIN, AdminCB(s="emp_appr", id=uid).pack()))
    async with session_maker() as session:
        assert (await session.get(User, uid)).status == UserStatus.ACTIVE
    assert any("faollashtirildi" in t for t in s.texts())


async def test_auto_approve_all_setting(tg, session_maker):
    async with session_maker() as session:
        await settings_service.set_value(session, "auto_approve_all", True)
    await tg(message_update(3100, "/start"))
    s = await tg(message_update(3100, "Karimova Dilnoza"))
    assert any("muvaffaqiyatli" in t for t in s.texts())


async def test_admin_menu_sections_render(tg, session_maker):
    for section in ("emp", "mat", "news", "tst", "qb", "st", "rep", "grp", "set"):
        tg.session.clear()
        await tg(callback_update(ADMIN, AdminCB(s=section).pack()))
        assert tg.session.texts(), f"section {section} produced no output"
        assert not any("Kutilmagan xatolik" in t for t in tg.session.texts()), section
    tg.session.clear()
    await tg(callback_update(ADMIN, AdminCB(s="rk", v="month").pack()))
    assert any("reytingi" in t for t in tg.session.texts())


async def test_employee_takes_test_via_buttons(tg, session_maker):
    async with session_maker() as session:
        mid = await make_material_with_text(session_maker)
        for i in range(2):
            await make_question(session, text_=f"Telegram orqali savol {i} nima?", material_id=mid, correct="C")
        test = await make_test(session, question_count=2)
        await tb.assemble_questions(session_maker, test.id, None)
        await tb.mark_ready(session, test.id)
        await tb.publish(session, test.id)
        emp = await make_employee(session, 4000, "Test Topshiruvchi")
        test_id, emp_id = test.id, emp.id

    s = await tg(message_update(4000, "📝 Mening testlarim"))
    assert any("Sud amaliyoti" in t for t in s.texts())
    s.clear()
    await tg(callback_update(4000, EmpCB(a="start", id=test_id).pack()))
    assert any("Savol <b>1/2</b>" in t for t in s.texts())
    # Double click on START does not create a second attempt.
    await tg(callback_update(4000, EmpCB(a="start", id=test_id).pack()))
    async with session_maker() as session:
        attempts = (await session.execute(select(TestAttempt).where(TestAttempt.user_id == emp_id))).scalars().all()
        assert len(attempts) == 1
        attempt = attempts[0]
        from app.models import TestQuestion

        letters = []
        for item in attempt.layout:
            tq = await session.get(TestQuestion, item["tq"])
            letters.append(original_to_display(item, tq.correct_option))
    for pos, letter in enumerate(letters):
        s.clear()
        await tg(callback_update(4000, AnsCB(at=attempt.id, pos=pos, o=letter).pack()))
    assert any("TEST YAKUNLANDI" in t for t in s.texts())
    assert any("2/2" in t for t in s.texts())
    # Pressing an old answer button again is harmless.
    s.clear()
    await tg(callback_update(4000, AnsCB(at=attempt.id, pos=0, o=letters[0]).pack()))
    assert s.alerts() and not any("YAKUNLANDI" in t for t in s.texts())
    # Another employee cannot see this result.
    async with session_maker() as session:
        await make_employee(session, 4001, "Boshqa Xodim")
    s.clear()
    await tg(callback_update(4001, EmpCB(a="res", id=attempt.id).pack()))
    assert not any("YAKUNLANDI" in t for t in s.texts())
    # Admin sees the participant list with the completed employee.
    s.clear()
    await tg(callback_update(ADMIN, AdminCB(s="tst_part", id=test_id, v="all").pack()))
    assert any("Test Topshiruvchi" in t and "Yakunlagan" in t for t in s.texts())


async def test_admin_test_wizard_creates_draft(tg, session_maker):
    from app.keyboards.callbacks import WizCB
    from app.models import Test

    async with session_maker() as session:
        mid = await make_material_with_text(session_maker)
        for i in range(3):
            await make_question(session, text_=f"Wizard savoli {i} nima?", material_id=mid)
    await tg(callback_update(ADMIN, AdminCB(s="tst_new").pack()))
    await tg(message_update(ADMIN, "Oktyabr o'quvi"))
    await tg(callback_update(ADMIN, WizCB(f="description", v="_skip").pack()))
    await tg(callback_update(ADMIN, WizCB(f="count", v="10").pack()))
    for f, v in (("options", "4"), ("difficulty", "mixed")):
        await tg(callback_update(ADMIN, WizCB(f=f, v=v).pack()))
    await tg(callback_update(ADMIN, WizCB(f="src_all").pack()))
    await tg(callback_update(ADMIN, WizCB(f="src_done").pack()))
    await tg(callback_update(ADMIN, WizCB(f="focus", v="_skip").pack()))
    await tg(callback_update(ADMIN, WizCB(f="audience", v="0").pack()))
    await tg(callback_update(ADMIN, WizCB(f="start", v="now").pack()))
    for f, v in (
        ("duration", "24"),
        ("rand_q", "1"),
        ("rand_o", "1"),
        ("reveal", "after"),
        ("retakes", "0"),
        ("passing", "70"),
    ):
        await tg(callback_update(ADMIN, WizCB(f=f, v=v).pack()))
    assert any("TEST SOZLAMALARI" in t for t in tg.session.texts())
    await tg(callback_update(ADMIN, WizCB(f="confirm", v="1").pack()))
    await tg(callback_update(ADMIN, WizCB(f="confirm", v="1").pack()))  # double click
    import asyncio

    from app.services import background

    for _ in range(50):
        if background.running_count() == 0:
            break
        await asyncio.sleep(0.1)
    async with session_maker() as session:
        tests = (await session.execute(select(Test))).scalars().all()
        assert len(tests) == 1
        test = tests[0]
        assert test.title == "Oktyabr o'quvi" and test.question_count == 10 and test.passing_percent == 70
        total, _ = await tb.question_counts(session, test.id)
        assert total == 3  # bank had 3; AI not configured in tests so 7 are missing
    assert any("Yetishmayapti: 7" in t for t in tg.session.texts())


@pytest.mark.parametrize("payload", ["/start test_999999", "/start"])
async def test_deep_link_unknown_test_is_safe(tg, session_maker, payload):
    async with session_maker() as session:
        await make_employee(session, 5000, "Deep Link")
    s = await tg(message_update(5000, payload))
    assert s.texts()
    assert not any("Kutilmagan xatolik" in t for t in s.texts())
