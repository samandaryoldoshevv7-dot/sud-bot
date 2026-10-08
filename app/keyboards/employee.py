from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

from app.keyboards.callbacks import AnsCB, EmpCB
from app.keyboards.common import kb
from app.locales import t


def employee_reply_kb(is_admin: bool = False) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text=t("emp.btn.my_tests")), KeyboardButton(text=t("emp.btn.my_results"))],
        [KeyboardButton(text=t("emp.btn.help"))],
    ]
    if is_admin:
        rows.append([KeyboardButton(text=t("emp.btn.admin_panel"))])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=True)


def answer_kb(attempt_id: int, position: int, tq, item: dict) -> InlineKeyboardMarkup:
    """One tappable button per variant ("A) Co"); the full texts are in the message itself."""
    from app.schemas.ai import LETTERS
    from app.utils.text import truncate

    return kb(
        *[
            [(f"{LETTERS[i]}) {truncate(tq.options[original], 60)}", AnsCB(at=attempt_id, pos=position, o=LETTERS[i]))]
            for i, original in enumerate(item["opts"])
        ]
    )


def start_test_kb(test_id: int, label_key: str = "emp.btn.start_test") -> InlineKeyboardMarkup:
    return kb([(t(label_key), EmpCB(a="start", id=test_id))])
