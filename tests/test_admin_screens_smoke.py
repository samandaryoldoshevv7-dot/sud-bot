"""Every admin screen opens without "Kutilmagan xatolik" on realistic data."""

from sqlalchemy import select

from app.keyboards.callbacks import AdminCB
from app.models import Group, Material, MaterialStatus, News, Question, Test, TestAttempt, User
from app.services import attempts as attempt_service
from app.services import groups as group_service
from app.services import materials as material_service
from app.services import test_builder as tb
from tests.factories import make_employee, make_material_with_text, make_question, make_test
from tests.telegram_mock import callback_update, message_update
from tests.test_group_flow import tg  # noqa: F401  (fixture)

ADMIN = 1000


async def _data(session_maker):
    mid = await make_material_with_text(session_maker, title="Ma'muriy kodeks", file_name="kodeks.pdf")
    async with session_maker() as session:
        admin = await make_employee(session, ADMIN, "Admin Bosh")
        emp = await make_employee(session, 9601, "Ishchi Xodim")
        await make_employee(session, 9602, "Kutayotgan Xodim", active=False)
        for i in range(6):
            await make_question(session, text_=f"Ekran savoli {i} nimani anglatadi?", material_id=mid)
        group, _ = await group_service.register_group(session, -100777, "Sud guruhi", admin)
        await group_service.mark_membership(session, group, emp, True)
        processing = await material_service.create_material(
            session, title="O'qilayotgan", file_type=Material.file_type.type.enum_class("pdf"), uploaded_by_id=None
        )
        processing.status = MaterialStatus.PROCESSING
        await session.commit()
        active = await make_test(session, title="Faol test", question_count=3, material_ids=[mid])
        await tb.assemble_questions(session_maker, active.id, None)
        await tb.mark_ready(session, active.id)
        await tb.publish(session, active.id)
        attempt = (await attempt_service.start_attempt(session, emp, active.id)).attempt
        await attempt_service.submit_answer(session, emp, attempt.id, 0, "A")
        draft = await make_test(session, title="Qoralama", question_count=3)
        await tb.assemble_questions(session_maker, draft.id, None)
        ended = await make_test(session, title="Tugagan", question_count=2)
        await tb.assemble_questions(session_maker, ended.id, None)
        await tb.mark_ready(session, ended.id)
        await tb.publish(session, ended.id)
        await tb.close_test(session, ended.id)
    return mid


async def test_every_admin_screen_opens(tg, session_maker):  # noqa: F811
    await _data(session_maker)
    async with session_maker() as session:
        users = [u.id for u in (await session.execute(select(User))).scalars()]
        materials = [m.id for m in (await session.execute(select(Material))).scalars()]
        tests = [x.id for x in (await session.execute(select(Test))).scalars()]
        attempts = [a.id for a in (await session.execute(select(TestAttempt))).scalars()]
        questions = [q.id for q in (await session.execute(select(Question))).scalars()][:2]
        groups = [g.id for g in (await session.execute(select(Group))).scalars()]
        news = [n.id for n in (await session.execute(select(News))).scalars()]
    calls = [AdminCB(s=s) for s in ("menu", "emp", "mat", "news", "tst", "qb", "st", "st_hard", "st_mist", "st_top",
                                    "rep", "grp", "set", "res", "ct", "tst_new")]  # fmt: skip
    calls += [AdminCB(s="emp_l", v=v) for v in ("all", "active", "pending", "inactive", "blocked")]
    calls += [AdminCB(s="rk", v=v) for v in ("day", "week", "month", "all")]
    calls += [AdminCB(s="tst", v=v) for v in ("DRAFT", "ACTIVE", "EXPIRED")]
    for uid in users:
        calls += [AdminCB(s="emp_v", id=uid, v="all"), AdminCB(s="emp_wr", id=uid)]
    for mid in materials:
        calls += [AdminCB(s=s, id=mid) for s in ("mat_v", "mat_gen", "mat_qt", "qb_mat", "mat_re")]
        calls.append(AdminCB(s="mat_qt_n", id=mid, v="10"))
        calls.append(AdminCB(s="mat_qt_go", id=mid, v="3-0"))
    for tid in tests:
        calls += [AdminCB(s=s, id=tid) for s in ("tst_v", "tst_qs", "tst_dur", "tst_grp", "res_t", "tst_rep",
                                                 "tst_csv", "tst_close", "tst_del", "tst_reopen")]  # fmt: skip
        calls += [AdminCB(s="tst_rv", id=tid, p=1), AdminCB(s="tst_ans", id=tid, p=1)]
        calls += [AdminCB(s="tst_part", id=tid, v=v) for v in ("all", "done", "prog", "exp", "none", "canc")]
    for aid in attempts:
        calls += [AdminCB(s=s, id=aid) for s in ("res_a", "tst_pu", "res_xt", "res_rm")]
    for qid in questions:
        calls += [AdminCB(s="qb_v", id=qid), AdminCB(s="qb_ed", id=qid)]
        calls += [AdminCB(s="qb_edf", id=qid, v=v) for v in ("correct", "difficulty")]
        calls += [AdminCB(s="qb_set", id=qid, v=v) for v in ("correct-A", "difficulty-hard")]
    for gid in groups:
        calls += [AdminCB(s="grp_v", id=gid), AdminCB(s="grp_mem", id=gid)]
    for nid in news:
        calls.append(AdminCB(s="news_v", id=nid))
    failures = []
    for cb in calls:
        tg.session.clear()
        await tg(callback_update(ADMIN, cb.pack()))
        out = tg.session.texts() + tg.session.alerts()
        if any("Kutilmagan xatolik" in x for x in out):
            failures.append(cb.pack())
    tg.session.clear()
    await tg(message_update(ADMIN, "👨‍💼 Admin panel"))
    assert not any("Kutilmagan xatolik" in x for x in tg.session.texts())
    assert not failures, failures
