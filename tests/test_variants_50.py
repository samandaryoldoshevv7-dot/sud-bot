"""50-question variants from all materials: as many variants as the unique questions give (the last
one smaller), never sent by themselves, no question repeated, the source at the start of every
question, and nothing posted into the group per answer."""

import pytest
from sqlalchemy import func, select

from app.handlers.admin import create_test as ct
from app.models import BotSetting, Material, Test, TestQuestion, TestStatus
from app.services import group_results
from app.services import test_builder as tb
from app.services.mixed_tests import split_proportional
from app.services.scheduler import launch_test
from tests.factories import make_employee, make_material_with_text, make_question
from tests.telegram_mock import poll_answer_update, poll_id_of
from tests.test_group_flow import tg  # noqa: F401  (fixture)
from tests.test_mixed_and_timer import ADMIN, GROUP_CHAT
from tests.test_test_mode import _group_polls, _setup


async def _pack_with_bank(session_maker, shares: list[int]) -> int:
    """A 50-question test over len(shares) materials whose bank holds ``shares[i]`` unique questions."""
    async with session_maker() as session:
        admin = await make_employee(session, ADMIN, "Admin Bosh")
    mids = [
        await make_material_with_text(session_maker, title=f"Qonun {i + 1}", file_name=f"qonun{i + 1}.pdf")
        for i in range(len(shares))
    ]
    async with session_maker() as session:
        for mid, n in zip(mids, shares, strict=True):
            for k in range(n):
                await make_question(session, text_=f"M{mid}: {k + 1}-qoida nimani belgilaydi?", material_id=mid)
        now = tb.utcnow()
        draft = await tb.create_test(
            session,
            tb.TestDraftData(title="Aralash", question_count=50, starts_at=now, deadline_at=now + tb.OPEN_WINDOW,
                             material_ids=mids, answer_reveal="immediate"),
            admin.id,
        )  # fmt: skip
        for mid in mids:
            await tb.ensure_source(session, draft.id, await session.get(Material, mid))
        # 📦 Maksimal: as many variants as the files allow.
        session.add(BotSetting(key=f"ct_variants:{draft.id}", value=ct.MAX_PACK_TESTS))
        session.add(BotSetting(key=f"ct_pack:{draft.id}", value=True))
        await session.commit()
        return draft.id


@pytest.mark.parametrize(
    ("bank", "sizes"),
    [(37, [37]), (50, [50]), (51, [50, 1]), (150, [50, 50, 50]), (437, [50] * 8 + [37])],
)
async def test_variants_of_50_take_every_unique_question_once(tg, session_maker, bank, sizes):  # noqa: F811
    third = bank // 3
    draft_id = await _pack_with_bank(session_maker, [bank - 2 * third, third, third])
    msg = await tg.bot.send_message(ADMIN, "⏳")
    tg.session.clear()
    await ct.build_and_send(session_maker, tg.bot, draft_id, ADMIN, msg.message_id)
    summary = tg.session.texts()[-1]
    assert f"{len(sizes)} ta variant tayyor" in summary and "hech kimga yuborilmadi" in summary
    # Nothing goes to employees or groups: the admin starts each variant later.
    assert not tg.session.polls()
    assert {int(m.chat_id) for m in tg.session.requests if getattr(m, "chat_id", None) is not None} <= {ADMIN}
    async with session_maker() as session:
        tests = (await session.execute(select(Test).order_by(Test.id))).scalars().all()
        assert [x.question_count for x in tests] == sizes
        assert all(x.status == TestStatus.READY and x.published_at is None for x in tests)
        rows = (await session.execute(select(TestQuestion.test_id, TestQuestion.question_id))).all()
        assert [sum(1 for t_id, _ in rows if t_id == x.id) for x in tests] == sizes
        assert len({q for _, q in rows}) == len(rows) == bank  # no question in two variants, none padded
        # Every question keeps the material title it came from.
        empty = (
            await session.execute(select(func.count(TestQuestion.id)).where(TestQuestion.source_name == ""))
        ).scalar_one()
        assert empty == 0


