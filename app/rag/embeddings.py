"""Embedding providers.

``fastembed`` runs a multilingual ONNX sentence-transformer locally (no extra API key,
no PyTorch). The model is downloaded once (baked into the Docker image at build time).
If the model cannot be loaded, retrieval transparently falls back to PostgreSQL
full-text search, so the bot keeps working.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from abc import ABC, abstractmethod

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)


class EmbeddingProvider(ABC):
    name: str = "base"
    model_name: str = ""
    dim: int = 0

    @property
    def available(self) -> bool:
        return True

    @abstractmethod
    async def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    async def embed_query(self, text: str) -> list[float]:
        return (await self.embed_documents([text]))[0]


class NullEmbeddingProvider(EmbeddingProvider):
    name = "none"

    @property
    def available(self) -> bool:
        return False

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("Embeddings are disabled")


class FastEmbedProvider(EmbeddingProvider):
    name = "fastembed"

    def __init__(self, settings: Settings):
        self.model_name = settings.embedding_model
        self.dim = settings.embedding_dim
        self._cache_dir = settings.embedding_cache_dir
        self._batch_size = settings.embedding_batch_size
        self._model = None
        self._failed: str | None = None
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        return self._failed is None

    def _load(self):
        with self._lock:
            if self._model is None and self._failed is None:
                try:
                    from fastembed import TextEmbedding

                    self._model = TextEmbedding(model_name=self.model_name, cache_dir=self._cache_dir)
                    logger.info("Embedding model loaded", extra={"model": self.model_name})
                except Exception as exc:
                    self._failed = str(exc)[:300]
                    logger.error(
                        "Embedding model could not be loaded; falling back to full-text search",
                        extra={"model": self.model_name, "error": self._failed},
                    )
            return self._model

    def _embed_sync(self, texts: list[str]) -> list[list[float]]:
        model = self._load()
        if model is None:
            raise RuntimeError(f"Embedding model unavailable: {self._failed}")
        vectors = [vec.tolist() for vec in model.embed(texts, batch_size=self._batch_size)]
        if vectors and len(vectors[0]) != self.dim:
            raise RuntimeError(
                f"Embedding dimension mismatch: model returns {len(vectors[0])}, EMBEDDING_DIM={self.dim}"
            )
        return vectors

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return await asyncio.to_thread(self._embed_sync, texts)

    async def warmup(self) -> bool:
        await asyncio.to_thread(self._load)
        return self.available


_provider: EmbeddingProvider | None = None


def get_embedding_provider(settings: Settings | None = None) -> EmbeddingProvider:
    global _provider
    if _provider is None:
        settings = settings or get_settings()
        if settings.embedding_provider == "fastembed":
            _provider = FastEmbedProvider(settings)
        else:
            _provider = NullEmbeddingProvider()
    return _provider


def set_embedding_provider(provider: EmbeddingProvider | None) -> None:
    global _provider
    _provider = provider
