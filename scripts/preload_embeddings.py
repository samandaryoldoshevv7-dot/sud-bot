"""Download the embedding model at Docker build time so containers start without network fetches.

Usage: python scripts/preload_embeddings.py   (honours EMBEDDING_MODEL / EMBEDDING_CACHE_DIR)
"""

from __future__ import annotations

import os
import sys


def main() -> int:
    model = os.environ.get("EMBEDDING_MODEL", "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
    cache_dir = os.environ.get("EMBEDDING_CACHE_DIR", "/app/models_cache")
    from fastembed import TextEmbedding

    embedder = TextEmbedding(model_name=model, cache_dir=cache_dir)
    vector = next(iter(embedder.embed(["Sud ishlarini yuritish qoidalari"])))
    print(f"Embedding model ready: {model} (dim={len(vector)}) in {cache_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
