"""DDL helpers for the embedding column (shared by the migration and the test suite)."""

from __future__ import annotations

import logging
import os

from sqlalchemy import text
from sqlalchemy.engine import Connection

logger = logging.getLogger(__name__)


def embedding_dim() -> int:
    return int(os.environ.get("EMBEDDING_DIM", "384"))


def add_embedding_column(conn: Connection, dim: int | None = None) -> str:
    """Add ``source_chunks.embedding``. Returns ``"pgvector"`` or ``"array"``.

    ``CREATE EXTENSION`` is attempted inside a SAVEPOINT so a missing extension (or
    missing privileges) does not abort the surrounding migration transaction.
    """
    dim = dim or embedding_dim()
    backend = "array"
    if os.environ.get("DISABLE_PGVECTOR", "").lower() not in ("1", "true", "yes"):
        savepoint = conn.begin_nested()
        try:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            savepoint.commit()
            backend = "pgvector"
        except Exception as exc:  # extension not available on this server
            savepoint.rollback()
            logger.warning("pgvector extension unavailable, using real[] embeddings: %s", exc)

    if backend == "pgvector":
        conn.execute(text(f"ALTER TABLE source_chunks ADD COLUMN embedding vector({dim})"))
        savepoint = conn.begin_nested()
        try:
            conn.execute(
                text(
                    "CREATE INDEX ix_source_chunks_embedding ON source_chunks USING hnsw (embedding vector_cosine_ops)"
                )
            )
            savepoint.commit()
        except Exception as exc:  # very old pgvector without HNSW: exact search still works
            savepoint.rollback()
            logger.warning("Could not create HNSW index (exact search will be used): %s", exc)
    else:
        conn.execute(text("ALTER TABLE source_chunks ADD COLUMN embedding real[]"))
    return backend
