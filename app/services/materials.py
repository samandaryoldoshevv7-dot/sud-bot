"""Material ingestion: extraction → cleaning → chunking → embeddings → READY."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.documents import DocumentError, ExtractedDocument, chunk_pages, clean_pages, extract_document
from app.documents.extractors import extract_plain_text
from app.models import FileType, Material, MaterialStatus, News, Question, QuestionStatus, SourceChunk
from app.rag.embeddings import EmbeddingProvider, get_embedding_provider
from app.rag.vector_store import store_embeddings
from app.utils.time import utcnow

logger = logging.getLogger(__name__)

PAGE_SIZE = 8


@dataclass
class ProcessResult:
    ok: bool
    chunk_count: int = 0
    page_count: int | None = None
    char_count: int = 0
    embedded: bool = False
    error_code: str | None = None
    error_detail: str = ""


async def find_duplicate(session: AsyncSession, content_hash: str) -> Material | None:
    stmt = select(Material).where(Material.content_hash == content_hash, Material.is_active.is_(True)).limit(1)
    return (await session.execute(stmt)).scalar_one_or_none()


async def create_material(
    session: AsyncSession,
    *,
    title: str,
    file_type: FileType,
    uploaded_by_id: int | None,
    file_name: str | None = None,
    telegram_file_id: str | None = None,
    telegram_file_unique_id: str | None = None,
    file_size: int | None = None,
    content_hash: str | None = None,
    category: str | None = None,
    description: str | None = None,
    raw_text: str | None = None,
) -> Material:
    material = Material(
        title=title.strip()[:255],
        file_name=(file_name or "")[:255] or None,
        file_type=file_type,
        telegram_file_id=telegram_file_id,
        telegram_file_unique_id=telegram_file_unique_id,
        file_size=file_size,
        content_hash=content_hash,
        category=(category or "").strip()[:128] or None,
        description=(description or "").strip() or None,
        status=MaterialStatus.UPLOADED,
        uploaded_by_id=uploaded_by_id,
        extracted_text=raw_text,
    )
    session.add(material)
    await session.commit()
    logger.info("Material uploaded", extra={"material_id": material.id, "file_type": file_type.value})
    return material


# A material still PROCESSING after this long is treated as interrupted (the admin may restart it).
STALE_PROCESSING = timedelta(minutes=15)
# Embedding a very large document on a small CPU can take long; after this the material is still
# usable through full-text search, so processing finishes instead of hanging.
EMBEDDING_TIMEOUT_SECONDS = 600


async def _embed_chunks(session: AsyncSession, chunks: list[SourceChunk], embeddings: EmbeddingProvider) -> bool:
    if not embeddings.available or not chunks:
        return False
    loop = asyncio.get_running_loop()
    deadline = loop.time() + EMBEDDING_TIMEOUT_SECONDS
    try:
        batch = get_settings().embedding_batch_size * 4
        for start in range(0, len(chunks), batch):
            part = chunks[start : start + batch]
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError
            vectors = await asyncio.wait_for(embeddings.embed_documents([c.text for c in part]), remaining)
            await store_embeddings(
                session, [(c.id, v) for c, v in zip(part, vectors, strict=True)], embeddings.model_name
            )
        return True
    except TimeoutError:
        logger.error(
            "Embedding took too long; material stays searchable via full-text",
            extra={"chunks": len(chunks), "timeout": EMBEDDING_TIMEOUT_SECONDS},
        )
        return False
    except Exception as exc:
        logger.error("Embedding failed; chunks remain searchable via full-text", extra={"error": str(exc)[:300]})
        return False


async def _store_chunks(
    session: AsyncSession, doc: ExtractedDocument, *, material_id: int | None = None, news_id: int | None = None
) -> list[SourceChunk]:
    settings = get_settings()
    pieces = chunk_pages(doc.pages, settings.chunk_size, settings.chunk_overlap, settings.chunk_min_chars)
    if material_id is not None:
        await session.execute(delete(SourceChunk).where(SourceChunk.material_id == material_id))
    if news_id is not None:
        await session.execute(delete(SourceChunk).where(SourceChunk.news_id == news_id))
    chunks = [
        SourceChunk(
            material_id=material_id,
            news_id=news_id,
            chunk_index=i,
            text=piece.text,
            page_start=piece.page_start,
            page_end=piece.page_end,
            heading=piece.heading,
            char_count=piece.char_count,
        )
        for i, piece in enumerate(pieces)
    ]
    session.add_all(chunks)
    await session.flush()
    return chunks


async def process_material(
    session_maker: async_sessionmaker[AsyncSession],
    material_id: int,
    file_path: Path | None = None,
    embeddings: EmbeddingProvider | None = None,
) -> ProcessResult:
    """Run the full ingestion pipeline. Always leaves the material in READY or FAILED."""
    embeddings = embeddings or get_embedding_provider()
    settings = get_settings()
    async with session_maker() as session:
        material = await session.get(Material, material_id)
        if material is None:
            return ProcessResult(ok=False, error_code="not_found")
        material.status = MaterialStatus.PROCESSING
        material.error_message = None
        await session.commit()
        try:
            if file_path is not None:
                doc = await asyncio.to_thread(extract_document, file_path, material.file_type, settings.max_text_chars)
            else:
                stored = (
                    await session.execute(select(Material.extracted_text).where(Material.id == material_id))
                ).scalar_one()
                if not stored:
                    raise DocumentError("no_source")
                doc = extract_plain_text(stored)
            doc.pages = clean_pages(doc.pages)
            if doc.char_count < 50:
                raise DocumentError("empty_document")
            chunks = await _store_chunks(session, doc, material_id=material_id)
            if not chunks:
                raise DocumentError("empty_document")
            embedded = await _embed_chunks(session, chunks, embeddings)
            material.extracted_text = doc.text
            material.page_count = doc.page_count
            material.char_count = doc.char_count
            material.chunk_count = len(chunks)
            material.status = MaterialStatus.READY
            material.processed_at = utcnow()
            await session.commit()
            logger.info(
                "Material processed",
                extra={"material_id": material_id, "chunks": len(chunks), "embedded": embedded},
            )
            return ProcessResult(
                ok=True,
                chunk_count=len(chunks),
                page_count=doc.page_count,
                char_count=doc.char_count,
                embedded=embedded,
            )
        except DocumentError as exc:
            await session.rollback()
            await _mark_failed(session, material_id, exc.code, exc.detail)
            logger.warning("Material processing failed", extra={"material_id": material_id, "code": exc.code})
            return ProcessResult(ok=False, error_code=exc.code, error_detail=exc.detail)
        except Exception as exc:
            await session.rollback()
            await _mark_failed(session, material_id, "internal", str(exc)[:300])
            logger.exception("Material processing crashed", extra={"material_id": material_id})
            return ProcessResult(ok=False, error_code="internal", error_detail=str(exc)[:300])


def is_stale(material: Material, now: datetime | None = None) -> bool:
    """PROCESSING for too long: the worker died (restart, out of memory) — safe to start again."""
    now = now or utcnow()
    return material.status == MaterialStatus.PROCESSING and now - material.updated_at > STALE_PROCESSING


async def interrupted_material_ids(session: AsyncSession) -> list[int]:
    """Materials left UPLOADED/PROCESSING by a previous process (called once at startup)."""
    stmt = select(Material.id).where(
        Material.status.in_([MaterialStatus.UPLOADED, MaterialStatus.PROCESSING]), Material.is_active.is_(True)
    )
    return list((await session.execute(stmt)).scalars().all())


async def _mark_failed(session: AsyncSession, material_id: int, code: str, detail: str) -> None:
    material = await session.get(Material, material_id)
    if material is not None:
        material.status = MaterialStatus.FAILED
        material.error_message = f"{code}: {detail}" if detail else code
        await session.commit()


async def list_materials(
    session: AsyncSession, page: int, include_archived: bool = False
) -> tuple[list[tuple[Material, int]], int]:
    qcount = (
        select(Question.source_material_id, func.count(Question.id).label("n"))
        .where(Question.status != QuestionStatus.REJECTED)
        .group_by(Question.source_material_id)
        .subquery()
    )
    base = select(Material)
    if not include_archived:
        base = base.where(Material.is_active.is_(True))
    total = (await session.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
    stmt = select(Material, func.coalesce(qcount.c.n, 0)).outerjoin(qcount, qcount.c.source_material_id == Material.id)
    if not include_archived:
        stmt = stmt.where(Material.is_active.is_(True))
    stmt = (
        stmt.order_by(Material.is_active.desc(), Material.created_at.desc()).offset(page * PAGE_SIZE).limit(PAGE_SIZE)
    )
    return [(m, int(n)) for m, n in await session.execute(stmt)], total


async def ready_materials(session: AsyncSession) -> list[Material]:
    stmt = (
        select(Material)
        .where(Material.status == MaterialStatus.READY, Material.is_active.is_(True))
        .order_by(Material.created_at.desc())
    )
    return list((await session.execute(stmt)).scalars().all())


async def material_question_stats(session: AsyncSession, material_id: int) -> dict[str, int]:
    rows = await session.execute(
        select(Question.status, func.count(Question.id))
        .where(Question.source_material_id == material_id)
        .group_by(Question.status)
    )
    stats = {s.value: 0 for s in QuestionStatus}
    for status, n in rows:
        stats[status.value] = n
    return stats


async def set_archived(session: AsyncSession, material_id: int, archived: bool) -> Material | None:
    material = await session.get(Material, material_id)
    if material:
        material.is_active = not archived
        await session.commit()
    return material


async def delete_material(session: AsyncSession, material_id: int) -> bool:
    """Hard delete. Chunks cascade; bank questions keep their text but lose the source link and
    are rejected (they can no longer be verified). Test snapshots are untouched."""
    material = await session.get(Material, material_id)
    if material is None:
        return False
    await session.execute(
        update(Question)
        .where(Question.source_material_id == material_id, Question.status != QuestionStatus.REJECTED)
        .values(status=QuestionStatus.REJECTED)
    )
    await session.delete(material)
    await session.commit()
    logger.info("Material deleted", extra={"material_id": material_id})
    return True


# ------------------------------------------------------------------------------------- news


async def create_news(
    session: AsyncSession,
    *,
    title: str,
    body: str,
    news_date,
    source: str | None,
    category: str | None,
    created_by_id: int | None,
    embeddings: EmbeddingProvider | None = None,
) -> News:
    embeddings = embeddings or get_embedding_provider()
    news = News(
        title=title.strip()[:255],
        body=body.strip(),
        news_date=news_date,
        source=(source or "").strip()[:255] or None,
        category=(category or "").strip()[:128] or None,
        created_by_id=created_by_id,
        status=MaterialStatus.PROCESSING,
    )
    session.add(news)
    await session.commit()
    await index_news(session, news, embeddings)
    return news


async def index_news(session: AsyncSession, news: News, embeddings: EmbeddingProvider | None = None) -> None:
    embeddings = embeddings or get_embedding_provider()
    try:
        text_ = f"{news.title}\n\n{news.body}"
        doc = extract_plain_text(text_)
        doc.pages = clean_pages(doc.pages)
        chunks = await _store_chunks(session, doc, news_id=news.id)
        await _embed_chunks(session, chunks, embeddings)
        news.chunk_count = len(chunks)
        news.status = MaterialStatus.READY
        news.error_message = None
        await session.commit()
        logger.info("News indexed", extra={"news_id": news.id, "chunks": len(chunks)})
    except Exception as exc:
        await session.rollback()
        news = await session.get(News, news.id)  # type: ignore[assignment]
        if news:
            news.status = MaterialStatus.FAILED
            news.error_message = str(exc)[:300]
            await session.commit()
        logger.exception("News indexing failed")


async def list_news(session: AsyncSession, page: int) -> tuple[list[News], int]:
    total = (await session.execute(select(func.count(News.id)))).scalar_one()
    rows = await session.execute(
        select(News).order_by(News.news_date.desc(), News.id.desc()).offset(page * PAGE_SIZE).limit(PAGE_SIZE)
    )
    return list(rows.scalars().all()), total


async def active_news_ids(session: AsyncSession) -> list[int]:
    stmt = select(News.id).where(News.is_active.is_(True), News.status == MaterialStatus.READY)
    return list((await session.execute(stmt)).scalars().all())
