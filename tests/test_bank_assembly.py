"""A test must be buildable from the question bank alone (no AI), whatever difficulty is chosen."""

from app.models import Difficulty, QuestionStatus, TestDifficulty, TestStatus
from app.services import test_builder as tb
from tests.factories import make_material_with_text, make_question, make_test


async def _bank(session_maker, session, n=20):
    mid = await make_material_with_text(session_maker)
    levels = [Difficulty.EASY, Difficulty.MEDIUM, Difficulty.HARD]
    for i in range(n):
        await make_question(session, text_=f"Bank savoli {i} nima haqida?", material_id=mid,
                            status=QuestionStatus.PENDING, difficulty=levels[i % 3])  # fmt: skip
    return mid


async def test_twenty_pending_bank_questions_make_a_twenty_question_test(session_maker, session):
    await _bank(session_maker, session)
    for difficulty in (TestDifficulty.MIXED, TestDifficulty.MEDIUM, TestDifficulty.HARD):
        test = await make_test(session, question_count=20, difficulty=difficulty)
        report = await tb.assemble_questions(session_maker, test.id, None)
        assert report.missing == 0, (difficulty, report)
        await tb.approve_all(session, test.id, None)
        ready = await tb.mark_ready(session, test.id)
        assert ready.status == TestStatus.READY


async def test_preferred_difficulty_is_used_first(session_maker, session):
    await _bank(session_maker, session)
    test = await make_test(session, question_count=5, difficulty=TestDifficulty.HARD)
    await tb.assemble_questions(session_maker, test.id, None)
    await session.refresh(test, ["questions"])
    assert all(tq.difficulty == Difficulty.HARD for tq in test.questions)


async def test_continue_with_available_questions(session_maker, session):
    await _bank(session_maker, session, n=7)
    test = await make_test(session, question_count=20)
    report = await tb.assemble_questions(session_maker, test.id, None)
    assert report.missing == 13
    test = await tb.shrink_to_available(session, test.id)
    assert test.question_count == 7
    await tb.approve_all(session, test.id, None)
    assert (await tb.mark_ready(session, test.id)).status == TestStatus.READY
