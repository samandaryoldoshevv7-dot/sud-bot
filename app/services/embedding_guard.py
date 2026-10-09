"""Keep the bot alive when the embedding model does not fit into the server's memory.

Indexing a file with the local ONNX model can use more RAM than a small server has; the OS then
kills the whole process, whatever the file size. A marker row is written before the model works
and removed after it. If the next start finds the marker, the previous process died while
embedding: semantic search is switched off (materials stay searchable via full-text search) and
the admins are told, instead of every file crashing the bot again.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import BotSetting
from app.rag.embeddings import FastEmbedProvider, NullEmbeddingProvider, get_embedding_provider, set_embedding_provider

logger = logging.getLogger(__name__)

RUNNING_KEY = "embedding_running"
DISABLED_KEY = "embeddings_disabled"


async def _set(session_maker: async_sessionmaker[AsyncSession], key: str, value: object) -> None:
    async with session_maker() as session:
        stmt = insert(BotSetting).values(key=key, value=value)
        await session.execute(stmt.on_conflict_do_update(index_elements=[BotSetting.key], set_={"value": value}))
        await session.commit()


async def _delete(session_maker: async_sessionmaker[AsyncSession], key: str) -> None:
    async with session_maker() as session:
        await session.execute(delete(BotSetting).where(BotSetting.key == key))
        await session.commit()


async def is_disabled(session: AsyncSession) -> bool:
    return await session.get(BotSetting, DISABLED_KEY) is not None


async def check_after_restart(session: AsyncSession) -> bool:
    """At startup, before the model is loaded. True when the last process died while embedding:
    semantic search is then switched off (stored, so it stays off after later restarts)."""
    if await session.get(BotSetting, RUNNING_KEY) is None:
        return False
    await session.execute(delete(BotSetting).where(BotSetting.key == RUNNING_KEY))
    # The model, not the file, killed the bot: files being processed again get a fresh chance.
    await session.execute(delete(BotSetting).where(BotSetting.key.like("material_resume:%")))
    await session.execute(insert(BotSetting).values(key=DISABLED_KEY, value=True).on_conflict_do_nothing())
    await session.commit()
    logger.error("The bot died while embedding; semantic search switched off (full-text search is used)")
    return True


def switch_off() -> None:
    set_embedding_provider(NullEmbeddingProvider())


@asynccontextmanager
async def guard(session_maker: async_sessionmaker[AsyncSession], provider: object) -> AsyncIterator[None]:
    """Wrap work that runs the local model. Other providers (tests, disabled) need no marker."""
    if not isinstance(provider, FastEmbedProvider):
        yield
        return
    await _set(session_maker, RUNNING_KEY, True)
    try:
        yield
    finally:
        await asyncio.shield(_delete(session_maker, RUNNING_KEY))


async def switch_on(session_maker: async_sessionmaker[AsyncSession]) -> bool:
    """Admin asked to try semantic search again (e.g. after giving the server more memory)."""
    await _delete(session_maker, DISABLED_KEY)
    set_embedding_provider(None)
    provider = get_embedding_provider()
    if not isinstance(provider, FastEmbedProvider):
        return provider.available
    async with guard(session_maker, provider):
        return await provider.warmup()
