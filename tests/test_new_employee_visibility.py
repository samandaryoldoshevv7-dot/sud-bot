"""New employees / new group members must see every running test without hunting for it."""

from datetime import UTC, datetime

from aiogram.types import Chat, Message, Update, User
from sqlalchemy import select

from app.keyboards.callbacks import AdminCB
from app.models import DeliveryMode, UserStatus
from app.models import User as DbUser
from app.services import group_tests, settings_service
from app.services import groups as group_service
from app.services import test_builder as tb
from app.services import users as user_service
from tests.factories import make_employee, make_material_with_text, make_question, make_test, tg_user
from tests.telegram_mock import callback_update, message_update
from tests.test_group_flow import tg  # noqa: F401  (fixture)

GROUP_CHAT = -1009876543210
ADMIN = 1000


async def _active_test(session_maker, session, *, group_id=None, delivery=DeliveryMode.PRIVATE, title="Faol test"):
    mid = await make_material_with_text(session_maker)
    for i in range(2):
        await make_question(session, text_=f"{title} savoli {i} nima?", material_id=mid)
    test = await make_test(session, title=title, question_count=2, group_id=group_id, delivery_mode=delivery)
    await tb.assemble_questions(session_maker, test.id, None)
    await tb.mark_ready(session, test.id)
    await tb.publish(session, test.id)
    return test


async def _group(session):
    admin = await user_service.upsert_user(session, tg_user(ADMIN))
    group, _ = await group_service.register_group(session, GROUP_CHAT, "Sud xodimlari", admin)
    return group


async def test_new_employee_sees_running_tests_right_after_registration(tg, session_maker):  # noqa: F811
    async with session_maker() as session:
        await settings_service.set_value(session, "auto_approve_all", True)
        await _active_test(session_maker, session, title="Hammaga test")
    await tg(message_update(9301, "/start", "Yangi"))
    tg.session.clear()
    await tg(message_update(9301, "Yangiyev Xodim Aliyevich", "Yangi"))
    texts = "\n".join(tg.session.texts())
    assert "muvaffaqiyatli" in texts and "Hammaga test" in texts  # tests listed without pressing anything


async def test_manually_approved_member_sees_group_test(tg, session_maker):  # noqa: F811
    async with session_maker() as session:
        group = await _group(session)
        await _active_test(session_maker, session, group_id=group.id, delivery=DeliveryMode.GROUP, title="Guruh testi")
        await make_employee(session, 9302, "Qolda Tasdiqlangan")  # ACTIVE, but no recorded group membership
    tg.session.member_ids.add(9302)  # Telegram says: this user IS a member of the group
    await tg(message_update(9302, "/start", "Qolda"))
    assert any("Guruh testi" in t for t in tg.session.texts())
    tg.session.clear()
    await tg(message_update(9302, "📝 Mening testlarim", "Qolda"))
    assert any("Guruh testi" in t for t in tg.session.texts())


async def test_non_member_does_not_see_other_groups_test(tg, session_maker):  # noqa: F811
    async with session_maker() as session:
        group = await _group(session)
        await _active_test(session_maker, session, group_id=group.id, delivery=DeliveryMode.GROUP, title="Begona guruh")
        await make_employee(session, 9303, "Boshqa Xodim")
    await tg(message_update(9303, "📝 Mening testlarim", "Boshqa"))
    assert not any("Begona guruh" in t for t in tg.session.texts())


async def test_admin_approval_sends_running_tests(tg, session_maker):  # noqa: F811
    async with session_maker() as session:
        await _active_test(session_maker, session, title="Tasdiqdan keyin")
        pending = await user_service.upsert_user(session, tg_user(9304, "Kutayotgan"), private_chat=True)
        await user_service.set_full_name(session, pending, "Kutayotgan Xodim")
        pending_id = pending.id
    await tg(callback_update(ADMIN, AdminCB(s="emp_appr", id=pending_id).pack()))
    sent_to_employee = [m.text for m in tg.session.sent_to(9304)]
    assert any("faollashtirildi" in t for t in sent_to_employee)
    assert any("Tasdiqdan keyin" in t for t in sent_to_employee)
    async with session_maker() as session:
        assert (await session.get(DbUser, pending_id)).status == UserStatus.ACTIVE


async def test_new_group_member_is_told_about_private_chat_tests(tg, session_maker):  # noqa: F811
    async with session_maker() as session:
        await _group(session)
        test = await _active_test(session_maker, session, title="Shaxsiy chat testi")  # announced privately
        assert await group_tests.active_group_tests(session, GROUP_CHAT) == [test]
    newcomer = User(id=9305, is_bot=False, first_name="Yangi", last_name="A'zo")
    await tg(
        Update(
            update_id=991001,
            message=Message(
                message_id=991001, date=datetime.now(UTC), chat=Chat(id=GROUP_CHAT, type="supergroup", title="Sud"),
                from_user=newcomer, new_chat_members=[newcomer],
            ),
        )
    )  # fmt: skip
    sent = tg.session.sent_to(GROUP_CHAT)
    assert len(sent) == 1 and "Shaxsiy chat testi" in sent[0].text
    assert sent[0].reply_markup.inline_keyboard[0][0].url.endswith(f"start=test_{test.id}")
    async with session_maker() as session:
        user = (await session.execute(select(DbUser).where(DbUser.telegram_id == 9305))).scalar_one()
        assert user.id
