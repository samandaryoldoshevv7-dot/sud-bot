"""🗑 Barchasini o'chirish in the question bank: deletes what the current filter shows, after a
confirmation; tests already given keep their own copies of the questions."""

from sqlalchemy import func, select

from app.keyboards.callbacks import AdminCB
from app.models import Question, TestQuestion
from app.services import test_builder as tb
from tests.factories import make_employee, make_material_with_text, make_question, make_test
from tests.telegram_mock import callback_update
from tests.test_group_flow import tg  # noqa: F401  (fixture)

ADMIN = 1000


def _buttons(tg) -> list[str]:  # noqa: F811
    return [b.text for row in tg.session.last_markup().inline_keyboard for b in row]


async def test_delete_all_questions_of_one_material_then_the_whole_bank(tg, session_maker):  # noqa: F811
    first = await make_material_with_text(session_maker, title="Konstitutsiya", file_name="k.pdf")
    second = await make_material_with_text(session_maker, title="Mehnat kodeksi", file_name="m.pdf")
    async with session_maker() as session:
        await make_employee(session, ADMIN, "Admin Bosh")
        for i in range(5):
            await make_question(session, text_=f"Konstitutsiya {i}-savol?", material_id=first)
        for i in range(3):
            await make_question(session, text_=f"Mehnat {i}-savol?", material_id=second)
        test = await make_test(session, question_count=2, material_ids=[first])
        await tb.assemble_questions(session_maker, test.id, None)  # a test already uses 2 of them
        test_id = test.id

    # Filter: 📚 Konstitutsiya → the button counts exactly those questions.
    await tg(callback_update(ADMIN, AdminCB(s="qb_mat", id=first).pack()))
    assert "🗑 Barchasini o'chirish (5 ta)" in _buttons(tg)
    tg.session.clear()
    await tg(callback_update(ADMIN, AdminCB(s="qb_delall").pack()))
    assert any("5 ta savol o'chirilsinmi?" in x and "Konstitutsiya" in x for x in tg.session.texts())
    # Someone adds a question meanwhile: the old confirmation does not delete blindly, it asks again.
    async with session_maker() as session:
        await make_question(session, text_="Konstitutsiya yangi savol?", material_id=first)
    tg.session.clear()
    await tg(callback_update(ADMIN, AdminCB(s="qb_delall_ok", v="5").pack()))
    assert any("6 ta savol o'chirilsinmi?" in x for x in tg.session.texts())
    tg.session.clear()
    await tg(callback_update(ADMIN, AdminCB(s="qb_delall_ok", v="6").pack()))
    assert any("6 ta savol o'chirildi" in a for a in tg.session.alerts())
    async with session_maker() as session:
        left = (await session.execute(select(Question.source_material_id))).scalars().all()
        assert sorted(left) == [second] * 3  # only the filtered material's questions are gone
        kept = await session.execute(select(func.count(TestQuestion.id)).where(TestQuestion.test_id == test_id))
        assert kept.scalar_one() == 2  # the test keeps its own copies

    # No filter: the whole bank.
    await tg(callback_update(ADMIN, AdminCB(s="qb_fc").pack()))  # 🧹 Tozalash (clear filters)
    tg.session.clear()
    await tg(callback_update(ADMIN, AdminCB(s="qb").pack()))
    assert "🗑 Barchasini o'chirish (3 ta)" in _buttons(tg)
    await tg(callback_update(ADMIN, AdminCB(s="qb_delall_ok", v="3").pack()))
    async with session_maker() as session:
        assert (await session.execute(select(func.count(Question.id)))).scalar_one() == 0
    tg.session.clear()
    await tg(callback_update(ADMIN, AdminCB(s="qb").pack()))
    assert not any("Barchasini o'chirish" in b for b in _buttons(tg))  # empty bank: no button


async def test_employee_cannot_delete_the_bank(tg, session_maker):  # noqa: F811
    mid = await make_material_with_text(session_maker)
    async with session_maker() as session:
        await make_employee(session, 9901, "Oddiy Xodim")
        await make_question(session, text_="Savol?", material_id=mid)
    await tg(callback_update(9901, AdminCB(s="qb_delall_ok", v="1").pack(), "Xodim"))
    async with session_maker() as session:
        assert (await session.execute(select(func.count(Question.id)))).scalar_one() == 1
