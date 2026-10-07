import random
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.models import (
    AttemptStatus,
    GenerationStatus,
    News,
    ParticipationStatus,
    Question,
    QuestionStatus,
    SourceKind,
    Test,
    TestAttempt,
    TestQuestion,
    TestStatus,
    UserAnswer,
)
from app.rag.retriever import Retriever
from app.services import attempts as attempt_service
from app.services import test_builder as tb
from app.services.attempts import AnswerOutcome, StartError, display_to_original, original_to_display
from app.services.question_bank import apply_edit
from app.services.question_generation import QuestionGenerator
from app.services.test_builder import TestStateError, make_snapshot_options
from app.statistics.employee import employee_stats, wrong_answers
from app.statistics.participation import participation
from app.statistics.rankings import ranking
from app.utils.time import utcnow
from tests.factories import (
    HashEmbeddings,
    ScriptedLLM,
    make_employee,
    make_material_with_text,
    make_question,
    make_test,
)


async def _ready_active_test(session_maker, session, n_questions=4, **kw):
    mid = await make_material_with_text(session_maker)
    for i in range(n_questions + 2):
        await make_question(session, text_=f"Bankdagi savol raqam {i} haqida nima deyilgan?", material_id=mid,
                            correct="ABCD"[i % 4])  # fmt: skip
    test = await make_test(session, question_count=n_questions, **kw)
    report = await tb.assemble_questions(session_maker, test.id, None)
    assert report.missing == 0 and report.added_from_bank == n_questions
    await session.refresh(test)
    await tb.mark_ready(session, test.id)
    await tb.publish(session, test.id)
    await session.refresh(test)
    return test


def test_snapshot_option_trimming_keeps_correct_answer():
    rng = random.Random(1)
    options = {"A": "a", "B": "b", "C": "c", "D": "d", "E": "e"}
    for _ in range(50):
        trimmed, correct = make_snapshot_options(options, "D", 3, rng)
        assert list(trimmed) == ["A", "B", "C"]
        assert trimmed[correct] == "d"
    same, correct = make_snapshot_options(options, "B", 5, rng)
    assert same == options and correct == "B"


def test_display_mapping_roundtrip():
    item = {"tq": 1, "opts": ["C", "A", "D", "B"]}
    assert display_to_original(item, "A") == "C"
    assert display_to_original(item, "D") == "B"
    assert display_to_original(item, "E") is None
    assert original_to_display(item, "B") == "D"
    for letter in "ABCD":
        assert display_to_original(item, original_to_display(item, letter)) == letter


async def test_test_creation_assembly_and_lifecycle(session_maker, session):
    test = await _ready_active_test(session_maker, session)
    assert test.status == TestStatus.ACTIVE and test.activated_at is not None
    tqs = (await session.execute(select(TestQuestion).where(TestQuestion.test_id == test.id))).scalars().all()
    assert sorted(tq.position for tq in tqs) == [1, 2, 3, 4]
    assert len({tq.question_id for tq in tqs}) == 4  # no duplicates inside one test
    used = (await session.execute(select(func.sum(Question.times_used)))).scalar_one()
    assert used == 4
    with pytest.raises(TestStateError):
        await tb.publish(session, test.id + 999)


async def test_draft_tests_are_not_accessible(session_maker, session):
    emp = await make_employee(session, 50)
    test = await make_test(session, question_count=2)
    result = await attempt_service.start_attempt(session, emp, test.id)
    assert result.error == StartError.NOT_FOUND


