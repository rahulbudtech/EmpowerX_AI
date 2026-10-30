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

    # --- LLM (Gemini primary; fallback TBD) ---
    llm_provider: str = Field(default="gemini", description="Primary provider id")
    llm_model: str = Field(
        default="gemini-3.6-flash",
        description="Primary model name for findings",
    )
    gemini_api_key: str | None = Field(default=None, alias="GEMINI_API_KEY")
    google_api_key: str | None = Field(default=None, alias="GOOGLE_API_KEY")

    # Optional fallback (not wired yet — reserved)
    llm_fallback_provider: str | None = None
    llm_fallback_model: str | None = None

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
