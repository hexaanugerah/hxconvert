"""Application configuration loaded from environment variables."""
from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[2]
PROJECT_DIR = BACKEND_DIR.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BACKEND_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "XConvert"
    environment: str = "development"
    api_prefix: str = "/api/v1"

    secret_key: str = "dev-only-change-me"
    access_token_expire_minutes: int = 60 * 24 * 7

    database_url: str = f"sqlite:///{(BACKEND_DIR / 'docuconvert.db').as_posix()}"

    storage_dir: Path = PROJECT_DIR / "storage"
    max_upload_mb: int = 200
    max_files_per_job: int = 15
    max_batch_size_mb: int = 1024
    result_ttl_hours: int = 24
    cleanup_interval_minutes: int = 15
    conversion_timeout_seconds: int = 300
    preview_dpi: int = 96
    preview_max_width: int = 1400
    preview_max_pages: int = 60
    allow_anonymous: bool = True
    rate_limit_per_minute: int = 60

    # Performance tuning. CPU-bound conversions run in a process pool (real
    # parallelism across cores); subprocess tools (LibreOffice/Word) keep the
    # thread pool. ``0`` means auto: one worker less than the core count.
    conversion_workers: int = 0
    max_concurrent_uploads: int = 3
    zip_compression_level: int = 6
    result_cache_hours: int = 6
    result_cache_max_file_mb: int = 50

    cors_origins: str = (
        "http://localhost:3000,http://127.0.0.1:3000,"
        "http://localhost:3001,http://127.0.0.1:3001"
    )

    libreoffice_path: str | None = None
    word_path: str | None = None
    tesseract_cmd: str | None = None

    @field_validator("storage_dir", mode="before")
    @classmethod
    def _resolve_storage(cls, value: str | Path) -> Path:
        path = Path(value)
        return path if path.is_absolute() else (PROJECT_DIR / path)

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.storage_dir.mkdir(parents=True, exist_ok=True)
    (settings.storage_dir / "inputs").mkdir(exist_ok=True)
    (settings.storage_dir / "outputs").mkdir(exist_ok=True)
    return settings


settings = get_settings()
