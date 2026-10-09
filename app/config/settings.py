"""Application configuration loaded from environment variables.

All secrets (bot token, API keys, database credentials) come exclusively from the
environment. They are stored as ``SecretStr`` so they never appear in ``repr`` or logs.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_VALIDATION_MODEL = "openai/gpt-oss-20b"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Telegram -----------------------------------------------------------------
    bot_token: SecretStr
    admin_telegram_ids: str = Field(default="", validation_alias=AliasChoices("ADMIN_TELEGRAM_IDS", "ADMIN_IDS"))
    bot_mode: Literal["polling", "webhook"] = "polling"
    webhook_base_url: str = ""
    webhook_path: str = "/telegram/webhook"
    webhook_secret: SecretStr | None = None
    telegram_api_base: str = ""  # optional self-hosted Bot API server, e.g. http://telegram-bot-api:8081

    # --- Database -----------------------------------------------------------------
    database_url: SecretStr
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_pool_timeout: int = 30
    db_echo: bool = False

    # --- AI / LLM -----------------------------------------------------------------
    ai_provider: str = "groq"  # legacy, unused: see AI_PROVIDER_ORDER
    groq_api_key: SecretStr | None = None
    groq_model: str = "openai/gpt-oss-120b"
    groq_fallback_models: str = "openai/gpt-oss-20b,qwen/qwen3-32b,llama-3.1-8b-instant"
    # Checks (blind source check, quality review) run on a smaller model so the main model's daily
    # token limit lasts longer; set GROQ_VALIDATION_MODEL to the main model to use it for checks too.
    groq_validation_model: str = ""
    groq_temperature: float = 0.3
    groq_timeout_seconds: float = 60.0
    ai_max_retries: int = 3
    # Other AI services, each with its own official key. When one reaches its limit the bot moves
    # on to the next one in AI_PROVIDER_ORDER (only services whose key is set take part).
    ai_provider_order: str = "gemini,groq,cerebras,openrouter"
    gemini_api_key: SecretStr | None = None
    gemini_model: str = "gemini-2.5-flash"
    gemini_fallback_models: str = "gemini-2.5-flash-lite,gemini-2.0-flash"
    gemini_validation_model: str = "gemini-2.5-flash-lite"
    cerebras_api_key: SecretStr | None = None
    cerebras_model: str = "gpt-oss-120b"
    cerebras_fallback_models: str = "llama-3.3-70b,qwen-3-32b"
    openrouter_api_key: SecretStr | None = None
    openrouter_model: str = "openai/gpt-oss-120b:free"
    openrouter_fallback_models: str = "meta-llama/llama-3.3-70b-instruct:free"
    # At most this many Groq requests at the same time (bursts cause 429 rate-limit errors).
    groq_max_concurrency: int = Field(default=2, ge=1, le=10)
    # Identical deterministic AI checks (temperature 0) are answered from memory for this long.
    ai_cache_ttl_seconds: int = 6 * 3600
    ai_min_confidence: float = 0.7
    ai_excerpt_min_similarity: int = 88
    ai_questions_per_chunk: int = 2

    # --- Embeddings / RAG ---------------------------------------------------------
    embedding_provider: Literal["fastembed", "none"] = "fastembed"
    embedding_model: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    embedding_dim: int = 384
    embedding_cache_dir: str = "/app/models_cache"
    embedding_batch_size: int = 32
    chunk_size: int = 1200
    chunk_overlap: int = 200
    chunk_min_chars: int = 200

    # --- Files --------------------------------------------------------------------
    max_file_size_mb: int = Field(default=20, ge=1, le=20)
    max_text_chars: int = 2_000_000

    # --- Runtime ------------------------------------------------------------------
    timezone: str = "Asia/Tashkent"
    default_language: str = "uz"
    log_level: str = "INFO"
    log_format: Literal["json", "text"] = "json"
    port: int = 8080
    scheduler_interval_seconds: int = 60
    broadcast_delay_seconds: float = 0.05

    @field_validator("admin_telegram_ids")
    @classmethod
    def _validate_admin_ids(cls, value: str) -> str:
        for part in value.replace(";", ",").split(","):
            part = part.strip()
            if part and not part.lstrip("-").isdigit():
                raise ValueError(f"ADMIN_TELEGRAM_IDS contains a non-numeric value: {part!r}")
        return value

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, value: str) -> str:
        ZoneInfo(value)  # raises if unknown
        return value

    @property
    def admin_ids(self) -> frozenset[int]:
        ids = set()
        for part in self.admin_telegram_ids.replace(";", ",").split(","):
            part = part.strip()
            if part:
                ids.add(int(part))
        return frozenset(ids)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def max_file_size_bytes(self) -> int:
        return self.max_file_size_mb * 1024 * 1024

    @property
    def fallback_models(self) -> list[str]:
        return [m.strip() for m in self.groq_fallback_models.split(",") if m.strip()]

    @property
    def validation_model(self) -> str:
        return self.groq_validation_model or DEFAULT_VALIDATION_MODEL

    @property
    def groq_enabled(self) -> bool:
        return self.groq_api_key is not None and bool(self.groq_api_key.get_secret_value())

    def provider_key(self, name: str) -> str:
        """The API key of one AI service ("" when not set)."""
        secret = {"groq": self.groq_api_key, "gemini": self.gemini_api_key,
                  "cerebras": self.cerebras_api_key, "openrouter": self.openrouter_api_key}.get(name)  # fmt: skip
        return secret.get_secret_value().strip() if secret is not None else ""

    @property
    def ai_providers(self) -> list[str]:
        """Configured AI services in the order they are tried."""
        known = ("gemini", "groq", "cerebras", "openrouter")
        order = [p.strip().lower() for p in self.ai_provider_order.split(",") if p.strip()]
        order += [p for p in known if p not in order]  # a key set but missing from the order: last
        seen: list[str] = []
        for name in order:
            if name in known and name not in seen and self.provider_key(name):
                seen.append(name)
        return seen

    @property
    def ai_enabled(self) -> bool:
        return bool(self.ai_providers)

    @property
    def async_database_url(self) -> str:
        return normalize_database_url(self.database_url.get_secret_value())[0]

    @property
    def database_connect_args(self) -> dict:
        return normalize_database_url(self.database_url.get_secret_value())[1]

    def secret_values(self) -> list[str]:
        """Raw secret strings used by the log redaction filter."""
        values = [self.bot_token.get_secret_value()]
        for key in (self.groq_api_key, self.gemini_api_key, self.cerebras_api_key, self.openrouter_api_key):
            if key:
                values.append(key.get_secret_value())
        if self.webhook_secret:
            values.append(self.webhook_secret.get_secret_value())
        password = urlsplit(self.database_url.get_secret_value()).password
        if password:
            values.append(password)
        return [v for v in values if v and len(v) >= 4]


def normalize_database_url(url: str) -> tuple[str, dict]:
    """Convert a Railway/Heroku style URL into an asyncpg SQLAlchemy URL.

    ``postgres://`` and ``postgresql://`` become ``postgresql+asyncpg://``. The libpq-only
    ``sslmode`` query parameter (not understood by asyncpg) is translated to ``connect_args``.
    """
    parts = urlsplit(url.strip())
    scheme = parts.scheme
    if scheme in ("postgres", "postgresql", "postgresql+psycopg2", "postgresql+psycopg"):
        scheme = "postgresql+asyncpg"
    query = dict(parse_qsl(parts.query))
    connect_args: dict = {}
    sslmode = query.pop("sslmode", None)
    if sslmode and sslmode not in ("disable", "allow"):
        # asyncpg understands the libpq sslmode names (prefer/require/verify-ca/verify-full).
        connect_args["ssl"] = sslmode
    return urlunsplit((scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)), connect_args


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
