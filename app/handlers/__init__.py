"""Router assembly. Order matters: specific routers first, fallbacks last."""

from __future__ import annotations

from aiogram import Dispatcher

from app.handlers import common, employee, errors, group
from app.handlers.admin import router as admin_router


def setup_routers(dp: Dispatcher) -> None:
    dp.include_router(errors.router)
    dp.include_router(group.router)
    dp.include_router(common.router)
    dp.include_router(admin_router)
    dp.include_router(employee.router)
    dp.include_router(common.fallback_router)
