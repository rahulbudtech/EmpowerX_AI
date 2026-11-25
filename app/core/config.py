"""
Application settings loaded from environment / .env.

Keep secrets out of source control — use `.env` locally and `.env.example`
as the documented template.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Central config. Extend as Postgres / Redis / Celery come online."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- LLM primary (any supported provider; switch via env) ---
    llm_provider: str = Field(
        default="gemini",
        alias="LLM_PROVIDER",
        description="Primary provider id: gemini | anthropic",
    )
    llm_model: str = Field(
        default="gemini-3.6-flash",
        alias="LLM_MODEL",
        description="Primary model name for findings",
    )
    gemini_api_key: str | None = Field(default=None, alias="GEMINI_API_KEY")
    google_api_key: str | None = Field(default=None, alias="GOOGLE_API_KEY")
    anthropic_api_key: str | None = Field(default=None, alias="ANTHROPIC_API_KEY")

    # Optional fallback (tried once if primary fails; also switchable via env)
    llm_fallback_provider: str | None = Field(
        default=None,
        alias="LLM_FALLBACK_PROVIDER",
    )
    llm_fallback_model: str | None = Field(
        default=None,
        alias="LLM_FALLBACK_MODEL",
    )

    # --- LLM token cost (USD per 1M tokens; human-approved, env-updated) ---
    # Gemini: thinking/reasoning billed at output rate.
    # Anthropic: no separate reasoning meter — keep reasoning_tokens at 0 in usage.
    gemini_input_price_per_million_usd: float = Field(
        default=0.75,
        alias="GEMINI_INPUT_PRICE_PER_MILLION_USD",
    )
    gemini_output_price_per_million_usd: float = Field(
        default=3.75,
        alias="GEMINI_OUTPUT_PRICE_PER_MILLION_USD",
    )
    gemini_cached_input_price_per_million_usd: float = Field(
        default=0.075,
        alias="GEMINI_CACHED_INPUT_PRICE_PER_MILLION_USD",
    )
    # Anthropic: input $2 / output $10 / MTok; cache read $0.10 / MTok.
    anthropic_input_price_per_million_usd: float = Field(
        default=2.0,
        alias="ANTHROPIC_INPUT_PRICE_PER_MILLION_USD",
    )
    anthropic_output_price_per_million_usd: float = Field(
        default=10.0,
        alias="ANTHROPIC_OUTPUT_PRICE_PER_MILLION_USD",
    )
    anthropic_cached_input_price_per_million_usd: float = Field(
        default=0.10,
        alias="ANTHROPIC_CACHED_INPUT_PRICE_PER_MILLION_USD",
    )

    # --- Postgres ---
    database_url: str = Field(
        default="postgresql+psycopg://postgres:postgres@127.0.0.1:5432/empowerx",
        alias="DATABASE_URL",
    )

    # --- Redis ---
    redis_url: str = Field(
        default="redis://127.0.0.1:6379/0",
        alias="REDIS_URL",
        description="Redis connection URL (db 0 default for app ping / broker)",
    )

    # --- Celery (defaults derive from REDIS_URL when unset) ---
    celery_broker_url: str | None = Field(default=None, alias="CELERY_BROKER_URL")
    celery_result_backend: str | None = Field(default=None, alias="CELERY_RESULT_BACKEND")

    # Local staging for uploaded DICOMs before the worker picks them up
    upload_dir: str = Field(default="uploads", alias="UPLOAD_DIR")

    # Local preprocessed X-ray PNGs (until S3); worker writes, API can read same disk
    preprocess_image_dir: str = Field(
        default="preprocessed",
        alias="PREPROCESS_IMAGE_DIR",
    )

    # Upload limits for POST /radiology/dicom/analyze
    max_dicom_files_per_request: int = Field(default=3, alias="MAX_DICOM_FILES_PER_REQUEST")
    max_dicom_file_size_mb: int = Field(default=10, alias="MAX_DICOM_FILE_SIZE_MB")

    # --- Auth (EmpowerX Node → AI; names must match Node .env) ---
    ai_service_key: str | None = Field(
        default=None,
        alias="AI_SERVICE_KEY",
        description="Shared server-to-server key (X-API-Key). Never expose to browsers.",
    )
    jwt_access_secret: str | None = Field(
        default=None,
        alias="JWT_ACCESS_SECRET",
        description="HS256 secret used by Node to sign access JWTs",
    )
    jwt_algorithm: str = Field(default="HS256", alias="JWT_ALGORITHM")

    # Comma-separated browser origins allowed by CORS
    cors_origins: str = Field(
        default=(
            "https://devempowerx.io,"
            "https://uatempowerx.io,"
            "https://ai.uatempowerx.io,"
            "http://localhost:3001,"
            "http://localhost:3000"
        ),
        alias="CORS_ORIGINS",
    )

    @property
    def cors_origins_list(self) -> list[str]:
        """Parsed CORS origins (no trailing slashes)."""
        return [
            origin.strip().rstrip("/")
            for origin in self.cors_origins.split(",")
            if origin.strip()
        ]

    @property
    def resolved_gemini_api_key(self) -> str | None:
        """Prefer GEMINI_API_KEY, fall back to GOOGLE_API_KEY."""
        return self.gemini_api_key or self.google_api_key

    def gemini_token_cost_rates(self):
        """Human-approved USD rates for Gemini token cost estimates."""
        from app.modules.radiology.token_cost import TokenCostRates

        return TokenCostRates.from_floats(
            input_per_million_usd=self.gemini_input_price_per_million_usd,
            output_per_million_usd=self.gemini_output_price_per_million_usd,
            cached_input_per_million_usd=self.gemini_cached_input_price_per_million_usd,
        )

    def anthropic_token_cost_rates(self):
        """Human-approved USD rates for Anthropic token cost estimates."""
        from app.modules.radiology.token_cost import TokenCostRates

        return TokenCostRates.from_floats(
            input_per_million_usd=self.anthropic_input_price_per_million_usd,
            output_per_million_usd=self.anthropic_output_price_per_million_usd,
            cached_input_per_million_usd=self.anthropic_cached_input_price_per_million_usd,
        )

    def token_cost_rates_for_model(self, model: str | None):
        """Pick Gemini vs Anthropic rates from the concrete model id."""
        name = (model or "").strip().lower()
        if "claude" in name or name.startswith("anthropic"):
            return self.anthropic_token_cost_rates()
        return self.gemini_token_cost_rates()

    @property
    def resolved_celery_broker_url(self) -> str:
        return self.celery_broker_url or self.redis_url

    @property
    def resolved_celery_result_backend(self) -> str:
        """Prefer explicit backend; else Redis DB 1 (keep broker on DB 0)."""
        if self.celery_result_backend:
            return self.celery_result_backend
        url = self.redis_url.rstrip("/")
        if url.endswith("/0"):
            return url[:-1] + "1"
        return f"{url}/1"


@lru_cache
def get_settings() -> Settings:
    return Settings()