async def test_review_reject_renumbers_and_requires_approval(session_maker, session):
    mid = await make_material_with_text(session_maker)
    for i in range(3):
        await make_question(session, text_=f"Tasdiqlangan savol {i} qaysi?", material_id=mid)
    pending = await make_question(
        session, text_="Kutilayotgan savol nima?", material_id=mid, status=QuestionStatus.PENDING
    )
    test = await make_test(session, question_count=3)
    await tb.assemble_questions(session_maker, test.id, None)
    # Inject the pending AI question as a 4th snapshot to emulate generated content.
    t_obj = await session.get(Test, test.id)
    session.add(tb.snapshot_question(t_obj, pending, 4, random.Random(0)))
    t_obj.question_count = 4
    await session.commit()
    with pytest.raises(TestStateError) as exc:
        await tb.mark_ready(session, test.id)
    assert exc.value.code == "not_all_approved"

    first = await tb.get_test_question(session, test.id, 1)
    await tb.reject_test_question(session, first.id, None)
    positions = (
        (await session.execute(select(TestQuestion.position).where(TestQuestion.test_id == test.id))).scalars().all()
    )
    assert sorted(positions) == [1, 2, 3]
    with pytest.raises(TestStateError) as exc:
        await tb.mark_ready(session, test.id)
    assert exc.value.code == "question_count_mismatch"
    # Refill from the bank (rejected question must not come back), approve all, ready.
    await make_question(session, text_="Yana bir tasdiqlangan savol qaysi?", material_id=mid)
    report = await tb.assemble_questions(session_maker, test.id, None)
    assert report.missing == 0
    await tb.approve_all(session, test.id, None)
    await session.refresh(pending)
    assert pending.status == QuestionStatus.APPROVED
    ready = await tb.mark_ready(session, test.id)
    assert ready.status == TestStatus.READY


async def test_news_percentage_distribution(session_maker, session):
    mid = await make_material_with_text(session_maker)
    news = News(title="Yangi qaror", body="Oliy sud plenumi yangi qaror qabul qildi. " * 5, news_date=utcnow().date())
    session.add(news)
    await session.commit()
    from app.services.materials import index_news

    await index_news(session, news, HashEmbeddings())
    for i in range(10):
        await make_question(session, text_=f"Material savoli {i} qanday?", material_id=mid)
    for i in range(5):
        await make_question(session, text_=f"Yangilik savoli {i} qanday?", kind=SourceKind.NEWS, news_id=news.id)
    test = await make_test(session, question_count=10, news_percent=30)
    report = await tb.assemble_questions(session_maker, test.id, None)
    assert report.missing == 0
    assert report.news_questions == 3 and report.material_questions == 7


async def test_ai_fills_shortfall_from_sources(session_maker, session):
    mid = await make_material_with_text(session_maker)
    await make_question(session, text_="Bankdagi yagona savol nima haqida?", material_id=mid)
    test = await make_test(session, question_count=3)
    llm = ScriptedLLM()
    report = await tb.assemble_questions(
        session_maker, test.id, lambda: QuestionGenerator(llm, Retriever(HashEmbeddings()))
    )
    assert report.added_from_bank == 1 and report.generated == 2 and report.missing == 0
    total, approved = await tb.question_counts(session, test.id)
    assert (total, approved) == (3, 1)  # AI questions wait for admin approval
    await session.refresh(test)
    assert test.generation_status == GenerationStatus.DONE


async def test_attempt_randomization_scoring_and_idempotency(session_maker, session):
    test = await _ready_active_test(session_maker, session, n_questions=4)
    emp = await make_employee(session, 60)
    rng = random.Random(42)
    started = await attempt_service.start_attempt(session, emp, test.id, chat_id=60, rng=rng)
    assert started.error is None and not started.resumed
    again = await attempt_service.start_attempt(session, emp, test.id, chat_id=60)
    assert again.resumed and again.attempt.id == started.attempt.id  # duplicate click → same attempt
    attempt = started.attempt
    assert len(attempt.layout) == 4

    # Answer: correct for the first 3 questions, wrong for the last, using DISPLAY letters.
    for pos in range(4):
        await session.refresh(attempt)
        item = attempt.layout[pos]
        tq = await session.get(TestQuestion, item["tq"])
        correct_display = original_to_display(item, tq.correct_option)
        letter = correct_display if pos < 3 else next(x for x in "ABCD" if x != correct_display)
        res = await attempt_service.submit_answer(session, emp, attempt.id, pos, letter)
        assert res.outcome == AnswerOutcome.ACCEPTED
        assert res.is_correct is (pos < 3)
        assert res.correct_display == correct_display
        dup = await attempt_service.submit_answer(session, emp, attempt.id, pos, letter)
        assert dup.outcome in (AnswerOutcome.DUPLICATE, AnswerOutcome.NOT_IN_PROGRESS)

    await session.refresh(attempt)
    assert attempt.status == AttemptStatus.COMPLETED
    assert (attempt.correct_count, attempt.incorrect_count, attempt.answered_count) == (3, 1, 4)
    assert attempt.score_percent == Decimal("75.00") and attempt.passed is True
    assert attempt.duration_seconds is not None and attempt.completed_at is not None
    assert (await session.execute(select(func.count(UserAnswer.id)))).scalar_one() == 4

    # Retakes are disabled by default.
    retry = await attempt_service.start_attempt(session, emp, test.id)
    assert retry.error == StartError.ALREADY_COMPLETED

    # Wrong-answer tracking stores employee answer vs correct answer with source info.
    rows, total = await wrong_answers(session, emp.id, 0)
    assert total == 1
    row = rows[0]
    assert row.answer.selected_option != row.answer.correct_option
    assert row.question.source_reference and row.question.topic_name is not None


