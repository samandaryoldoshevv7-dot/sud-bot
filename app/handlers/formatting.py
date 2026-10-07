"""Shared message renderers (all user-visible text goes through app.locales)."""

from __future__ import annotations

from app.locales import t
from app.models import (
    AnswerReveal,
    AttemptStatus,
    DeliveryMode,
    Test,
    TestAttempt,
    TestQuestion,
    TestStatus,
    User,
    UserAnswer,
)
from app.services.attempts import displayed_options
from app.utils.text import esc, pct, progress_bar, truncate
from app.utils.time import fmt_dt, fmt_duration, fmt_hours, utcnow

STATUS_EMOJI = {
    TestStatus.DRAFT: "📝",
    TestStatus.READY: "✅",
    TestStatus.ACTIVE: "🟢",
    TestStatus.EXPIRED: "⌛",
    TestStatus.CLOSED: "🔒",
}


def time_left(test: Test) -> str:
    seconds = (test.deadline_at - utcnow()).total_seconds()
    if seconds <= 0:
        return t("common.expired")
    return fmt_duration(int(seconds) // 60 * 60)


def employee_test_card(test: Test, attempt: TestAttempt | None) -> str:
    hours = max(1, round((test.deadline_at - test.starts_at).total_seconds() / 3600))
    lines = [
        t("emp.test_card.title", title=esc(test.title)),
        "",
        t("emp.test_card.questions", n=test.question_count),
        t("emp.test_card.duration", duration=fmt_hours(hours)),
        t("emp.test_card.deadline", deadline=fmt_dt(test.deadline_at), left=time_left(test)),
        t("emp.test_card.passing", p=test.passing_percent),
    ]
    if test.description:
        lines += ["", esc(test.description)]
    if test.delivery_mode == DeliveryMode.GROUP:
        lines += ["", t("emp.test_card.group_mode")]
    if attempt is not None:
        lines.append("")
        if attempt.status == AttemptStatus.IN_PROGRESS:
            lines.append(t("emp.test_card.in_progress", done=attempt.answered_count, total=attempt.total_questions))
        elif attempt.status == AttemptStatus.COMPLETED:
            lines.append(t("emp.test_card.completed", score=pct(attempt.score_percent)))
        elif attempt.status == AttemptStatus.EXPIRED:
            lines.append(t("emp.test_card.expired_attempt", score=pct(attempt.score_percent)))
    return "\n".join(lines)


def question_text(test: Test, attempt: TestAttempt, tq: TestQuestion, item: dict) -> str:
    position = attempt.current_index + 1
    total = attempt.total_questions
    lines = [
        f"📝 <b>{esc(truncate(test.title, 80))}</b>",
        t("emp.question.header", n=position, total=total) + f"  {progress_bar(attempt.current_index / total * 100)}",
        t("emp.question.deadline", deadline=fmt_dt(attempt.deadline_at)),
        "",
        f"<b>{esc(tq.question_text)}</b>",
        "",
    ]
    for letter, option in displayed_options(tq, item):
        lines.append(f"<b>{letter})</b> {esc(option)}")
    return "\n".join(lines)


def answered_question_text(base: str, selected: str, reveal: AnswerReveal, is_correct: bool | None,
                           correct_display: str | None, tq: TestQuestion | None) -> str:  # fmt: skip
    lines = [base, "", t("emp.question.your_answer", letter=selected)]
    if reveal == AnswerReveal.IMMEDIATE and is_correct is not None and tq is not None:
        if is_correct:
            lines.append(t("emp.question.correct"))
        else:
            lines.append(t("emp.question.wrong", letter=correct_display or "?"))
        if tq.explanation:
            lines.append(t("emp.question.explanation", text=esc(tq.explanation)))
    return "\n".join(lines)


def result_text(test: Test, attempt: TestAttempt) -> str:
    unanswered = attempt.total_questions - attempt.answered_count
    header = "emp.result.header" if attempt.status == AttemptStatus.COMPLETED else "emp.result.header_expired"
    lines = [
        t(header),
        "",
        t("emp.result.test", title=esc(test.title)),
        t("emp.result.correct", c=attempt.correct_count, total=attempt.total_questions),
        t("emp.result.incorrect", n=attempt.incorrect_count),
    ]
    if unanswered:
        lines.append(t("emp.result.unanswered", n=unanswered))
    lines += [
        t("emp.result.score", score=pct(attempt.score_percent)),
        t("emp.result.time", time=fmt_duration(attempt.duration_seconds)),
        "",
        t("emp.result.passed") if attempt.passed else t("emp.result.failed", p=test.passing_percent),
    ]
    return "\n".join(lines)


def corrections_text(items: list[tuple[UserAnswer, TestQuestion]], layout: list[dict]) -> list[str]:
    """Detailed corrections, shown with the letters the employee actually saw."""
    by_tq = {item["tq"]: item for item in layout}
    blocks: list[str] = []
    for answer, tq in items:
        item = by_tq.get(tq.id)
        if item:
            letters = dict(displayed_options(tq, item))
            sel_disp = answer.selected_display
            corr_disp = next(
                (d for d, orig in zip("ABCDE", item["opts"], strict=False) if orig == tq.correct_option), "?"
            )
        else:
            letters = dict(tq.options)
            sel_disp, corr_disp = answer.selected_option, tq.correct_option
        blocks.append(
            t(
                "emp.corrections.item",
                n=answer.position + 1,
                question=esc(tq.question_text),
                selected=f"{sel_disp}) {esc(letters.get(sel_disp, ''))}",
                correct=f"{corr_disp}) {esc(letters.get(corr_disp, ''))}",
                explanation=esc(tq.explanation or "—"),
                source=esc(tq.source_reference),
            )
        )
    return blocks


def user_line(user: User) -> str:
    username = f" (@{esc(user.username)})" if user.username else ""
    return f"{esc(user.display_name)}{username}"
