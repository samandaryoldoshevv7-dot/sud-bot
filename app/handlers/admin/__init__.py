"""Admin routers. Every admin router is guarded by the server-side ``IsAdmin`` filter."""

from __future__ import annotations

from aiogram import F, Router

from app.filters import IsAdmin
from app.handlers.admin import (
    create_test,
    employees,
    groups,
    materials,
    menu,
    news,
    questions,
    results,
    settings,
    stats,
    test_wizard,
    tests,
)

router = Router(name="admin")
router.message.filter(F.chat.type == "private", IsAdmin())
router.callback_query.filter(IsAdmin())

for module in (
    menu,
    employees,
    materials,
    news,
    create_test,
    test_wizard,
    tests,
    results,
    questions,
    stats,
    groups,
    settings,
):
    router.include_router(module.router)
