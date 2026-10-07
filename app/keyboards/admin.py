from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup

from app.keyboards.callbacks import AdminCB
from app.keyboards.common import kb
from app.locales import t


def admin_main_kb() -> InlineKeyboardMarkup:
    return kb(
        [(t("menu.employees"), AdminCB(s="emp")), (t("menu.materials"), AdminCB(s="mat"))],
        [(t("menu.news"), AdminCB(s="news")), (t("menu.tests"), AdminCB(s="tst"))],
        [(t("menu.questions"), AdminCB(s="qb")), (t("menu.statistics"), AdminCB(s="st"))],
        [(t("menu.rankings"), AdminCB(s="rk", v="week")), (t("menu.reports"), AdminCB(s="rep"))],
        [(t("menu.groups"), AdminCB(s="grp")), (t("menu.settings"), AdminCB(s="set"))],
    )