async def test_other_user_cannot_answer_foreign_attempt(session_maker, session):
    test = await _ready_active_test(session_maker, session, n_questions=2)
    owner = await make_employee(session, 61)
    intruder = await make_employee(session, 62, "Begona Odam")
    attempt = (await attempt_service.start_attempt(session, owner, test.id)).attempt
    res = await attempt_service.submit_answer(session, intruder, attempt.id, 0, "A")
    assert res.outcome == AnswerOutcome.INVALID


async def test_deadline_expiration_and_participation(session_maker, session):
    test = await _ready_active_test(session_maker, session, n_questions=2)
    done = await make_employee(session, 70, "Bajardi Birinchi")
    started = await make_employee(session, 71, "Boshladi Ikkinchi")
    await make_employee(session, 72, "Qatnashmadi Uchinchi")
    await make_employee(session, 73, "Faolmas Tortinchi", active=False)

    a1 = (await attempt_service.start_attempt(session, done, test.id)).attempt
    for pos in range(2):
        await session.refresh(a1)
        await attempt_service.submit_answer(session, done, a1.id, pos, "A")
    a2 = (await attempt_service.start_attempt(session, started, test.id)).attempt
    await attempt_service.submit_answer(session, started, a2.id, 0, "A")

    summary = await participation(session, test, utcnow())
    assert summary.total == 3
    assert summary.count(ParticipationStatus.COMPLETED) == 1
    assert summary.count(ParticipationStatus.IN_PROGRESS) == 1
    assert summary.count(ParticipationStatus.NOT_STARTED) == 1

    # Time passes beyond the deadline (server-side clock).
    later = test.deadline_at + timedelta(minutes=1)
    expired_ids = await tb.expire_due(session, later)
    assert expired_ids == [test.id]
    await session.refresh(test)
    await session.refresh(a2)
    assert test.status == TestStatus.EXPIRED
    assert a2.status == AttemptStatus.EXPIRED and a2.answered_count == 1 and a2.total_questions == 2
    late = await attempt_service.submit_answer(session, started, a2.id, 1, "A", now=later)
    assert late.outcome == AnswerOutcome.NOT_IN_PROGRESS

    summary = await participation(session, test, later)
    assert summary.count(ParticipationStatus.COMPLETED) == 1
    assert summary.count(ParticipationStatus.EXPIRED) == 1
    assert summary.count(ParticipationStatus.NOT_STARTED) == 1
    assert summary.participated == 2 and summary.started_not_finished == 1
    names = [r.user.full_name for r in summary.by_status(ParticipationStatus.NOT_STARTED)]
    assert names == ["Qatnashmadi Uchinchi"]
    late_start = await attempt_service.start_attempt(
        session, await make_employee(session, 74, "Kech Qoldi"), test.id, now=later
    )
    assert late_start.error == StartError.DEADLINE_PASSED


