from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base, TimestampMixin, str_enum
from app.models.enums import FileType, MaterialStatus


class Material(TimestampMixin, Base):
    """An uploaded training/informational document."""

    __tablename__ = "materials"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    file_name: Mapped[str | None] = mapped_column(String(255))
    file_type: Mapped[FileType] = mapped_column(str_enum(FileType, "file_type"), nullable=False)
    telegram_file_id: Mapped[str | None] = mapped_column(String(255))
    telegram_file_unique_id: Mapped[str | None] = mapped_column(String(128))
    file_size: Mapped[int | None] = mapped_column(BigInteger)
    content_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    category: Mapped[str | None] = mapped_column(String(128), index=True)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[MaterialStatus] = mapped_column(
        str_enum(MaterialStatus, "material_status"), default=MaterialStatus.UPLOADED, nullable=False, index=True
    )
    error_message: Mapped[str | None] = mapped_column(Text)
    extracted_text: Mapped[str | None] = mapped_column(Text, deferred=True)
    page_count: Mapped[int | None] = mapped_column(Integer)
    char_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    uploaded_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    chunks: Mapped[list[SourceChunk]] = relationship(
        back_populates="material", cascade="all, delete-orphan", passive_deletes=True
    )


class News(TimestampMixin, Base):
    """A manually added news item / announcement; may be used as a question source."""

    __tablename__ = "news"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    news_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    source: Mapped[str | None] = mapped_column(String(255))
    category: Mapped[str | None] = mapped_column(String(128))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    status: Mapped[MaterialStatus] = mapped_column(
        str_enum(MaterialStatus, "news_status"), default=MaterialStatus.UPLOADED, nullable=False
    )
    error_message: Mapped[str | None] = mapped_column(Text)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    chunks: Mapped[list[SourceChunk]] = relationship(
        back_populates="news", cascade="all, delete-orphan", passive_deletes=True
    )


class SourceChunk(Base):
    """A retrievable chunk of a material or a news item.

    The ``embedding`` column is created by the migration either as ``vector(N)`` (pgvector)
    or ``real[]`` (fallback) and is managed exclusively by :mod:`app.rag.vector_store`.
    """

    __tablename__ = "source_chunks"
    __table_args__ = (
        CheckConstraint(
            "(material_id IS NOT NULL AND news_id IS NULL) OR (material_id IS NULL AND news_id IS NOT NULL)",
            name="one_source",
        ),
        Index("ix_source_chunks_material_idx", "material_id", "chunk_index"),
        Index("ix_source_chunks_news_idx", "news_id", "chunk_index"),
        Index("ix_source_chunks_search_vector", "search_vector", postgresql_using="gin"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    material_id: Mapped[int | None] = mapped_column(ForeignKey("materials.id", ondelete="CASCADE"))
    news_id: Mapped[int | None] = mapped_column(ForeignKey("news.id", ondelete="CASCADE"))
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    page_start: Mapped[int | None] = mapped_column(Integer)
    page_end: Mapped[int | None] = mapped_column(Integer)
    heading: Mapped[str | None] = mapped_column(String(500))
    char_count: Mapped[int] = mapped_column(Integer, nullable=False)
    embedding_model: Mapped[str | None] = mapped_column(String(255))
    generation_attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    search_vector = mapped_column(TSVECTOR, Computed("to_tsvector('simple', text)", persisted=True), deferred=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    material: Mapped[Material | None] = relationship(back_populates="chunks")
    news: Mapped[News | None] = relationship(back_populates="chunks")

    @property
    def page_label(self) -> str | None:
        if self.page_start is None:
            return None
        if self.page_end and self.page_end != self.page_start:
            return f"{self.page_start}-{self.page_end}"
        return str(self.page_start)
