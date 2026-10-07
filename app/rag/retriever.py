"""Retrieval used for question generation: seed selection + hybrid context retrieval."""

from __future__ import annotations

import logging
import random
from collections.abc import Iterable

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import Material, News, Question, QuestionStatus, SourceChunk
from app.rag.embeddings import EmbeddingProvider, get_embedding_provider
from app.rag.vector_store import SourceScope, fulltext_search, vector_search

logger = logging.getLogger(__name__)

RRF_K = 60


def reciprocal_rank_fusion(*rankings: Iterable[int]) -> list[int]:
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking):
            scores[item] = scores.get(item, 0.0) + 1.0 / (RRF_K + rank + 1)
    return [item for item, _ in sorted(scores.items(), key=lambda kv: kv[1], reverse=True)]


def chunk_reference(chunk: SourceChunk, title: str, kind: str) -> str:
    """Human readable, verifiable source reference built from chunk metadata (not from the AI)."""
    parts = [title]
    if kind == "news":
        parts[0] = f"Yangilik: {title}"
    if chunk.page_label:
        parts.append(f"{chunk.page_label}-bet")
    if chunk.heading:
        parts.append(chunk.heading[:120])
    return ", ".join(parts)


class Retriever:
    def __init__(self, embeddings: EmbeddingProvider | None = None, rng: random.Random | None = None):
        self.embeddings = embeddings or get_embedding_provider()
        self.rng = rng or random.Random()
        self.min_chars = get_settings().chunk_min_chars

    async def search(
        self, session: AsyncSession, query: str, scope: SourceScope, limit: int = 8,
        exclude_ids: set[int] | None = None,
    ) -> list[int]:  # fmt: skip
        """Hybrid search: vector similarity (if available) fused with full-text rank."""
        if scope.empty:
            return []
        rankings: list[list[int]] = []
        if self.embeddings.available:
            try:
                qvec = await self.embeddings.embed_query(query)
                rankings.append([cid for cid, _ in await vector_search(session, qvec, scope, limit * 2, exclude_ids)])
            except Exception as exc:
                logger.warning("Vector search failed, using full-text only", extra={"error": str(exc)[:200]})
        rankings.append([cid for cid, _ in await fulltext_search(session, query, scope, limit * 2, exclude_ids)])
        return reciprocal_rank_fusion(*rankings)[:limit]

    async def seed_chunks(
        self,
        session: AsyncSession,
        scope: SourceScope,
        count: int,
        focus_query: str | None = None,
        exclude_ids: set[int] | None = None,
    ) -> list[SourceChunk]:
        """Pick chunks to generate questions from, preferring under-used chunks (coverage)."""
        if scope.empty or count <= 0:
            return []
        usage = (
            select(Question.source_chunk_id, func.count(Question.id).label("n"))
            .where(Question.status != QuestionStatus.REJECTED)
            .group_by(Question.source_chunk_id)
            .subquery()
        )
        stmt = (
            select(SourceChunk, func.coalesce(usage.c.n, 0))
            .outerjoin(usage, usage.c.source_chunk_id == SourceChunk.id)
            .outerjoin(Material, Material.id == SourceChunk.material_id)
            .outerjoin(News, News.id == SourceChunk.news_id)
            .where(SourceChunk.char_count >= min(self.min_chars, 120))
            .where(
                (SourceChunk.material_id.in_(scope.material_ids) & Material.is_active.is_(True))
                | (SourceChunk.news_id.in_(scope.news_ids) & News.is_active.is_(True))
            )
        )
        if exclude_ids:
            stmt = stmt.where(SourceChunk.id.not_in(exclude_ids))
        candidates: list[tuple[SourceChunk, int]] = [(row[0], int(row[1])) for row in await session.execute(stmt)]
        if not candidates:
            return []

        if focus_query:
            ranked = await self.search(session, focus_query, scope, limit=max(count * 3, 10), exclude_ids=exclude_ids)
            rank_pos = {cid: i for i, cid in enumerate(ranked)}
            focused = [c for c in candidates if c[0].id in rank_pos]
            if focused:
                focused.sort(key=lambda c: (c[1] + c[0].generation_attempts, rank_pos[c[0].id]))
                return [c[0] for c in focused[:count]]

        self.rng.shuffle(candidates)
        candidates.sort(key=lambda c: c[1] * 2 + c[0].generation_attempts)
        # Spread seeds across documents for coverage: round-robin over sources.
        by_source: dict[tuple, list[SourceChunk]] = {}
        for chunk, _ in candidates:
            by_source.setdefault((chunk.material_id, chunk.news_id), []).append(chunk)
        seeds: list[SourceChunk] = []
        queues = list(by_source.values())
        while len(seeds) < count and any(queues):
            for queue in queues:
                if queue and len(seeds) < count:
                    seeds.append(queue.pop(0))
        return seeds

    async def context_for(
        self, session: AsyncSession, seed: SourceChunk, scope: SourceScope, neighbours: int = 2
    ) -> list[SourceChunk]:
        """The seed chunk plus the most related chunks (semantic neighbours or adjacent chunks)."""
        related_ids: list[int] = []
        if self.embeddings.available:
            try:
                qvec = await self.embeddings.embed_query(seed.text[:2000])
                hits = await vector_search(session, qvec, scope, neighbours + 1, exclude_ids={seed.id})
                related_ids = [cid for cid, score in hits if score >= 0.35][:neighbours]
            except Exception as exc:
                logger.warning("Neighbour search failed", extra={"error": str(exc)[:200]})
        if len(related_ids) < neighbours:
            adjacent = select(SourceChunk.id).where(
                SourceChunk.chunk_index.in_([seed.chunk_index - 1, seed.chunk_index + 1]),
                SourceChunk.id != seed.id,
            )
            if seed.material_id is not None:
                adjacent = adjacent.where(SourceChunk.material_id == seed.material_id)
            else:
                adjacent = adjacent.where(SourceChunk.news_id == seed.news_id)
            for cid in (await session.execute(adjacent)).scalars():
                if cid not in related_ids and len(related_ids) < neighbours:
                    related_ids.append(cid)
        if not related_ids:
            return [seed]
        related = (await session.execute(select(SourceChunk).where(SourceChunk.id.in_(related_ids)))).scalars().all()
        order = {cid: i for i, cid in enumerate(related_ids)}
        return [seed, *sorted(related, key=lambda c: order[c.id])]
