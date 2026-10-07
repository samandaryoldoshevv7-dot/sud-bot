"""Re-compute embeddings for all source chunks (after changing EMBEDDING_MODEL, or to fill gaps).

Usage: python -m scripts.reembed [--only-missing]
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from sqlalchemy import select, text

from app.config import get_settings
from app.database.session import dispose_engine, get_session_maker
from app.models import SourceChunk
from app.rag.embeddings import get_embedding_provider
from app.rag.vector_store import detect_backend, store_embeddings
from app.utils.logging import setup_logging

BATCH = 128


async def run(only_missing: bool) -> None:
    settings = get_settings()
    setup_logging("INFO", "text", settings.secret_values())
    provider = get_embedding_provider()
    if not provider.available:
        raise SystemExit("Embedding provider is disabled or unavailable (EMBEDDING_PROVIDER).")
    maker = get_session_maker()
    async with maker() as session:
        backend = await detect_backend(session, refresh=True)
        if backend == "none":
            raise SystemExit("source_chunks.embedding column missing: run `alembic upgrade head` first.")
        stmt = select(SourceChunk.id, SourceChunk.text).order_by(SourceChunk.id)
        if only_missing:
            ids = set((await session.execute(text("SELECT id FROM source_chunks WHERE embedding IS NULL"))).scalars())
            rows = [r for r in (await session.execute(stmt)).all() if r[0] in ids]
        else:
            rows = (await session.execute(stmt)).all()
        logging.info("Re-embedding %s chunks with %s (%s backend)", len(rows), provider.model_name, backend)
        for start in range(0, len(rows), BATCH):
            part = rows[start : start + BATCH]
            vectors = await provider.embed_documents([r[1] for r in part])
            await store_embeddings(
                session, [(r[0], v) for r, v in zip(part, vectors, strict=True)], provider.model_name
            )
            await session.commit()
            logging.info("Embedded %s/%s", min(start + BATCH, len(rows)), len(rows))
    await dispose_engine()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--only-missing", action="store_true")
    asyncio.run(run(parser.parse_args().only_missing))
