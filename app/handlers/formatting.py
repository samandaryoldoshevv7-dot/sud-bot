"""Shared message renderers (all user-visible text goes through app.locales)."""

from __future__ import annotations

from app.locales import t
from app.models import (
    AnswerReveal,
    AttemptStatus,
    Test,
    TestAttempt,
    TestQuestion,
    TestStatus,
    User,
    UserAnswer,
)
from app.schemas.ai import LETTERS
from app.services.attempts import displayed_options
from app.utils.text import esc, pct, truncate
from app.utils.time import fmt_dt, fmt_hours, fmt_span, utcnow

STATUS_EMOJI = {
    TestStatus.DRAFT: "📝",
    TestStatus.READY: "✅",
    TestStatus.ACTIVE: "🟢",
    TestStatus.EXPIRED: "⌛",
    TestStatus.CLOSED: "🔒",
}


def source_line(names: list[str]) -> str:
    return " + ".join(names)


def employee_test_card(test: Test, attempt: TestAttempt | None, sources: list[str] | None = None, now=None) -> str:
    """Test card: name, sources, number of questions, personal time and the employee's state."""
    now = now or utcnow()
    lines = [f"📚 <b>{esc(test.title)}</b>"]
    if sources:
        lines += ["", esc(source_line(sources))]
    lines += [
        "",
        t("emp.card.questions", n=test.question_count),
        t("emp.card.duration", d=fmt_hours(test.duration_seconds / 3600)),
    ]
    if test.description:
        lines += ["", esc(truncate(test.description, 600))]
    lines.append("")
    if attempt is None:
        lines.append(t("emp.state.new"))
        lines.append(t("emp.card.timer_note", d=fmt_hours(test.duration_seconds / 3600)))
    elif attempt.status == AttemptStatus.IN_PROGRESS and now < attempt.deadline_at:
        lines += [
            t("emp.state.progress"),
            t("emp.card.progress", done=attempt.answered_count, total=attempt.total_questions),
            t("emp.card.started", d=fmt_dt(attempt.started_at)),
            t("emp.card.ends", d=fmt_dt(attempt.deadline_at)),
            t("emp.card.left", d=fmt_span((attempt.deadline_at - now).total_seconds())),
        ]
    elif attempt.status == AttemptStatus.COMPLETED:
        lines += [t("emp.state.done"), t("emp.card.score", score=pct(attempt.score_percent))]
    else:
        lines += [t("emp.state.expired"), t("emp.card.score", score=pct(attempt.score_percent))]
    if test.paused:
        lines += ["", t("emp.card.paused")]
    return "\n".join(lines)


def question_text(attempt: TestAttempt, tq: TestQuestion, item: dict, position: int, now=None) -> str:
    """Quiz-style question: [n/total] question, source, options."""
    now = now or utcnow()
    lines = [f"<b>[{position + 1}/{attempt.total_questions}]</b> <b>{esc(tq.question_text)}</b>", ""]
    if tq.source_name:
        lines += [t("quiz.source", s=esc(tq.source_name)), ""]
    for letter, option in displayed_options(tq, item):
        lines.append(f"○ <b>{letter})</b> {esc(option)}")
    left = (attempt.deadline_at - now).total_seconds()
    lines += ["", t("quiz.time_left", d=fmt_span(left))]
    return "\n".join(lines)


def answer_result_text(
    tq: TestQuestion, opts: list[str], position: int, total: int, selected: str, reveal: AnswerReveal
) -> str:
    """The answer window shown instead of the options: verdict, both answers, source (no praise).

    ``opts`` are the ORIGINAL option letters in the order the employee saw them (A, B, C, ...).
    """
    letters = {LETTERS[i]: original for i, original in enumerate(opts)}
    selected_original = letters.get(selected)
    correct_display = next((d for d, o in letters.items() if o == tq.correct_option), "?")
    chosen = f"{selected}) {esc(tq.options.get(selected_original or '', ''))}"
    lines = [f"<b>[{position + 1}/{total}]</b> <b>{esc(tq.question_text)}</b>", ""]
    if reveal != AnswerReveal.IMMEDIATE:
        lines += [t("quiz.accepted"), "", t("quiz.your_answer"), chosen]
        if reveal == AnswerReveal.AFTER_COMPLETION:
            lines += ["", t("quiz.reveal_later")]
        return "\n".join(lines)
    if selected_original == tq.correct_option:
        lines += [t("quiz.correct"), "", t("quiz.your_answer"), chosen]
    else:
        lines += [
            t("quiz.wrong"),
            "",
            t("quiz.your_answer"),
            chosen,
            "",
            t("quiz.correct_answer"),
            f"{correct_display}) {esc(tq.options[tq.correct_option])}",
        ]
    if tq.source_name:
        lines += ["", t("quiz.source", s=esc(tq.source_name))]
    if tq.explanation:
        lines += ["", t("quiz.explanation", text=esc(tq.explanation))]
    return "\n".join(lines)


def poll_verdict_text(tq: TestQuestion, opts: list[str], position: int, total: int, selected: str) -> str:
    """A lasting card under an answered quiz poll (Telegram's own explanation pop-up disappears)."""
    letters = {LETTERS[i]: original for i, original in enumerate(opts)}
    selected_original = letters.get(selected)
    correct_display = next((d for d, o in letters.items() if o == tq.correct_option), "?")
    head = f"<b>[{position + 1}/{total}]</b> "
    lines = []
    if selected_original == tq.correct_option:
        lines.append(head + t("quiz.correct"))
    else:
        lines += [
            head + t("quiz.wrong"),
            t("quiz.your_answer_inline", a=f"{selected}) {esc(tq.options.get(selected_original or '', ''))}"),
            t("quiz.correct_answer_inline", a=f"{correct_display}) {esc(tq.options[tq.correct_option])}"),
        ]
    if tq.source_name:
        lines.append(t("quiz.source", s=esc(tq.source_name)))
    if tq.explanation:
        lines.append(t("quiz.explanation", text=esc(tq.explanation)))
    return "\n".join(lines)


def result_text(test: Test, attempt: TestAttempt, user: User | None = None, sources: list | None = None) -> str:
    """Final result: facts only (no praise)."""
    unanswered = attempt.total_questions - attempt.answered_count
    header = "emp.result.header" if attempt.status == AttemptStatus.COMPLETED else "emp.result.header_expired"
    lines = [t(header), ""]
    if user is not None:
        lines.append(f"👤 {esc(user.display_name)}")
    lines += [
        f"📚 {esc(test.title)}",
        "",
        t("emp.result.questions", n=attempt.total_questions),
        "",
        t("emp.result.correct", c=attempt.correct_count),
        t("emp.result.incorrect", n=attempt.incorrect_count),
    ]
    if unanswered:
        lines.append(t("emp.result.unanswered", n=unanswered))
    lines += [
        "",
        t("emp.result.score", score=pct(attempt.score_percent)),
        "",
        t("emp.result.time", time=fmt_span(attempt.duration_seconds)),
    ]
    if sources and len(sources) > 1:
        lines += ["", t("emp.result.by_source")]
        for row in sources:
            lines += ["", f"<b>{esc(row.name)}</b>", t("emp.result.source_row", total=row.total, correct=row.correct)]
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
                source=esc(tq.source_name or tq.source_reference),
            )
        )
    return blocks


def user_line(user: User) -> str:
    username = f" (@{esc(user.username)})" if user.username else ""
    return f"{esc(user.display_name)}{username}"