async def test_answer_after_deadline_expires_attempt(session_maker, session):
    test = await _ready_active_test(session_maker, session, n_questions=2)
    emp = await make_employee(session, 80)
    attempt = (await attempt_service.start_attempt(session, emp, test.id)).attempt
    res = await attempt_service.submit_answer(
        session, emp, attempt.id, 0, "A", now=test.deadline_at + timedelta(seconds=1)
    )
    assert res.outcome == AnswerOutcome.EXPIRED
    await session.refresh(attempt)
    assert attempt.status == AttemptStatus.EXPIRED and attempt.answered_count == 0


async def test_finished_attempt_cannot_be_retaken(session_maker, session):
    test = await _ready_active_test(session_maker, session, n_questions=2)
    emp = await make_employee(session, 81)
    attempt = (await attempt_service.start_attempt(session, emp, test.id)).attempt
    for pos in range(2):
        await session.refresh(attempt)
        await attempt_service.submit_answer(session, emp, attempt.id, pos, "B")
    assert (await attempt_service.start_attempt(session, emp, test.id)).error == StartError.ALREADY_COMPLETED


async def test_snapshot_is_immutable_after_bank_edit(session_maker, session):
    test = await _ready_active_test(session_maker, session, n_questions=2)
    tq = await tb.get_test_question(session, test.id, 1)
    original_text, original_correct = tq.question_text, tq.correct_option
    question = await session.get(Question, tq.question_id)
    result = await apply_edit(session, question.id, "question", "Butunlay yangi tahrirlangan savol matni?", None)
    assert result.ok, result.errors
    await session.refresh(tq)
    assert tq.question_text == original_text and tq.correct_option == original_correct
    await session.refresh(question)
    assert question.version == 2 and question.status == QuestionStatus.PENDING


async def test_employee_statistics_topics_and_ranking(session_maker, session):
    test = await _ready_active_test(session_maker, session, n_questions=4)
    good = await make_employee(session, 90, "Alo Xodim")
    weak = await make_employee(session, 91, "Zaif Xodim")
    for emp, right in ((good, 4), (weak, 1)):
        attempt = (await attempt_service.start_attempt(session, emp, test.id)).attempt
        for pos in range(4):
            await session.refresh(attempt)
            item = attempt.layout[pos]
            tq = await session.get(TestQuestion, item["tq"])
            correct = original_to_display(item, tq.correct_option)
            letter = correct if pos < right else next(x for x in "ABCD" if x != correct)
            await attempt_service.submit_answer(session, emp, attempt.id, pos, letter)
    stats = await employee_stats(session, weak, min_topic_answers=1)
    assert stats.completed_tests == 1 and stats.correct == 1 and stats.incorrect == 3
    assert stats.average_score == 25.0 and stats.best_score == 25.0
    rows = await ranking(session, "all", min_attempts=1)
    assert [r.user.id for r in rows] == [good.id, weak.id]
    assert rows[0].percent == 100.0
    assert await ranking(session, "all", min_attempts=2) == []  # insufficient data is not ranked
    day = await ranking(session, "day", min_attempts=1)
    assert len(day) == 2


async def test_scheduler_tick_activates_and_expires(session_maker, session):
    from app.services.scheduler import run_tick

    mid = await make_material_with_text(session_maker)
    for i in range(2):
        await make_question(session, text_=f"Rejalashtirilgan savol {i} nima?", material_id=mid)
    now = utcnow()
    test = await make_test(
        session, question_count=2, starts_at=now + timedelta(hours=1), deadline_at=now + timedelta(hours=3)
    )
    await tb.assemble_questions(session_maker, test.id, None)
    await tb.mark_ready(session, test.id)
    await tb.publish(session, test.id)
    await session.refresh(test)
    assert test.status == TestStatus.READY and test.published_at is not None
    report = await run_tick(None, session_maker)
    assert report["activated"] == []
    # Move the start into the past → scheduler activates; deadline into the past → expires.
    test.starts_at = now - timedelta(minutes=5)
    await session.commit()
    report = await run_tick(None, session_maker)
    assert report["activated"] == [test.id]
    test.deadline_at = now - timedelta(minutes=1)
    await session.commit()
    report = await run_tick(None, session_maker)
    assert report["expired_tests"] == [test.id]
    assert (await session.execute(select(func.count(TestAttempt.id)))).scalar_one() == 0
