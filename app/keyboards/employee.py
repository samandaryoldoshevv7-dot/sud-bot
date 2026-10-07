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


def answer_kb(attempt_id: int, position: int, letters: list[str]) -> InlineKeyboardMarkup:
    return kb([(f"  {letter}  ", AnsCB(at=attempt_id, pos=position, o=letter)) for letter in letters])


def start_test_kb(test_id: int, label_key: str = "emp.btn.start_test") -> InlineKeyboardMarkup:
    return kb([(t(label_key), EmpCB(a="start", id=test_id))])
