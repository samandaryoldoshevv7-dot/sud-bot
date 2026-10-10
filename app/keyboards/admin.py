from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup

from app.keyboards.callbacks import AdminCB
from app.keyboards.common import kb
from app.locales import t


def admin_main_kb(in_group: bool = False) -> InlineKeyboardMarkup:
    """``in_group``: the test mode chosen by the admin (✅ marks it)."""
    group_label = ("✅ " if in_group else "") + t("menu.mode_group")
    private_label = ("" if in_group else "✅ ") + t("menu.mode_private")
    return kb(
        [(t("menu.create_test"), AdminCB(s="ct")), (t("menu.results"), AdminCB(s="res"))],
        [(t("menu.employees"), AdminCB(s="emp")), (t("menu.materials"), AdminCB(s="mat"))],
        [(t("menu.news"), AdminCB(s="news")), (t("menu.tests"), AdminCB(s="tst"))],
        [(t("menu.questions"), AdminCB(s="qb")), (t("menu.statistics"), AdminCB(s="st"))],
        [(t("menu.rankings"), AdminCB(s="rk", v="week")), (t("menu.reports"), AdminCB(s="rep"))],
        [(t("menu.groups"), AdminCB(s="grp")), (t("menu.settings"), AdminCB(s="set"))],
        [(group_label, AdminCB(s="mode", v="group")), (private_label, AdminCB(s="mode", v="private"))],
    )
