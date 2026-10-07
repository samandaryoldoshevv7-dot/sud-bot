"""Async SQLAlchemy engine / session factory."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.config import Settings, get_settings

_engine: AsyncEngine | None = None
_session_maker: async_sessionmaker[AsyncSession] | None = None


def create_engine(settings: Settings | None = None, url: str | None = None) -> AsyncEngine:
    settings = settings or get_settings()
    return create_async_engine(
        url or settings.async_database_url,
        echo=settings.db_echo,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout,
        pool_pre_ping=True,
        pool_recycle=1800,
        connect_args=settings.database_connect_args if url is None else {},
    )


def create_session_maker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_engine()
    return _engine


def get_session_maker() -> async_sessionmaker[AsyncSession]:
    global _session_maker
    if _session_maker is None:
        _session_maker = create_session_maker(get_engine())
    return _session_maker


def set_engine(engine: AsyncEngine) -> None:
    """Install an externally created engine (used by tests and the entrypoint)."""
    global _engine, _session_maker
    _engine = engine
    _session_maker = create_session_maker(engine)


async def dispose_engine() -> None:
    global _engine, _session_maker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_maker = None
