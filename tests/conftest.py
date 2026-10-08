"""Test configuration.

Database tests run against a real PostgreSQL (pgvector image recommended):
    TEST_DATABASE_URL=postgresql://court:devpass@localhost:5433/court_test

The test database is dropped and re-created, then migrated with Alembic (so the migration
itself is exercised). Tests needing the database are skipped if it is unreachable.
"""

from __future__ import annotations

import asyncio
import os
from urllib.parse import urlsplit, urlunsplit

import pytest

TEST_DB_URL = os.environ.get("TEST_DATABASE_URL", "postgresql://court:devpass@127.0.0.1:5433/court_test")
os.environ["DATABASE_URL"] = TEST_DB_URL
os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN-not-real")
os.environ["ADMIN_TELEGRAM_IDS"] = "1000,1001"
os.environ["EMBEDDING_PROVIDER"] = "none"
os.environ["TIMEZONE"] = "Asia/Tashkent"
os.environ["GROQ_API_KEY"] = ""
os.environ["LOG_FORMAT"] = "text"

import pytest_asyncio  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.config.settings import normalize_database_url  # noqa: E402

get_settings.cache_clear()


def _admin_url(url: str) -> tuple[str, str]:
    parts = urlsplit(url)
    db_name = parts.path.lstrip("/")
    return urlunsplit((parts.scheme, parts.netloc, "/postgres", parts.query, parts.fragment)), db_name


async def _recreate_database(url: str) -> None:
    admin_url, db_name = _admin_url(url)
    engine = create_async_engine(normalize_database_url(admin_url)[0], isolation_level="AUTOCOMMIT")
    async with engine.connect() as conn:
        await conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)'))
        await conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    await engine.dispose()


def _run_migrations(url: str) -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config(os.path.join(os.path.dirname(os.path.dirname(__file__)), "alembic.ini"))
    cfg.attributes["database_url"] = url
    command.upgrade(cfg, "head")
    # alembic's logging config disables every existing logger; re-enable them at INFO so each log
    # call of the bot really runs in tests (a bad ``extra`` key once crashed test building in prod).
    import logging

    for logger in [logging.getLogger(), *logging.Logger.manager.loggerDict.values()]:
        if isinstance(logger, logging.Logger):
            logger.disabled = False
    logging.getLogger("app").setLevel(logging.INFO)


@pytest.fixture(scope="session")
def database_url() -> str:
    try:
        asyncio.run(_recreate_database(TEST_DB_URL))
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"PostgreSQL not available for tests: {exc}")
    _run_migrations(TEST_DB_URL)
    return TEST_DB_URL


@pytest_asyncio.fixture(scope="session")
async def engine(database_url):
    from app.database.session import set_engine
    from app.rag.vector_store import reset_backend_cache

    eng = create_async_engine(normalize_database_url(database_url)[0], pool_size=5, max_overflow=5)
    set_engine(eng)
    reset_backend_cache()
    yield eng
    await eng.dispose()


TABLES = [
    "user_answers", "group_question_messages", "group_test_posts", "question_options", "test_attempts", "test_questions", "test_materials", "tests", "questions", "topics",
    "source_chunks", "news", "materials", "group_members", "groups", "users", "bot_settings",
]  # fmt: skip


@pytest_asyncio.fixture
async def session_maker(engine):
    from app.database.session import create_session_maker

    maker = create_session_maker(engine)
    yield maker
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE"))


@pytest_asyncio.fixture
async def session(session_maker):
    async with session_maker() as s:
        yield s


@pytest.fixture(autouse=True)
def _fresh_ai_cache():
    """The AI result cache is process-wide; tests with scripted answers must not share it."""
    from app.ai.structured import clear_cache

    clear_cache()
    yield
    clear_cache()