async def test_questions_are_shared_in_proportion_to_the_materials(tg, session_maker):  # noqa: F811
    assert split_proportional(50, [300, 100, 100]) == [30, 10, 10]
    assert split_proportional(50, [10, 10, 10]) == [10, 10, 10]  # never more than a file has
    assert sum(split_proportional(50, [97, 23, 17])) == 50
    draft_id = await _pack_with_bank(session_maker, [90, 30, 30])
    msg = await tg.bot.send_message(ADMIN, "⏳")
    await ct.build_and_send(session_maker, tg.bot, draft_id, ADMIN, msg.message_id)
    async with session_maker() as session:
        first = (await session.execute(select(Test).order_by(Test.id).limit(1))).scalar_one()
        counts = dict(
            (
                await session.execute(
                    select(TestQuestion.source_name, func.count())
                    .where(TestQuestion.test_id == first.id)
                    .group_by(TestQuestion.source_name)
                )
            ).all()
        )
    assert counts == {"Qonun 1": 30, "Qonun 2": 10, "Qonun 3": 10}


async def test_ready_variants_are_listed_and_started_one_by_one(tg, session_maker):  # noqa: F811
    from app.keyboards.admin import admin_main_kb
    from app.keyboards.callbacks import AdminCB
    from tests.telegram_mock import callback_update

    draft_id = await _pack_with_bank(session_maker, [60, 20, 20])
    msg = await tg.bot.send_message(ADMIN, "⏳")
    await ct.build_and_send(session_maker, tg.bot, draft_id, ADMIN, msg.message_id)
    assert "📚 Tayyor testlar (variantlar)" in [b.text for row in admin_main_kb().inline_keyboard for b in row]
    tg.session.clear()
    await tg(callback_update(ADMIN, AdminCB(s="tst", v="READY").pack()))
    labels = [b.text for row in tg.session.last_markup().inline_keyboard for b in row]
    assert sum("variant" in x for x in labels) == 2
    async with session_maker() as session:
        first, second = (await session.execute(select(Test).order_by(Test.id))).scalars().all()
    tg.session.clear()
    await tg(callback_update(ADMIN, AdminCB(s="tst_v", id=first.id).pack()))
    labels = [b.text for row in tg.session.last_markup().inline_keyboard for b in row]
    assert "▶️ Guruhda boshlash" in labels and "📢 E'lon qilish" in labels
    # Starting the first variant leaves the second one ready for later.
    async with session_maker() as session:
        await tb.publish(session, first.id)
    async with session_maker() as session:
        assert (await session.get(Test, first.id)).status == TestStatus.ACTIVE
        assert (await session.get(Test, second.id)).status == TestStatus.READY


async def test_group_answers_post_nothing_into_the_group(tg, session_maker, monkeypatch):  # noqa: F811
    """Right/wrong and the explanation are on the quiz poll only (💡); no message per answer."""
    personal: list = []

    async def record(*args, **kwargs):
        personal.append(args)
        return True

    monkeypatch.setattr(group_results, "send_personal", record)
    test_id = await _setup(session_maker, for_group=True)
    tg.session.clear()
    await launch_test(tg.bot, session_maker, test_id)
    polls = _group_polls(tg)
    assert polls and all(p.reply_markup is None for p in polls)  # no "Javobim va izoh" button
    assert all(p.question.startswith("📚 Manba: ") for p in polls)
    assert all(p.explanation and "📚" in p.explanation and len(p.explanation.encode("utf-16-le")) // 2 <= 200
               for p in polls)  # fmt: skip
    tg.session.clear()
    for poll in polls:  # wrong answers too: the poll itself shows the correct one
        await tg(poll_answer_update(7901, poll_id_of(poll), [(poll.correct_option_ids[0] + 1) % 4], "Ali"))
    assert tg.session.sent_to(GROUP_CHAT) == [] and personal == []
