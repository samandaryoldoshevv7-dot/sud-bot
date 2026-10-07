# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    EMBEDDING_CACHE_DIR=/app/models_cache \
    PORT=8080

WORKDIR /app

RUN useradd --create-home --uid 10001 appuser

COPY requirements.txt .
RUN pip install -r requirements.txt

# Bake the multilingual embedding model (~220 MB) into the image so the bot never downloads it
# at runtime. Disable with --build-arg PRELOAD_EMBEDDING_MODEL=0 (it is then fetched on first start).
ARG PRELOAD_EMBEDDING_MODEL=1
ARG EMBEDDING_MODEL=sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
ENV EMBEDDING_MODEL=${EMBEDDING_MODEL}
COPY scripts/preload_embeddings.py scripts/preload_embeddings.py
RUN mkdir -p /app/models_cache && \
    if [ "$PRELOAD_EMBEDDING_MODEL" = "1" ]; then \
        python scripts/preload_embeddings.py || echo "WARNING: embedding model preload failed; it will be downloaded at runtime"; \
    fi

COPY alembic.ini pyproject.toml ./
COPY migrations ./migrations
COPY app ./app
COPY scripts ./scripts

RUN chmod +x scripts/start.sh && chown -R appuser:appuser /app
USER appuser

EXPOSE 8080
CMD ["sh", "scripts/start.sh"]
