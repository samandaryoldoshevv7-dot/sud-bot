"""Storage and similarity search of chunk embeddings.

Backends (detected at runtime from the actual column type):
* ``pgvector`` – ``vector(N)`` column, cosine distance operator ``<=>`` with HNSW index.
* ``array``    – ``real[]`` column; cosine similarity computed with numpy (dev fallback).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

_backend_cache: str | None = None


@dataclass
class SourceScope:
    material_ids: list[int] = field(default_factory=list)
    news_ids: list[int] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.material_ids and not self.news_ids

    def sql_filter(self, alias: str = "c") -> str:
        return (
            f"(({alias}.material_id = ANY(CAST(:mids AS integer[]))) "
            f"OR ({alias}.news_id = ANY(CAST(:nids AS integer[]))))"
        )

    def params(self) -> dict:
        return {"mids": list(self.material_ids), "nids": list(self.news_ids)}


async def detect_backend(session: AsyncSession, refresh: bool = False) -> str:
    global _backend_cache
    if _backend_cache is not None and not refresh:
        return _backend_cache
    udt = (
        await session.execute(
            text(
                "SELECT udt_name FROM information_schema.columns "
                "WHERE table_name = 'source_chunks' AND column_name = 'embedding'"
            )
        )
    ).scalar_one_or_none()
    _backend_cache = "pgvector" if udt == "vector" else ("array" if udt == "_float4" else "none")
    return _backend_cache


def reset_backend_cache() -> None:
    global _backend_cache
    _backend_cache = None


def _vector_literal(vec: list[float]) -> str:
    return "[" + ",".join(f"{x:.7g}" for x in vec) + "]"


async def store_embeddings(session: AsyncSession, items: list[tuple[int, list[float]]], model_name: str) -> None:
    backend = await detect_backend(session)
    if backend == "none" or not items:
        return
    if backend == "pgvector":
        stmt = text(
            "UPDATE source_chunks SET embedding = CAST(:emb AS vector), embedding_model = :model WHERE id = :id"
        )
        params = [{"id": cid, "emb": _vector_literal(vec), "model": model_name} for cid, vec in items]
    else:
        stmt = text("UPDATE source_chunks SET embedding = :emb, embedding_model = :model WHERE id = :id")
        params = [{"id": cid, "emb": [float(x) for x in vec], "model": model_name} for cid, vec in items]
    await session.execute(stmt, params)


async def vector_search(
    session: AsyncSession,
    query_vec: list[float],
    scope: SourceScope,
    limit: int,
    exclude_ids: set[int] | None = None,
) -> list[tuple[int, float]]:
    """Return ``(chunk_id, cosine_similarity)`` ordered by similarity (desc)."""
    backend = await detect_backend(session)
    exclude = list(exclude_ids or [])
    if backend == "pgvector":
        rows = await session.execute(
            text(
                "SELECT c.id, 1 - (c.embedding <=> CAST(:q AS vector)) AS score FROM source_chunks c "
                f"WHERE c.embedding IS NOT NULL AND {scope.sql_filter()} "
                "AND NOT (c.id = ANY(CAST(:ex AS integer[]))) "
                "ORDER BY c.embedding <=> CAST(:q AS vector) LIMIT :k"
            ),
            {"q": _vector_literal(query_vec), "k": limit, "ex": exclude, **scope.params()},
        )
        return [(int(r[0]), float(r[1])) for r in rows]
    if backend == "array":
        rows = (
            await session.execute(
                text(
                    "SELECT c.id, c.embedding FROM source_chunks c "
                    f"WHERE c.embedding IS NOT NULL AND {scope.sql_filter()} "
                    "AND NOT (c.id = ANY(CAST(:ex AS integer[])))"
                ),
                {"ex": exclude, **scope.params()},
            )
        ).all()
        if not rows:
            return []
        ids = np.array([r[0] for r in rows])
        matrix = np.array([r[1] for r in rows], dtype=np.float32)
        q = np.asarray(query_vec, dtype=np.float32)
        norms = np.linalg.norm(matrix, axis=1) * (np.linalg.norm(q) or 1.0)
        norms[norms == 0] = 1.0
        scores = matrix @ q / norms
        order = np.argsort(-scores)[:limit]
        return [(int(ids[i]), float(scores[i])) for i in order]
    return []


def _tsquery_terms(query: str) -> list[str]:
    words = "".join(ch if ch.isalnum() else " " for ch in query.lower()).split()
    return [w for w in words if len(w) > 2][:12]


async def fulltext_search(
    session: AsyncSession,
    query: str,
    scope: SourceScope,
    limit: int,
    exclude_ids: set[int] | None = None,
) -> list[tuple[int, float]]:
    """PostgreSQL full-text search with the language-agnostic ``simple`` configuration.

    Uzbek is agglutinative ("arxiv" → "arxivga", "arxivda"), so every term is matched as a
    prefix (``term:*``). All terms are tried first (AND), then any term (OR).
    """
    terms = _tsquery_terms(query)
    if not terms:
        return []
    for joiner in (" & ", " | "):
        rows = await session.execute(
            text(
                "SELECT c.id, ts_rank_cd(c.search_vector, q) AS score "
                "FROM source_chunks c, to_tsquery('simple', :query) q "
                f"WHERE c.search_vector @@ q AND {scope.sql_filter()} "
                "AND NOT (c.id = ANY(CAST(:ex AS integer[]))) "
                "ORDER BY score DESC LIMIT :k"
            ),
            {
                "query": joiner.join(f"{term}:*" for term in terms),
                "k": limit,
                "ex": list(exclude_ids or []),
                **scope.params(),
            },
        )
        results = [(int(r[0]), float(r[1])) for r in rows]
        if results or len(terms) == 1:
            return results
    return []
