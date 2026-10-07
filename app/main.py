"""Application entrypoint: ``python -m app.main``.

Runs the Telegram bot (long polling by default, webhook optional), the background scheduler
and a tiny HTTP server exposing ``/health`` for Railway health checks — all in one process.
"""

from __future__ import annotations

import asyncio
import logging
import signal

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats, BotCommandScopeChat
from aiogram.webhook.aiohttp_server import SimpleRequestHandler
from aiohttp import web
from sqlalchemy import text

from app import __version__
from app.config import Settings, get_settings
from app.database.session import dispose_engine, get_session_maker
from app.handlers import setup_routers
from app.locales import t
from app.middlewares import DbSessionMiddleware, UserMiddleware
from app.rag.embeddings import FastEmbedProvider, get_embedding_provider
from app.rag.vector_store import detect_backend
from app.services import background
from app.services.scheduler import group_refresh_loop, scheduler_loop
from app.services.test_builder import reset_stuck_generation
from app.services.users import sync_admin_roles
from app.utils.logging import setup_logging

logger = logging.getLogger("app")


def create_bot(settings: Settings) -> Bot:
    session = None
    if settings.telegram_api_base:
        session = AiohttpSession(api=TelegramAPIServer.from_base(settings.telegram_api_base.rstrip("/")))
    return Bot(
        token=settings.bot_token.get_secret_value(),
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
    )


def create_dispatcher() -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    session_maker = get_session_maker()
    dp.update.outer_middleware(DbSessionMiddleware(session_maker))
    dp.update.outer_middleware(UserMiddleware())
    setup_routers(dp)
    return dp


async def set_commands(bot: Bot, settings: Settings) -> None:
    common = [
        BotCommand(command="start", description=t("cmd.start")),
        BotCommand(command="help", description=t("cmd.help")),
        BotCommand(command="cancel", description=t("cmd.cancel")),
    ]
    await bot.set_my_commands(common, scope=BotCommandScopeAllPrivateChats())
    for admin_id in settings.admin_ids:
        try:
            await bot.set_my_commands(
                common + [BotCommand(command="admin", description=t("cmd.admin"))],
                scope=BotCommandScopeChat(chat_id=admin_id),
            )
        except Exception as exc:  # admin has not started the bot yet
            logger.info("Could not set admin commands", extra={"admin_id": admin_id, "error": str(exc)[:100]})


async def startup_checks(settings: Settings) -> None:
    session_maker = get_session_maker()
    async with session_maker() as session:
        await session.execute(text("SELECT 1"))
        backend = await detect_backend(session, refresh=True)
        await sync_admin_roles(session)
        stuck = await reset_stuck_generation(session)
    logger.info(
        "Database ready",
        extra={"vector_backend": backend, "reset_generation_jobs": stuck},
    )
    if backend == "none":
        logger.error("source_chunks.embedding column missing - run `alembic upgrade head`")
    if not settings.admin_ids:
        logger.warning("ADMIN_TELEGRAM_IDS is empty: nobody can use the admin panel")
    if not settings.ai_enabled:
        logger.warning("GROQ_API_KEY is not set: AI question generation is disabled")
    provider = get_embedding_provider(settings)
    if isinstance(provider, FastEmbedProvider):
        ok = await provider.warmup()
        logger.info("Embeddings", extra={"provider": provider.name, "model": provider.model_name, "available": ok})


def build_web_app(bot: Bot, dp: Dispatcher, settings: Settings) -> web.Application:
    app = web.Application()

    async def health(_: web.Request) -> web.Response:
        try:
            async with get_session_maker()() as session:
                await session.execute(text("SELECT 1"))
        except Exception:
            return web.json_response({"status": "error", "db": False}, status=503)
        return web.json_response(
            {"status": "ok", "db": True, "version": __version__, "background_tasks": background.running_count()}
        )

    app.router.add_get("/health", health)
    app.router.add_get("/", health)
    if settings.bot_mode == "webhook":
        secret = settings.webhook_secret.get_secret_value() if settings.webhook_secret else None
        SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=secret).register(app, path=settings.webhook_path)
    return app


async def run() -> None:
    settings = get_settings()
    setup_logging(settings.log_level, settings.log_format, settings.secret_values())
    logger.info("Starting court training bot", extra={"version": __version__, "mode": settings.bot_mode})

    await startup_checks(settings)
    bot = create_bot(settings)
    dp = create_dispatcher()
    me = await bot.get_me()
    logger.info("Bot authorised", extra={"username": me.username, "bot_id": me.id})
    await set_commands(bot, settings)

    web_app = build_web_app(bot, dp, settings)
    runner = web.AppRunner(web_app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=settings.port)
    await site.start()
    logger.info("HTTP server listening", extra={"port": settings.port})

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover - Windows
            pass

    scheduler_task = asyncio.create_task(scheduler_loop(bot, get_session_maker(), stop), name="scheduler")
    refresh_task = asyncio.create_task(group_refresh_loop(bot, get_session_maker(), stop), name="group-refresh")
    allowed = dp.resolve_used_update_types()
    if "chat_member" not in allowed:
        allowed.append("chat_member")

    try:
        if settings.bot_mode == "webhook":
            url = settings.webhook_base_url.rstrip("/") + settings.webhook_path
            await bot.set_webhook(
                url,
                secret_token=settings.webhook_secret.get_secret_value() if settings.webhook_secret else None,
                allowed_updates=allowed,
                drop_pending_updates=False,
            )
            logger.info("Webhook set", extra={"path": settings.webhook_path})
            await stop.wait()
        else:
            await bot.delete_webhook(drop_pending_updates=False)
            polling = asyncio.create_task(
                dp.start_polling(bot, allowed_updates=allowed, handle_signals=False, close_bot_session=False),
                name="polling",
            )
            stop_wait = asyncio.create_task(stop.wait())
            done, _ = await asyncio.wait({polling, stop_wait}, return_when=asyncio.FIRST_COMPLETED)
            if polling in done and polling.exception():
                raise polling.exception()  # type: ignore[misc]
            if not polling.done():
                try:
                    await dp.stop_polling()
                except RuntimeError:
                    pass
            await asyncio.gather(polling, return_exceptions=True)
    finally:
        logger.info("Shutting down")
        stop.set()
        await asyncio.gather(scheduler_task, refresh_task, return_exceptions=True)
        await background.shutdown()
        await runner.cleanup()
        await bot.session.close()
        await dispose_engine()
        logger.info("Shutdown complete")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
