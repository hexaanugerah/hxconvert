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

    app_name: str = "HXconvertX"
    environment: str = "development"
    api_prefix: str = "/api/v1"

    secret_key: str = "dev-only-change-me"
    access_token_expire_minutes: int = 60 * 24 * 7

    database_url: str = f"sqlite:///{(BACKEND_DIR / 'docuconvert.db').as_posix()}"
    # TiDB Cloud (MySQL protocol) requires TLS on its public endpoint. Set
    # DATABASE_SSL=true there; the CA defaults to the Debian/Ubuntu bundle.
    database_ssl: bool = False
    database_ssl_ca: str | None = None
    database_ssl_verify_identity: bool = True

    # Cross-site cookies are needed only when the browser calls the API on a
    # different site (e.g. *.vercel.app -> *.up.railway.app). The Vercel
    # rewrite proxy and custom domains under one apex keep cookies same-site,
    # where the default "lax" is the safest choice.
    cookie_samesite: str = "lax"
    cookie_secure: bool | None = None

    storage_dir: Path = PROJECT_DIR / "storage"
    max_upload_mb: int = 200
    max_files_per_job: int = 50
    max_batch_size_mb: int = 1024
    result_ttl_hours: int = 24
    cleanup_interval_minutes: int = 15
    conversion_timeout_seconds: int = 300
    preview_dpi: int = 96
    preview_max_width: int = 1400
    preview_max_pages: int = 60
    # Visual original-vs-result comparison. Pages are rendered at compare_dpi and
    # only the first compare_max_pages are compared, to bound CPU and disk use.
    compare_dpi: int = 100
    compare_max_pages: int = 25
    allow_anonymous: bool = True
    rate_limit_per_minute: int = 60

    # Per-identity quota for the rolling last 24 hours. ``0`` disables a limit.
    # Registered users usually get more headroom than anonymous visitors.
    daily_job_quota: int = 100
    guest_daily_job_quota: int = 20
    daily_megabyte_quota: int = 2048

    # Optional shared secret for GET /api/v1/metrics. Empty keeps metrics
    # readable without a token (they contain aggregate counts, no file/user data).
    metrics_token: str | None = None

    # Performance tuning. CPU-bound conversions run in a process pool (real
    # parallelism across cores); subprocess tools (LibreOffice/Word) keep the
    # thread pool. ``0`` means auto: half the cores, capped at six.
    conversion_workers: int = 0
    # Persisted results and the batch ZIP normally each read the converted bytes,
    # so a 20-file all-image batch writes several hundred MB once for the downloads
    # and once for the archive. When the workdir and the output volume are the same,
    # ``result_persist_mode`` controls how that re-read is avoided:
    #   ``"hardlink"`` (default) — the download and the ZIP member point at the
    #     same bytes; cleanup of the temporary workdir still leaves the results
    #     intact. Falls back to a copy automatically when a hard link cannot be
    #     created (different volumes, permissions, or the source was already moved).
    #   ``"copy"`` — the old behaviour, one copy per result, no hard links.
    result_persist_mode: str = "hardlink"

    # Content-hash cache for single-file conversions. A batch conversion first
    # checks this cache so repeated conversions of identical inputs (e.g. common
    # templates re-uploaded many times) are served from the cache instead of
    # re-running the converter. Cached results are copied into the cache with
    # shutil.copy2; the persist hard-link optimisation above does not apply to
    # cache entries (the cache owns its own copy), and cache hits are excluded
    # from quota/major-metric counting already by the job-creation path.
    result_cache_hours: int = 6
    result_cache_max_file_mb: int = 50
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

    @field_validator("database_url", mode="before")
    @classmethod
    def _normalize_database_url(cls, value: str) -> str:
        """Accept the URLs cloud providers hand out.

        Railway/Heroku use ``postgres://`` and TiDB examples often use
        ``mysql://``; SQLAlchemy needs an explicit driver in both cases.
        """
        url = str(value or "").strip()
        if url.startswith("mysql://"):
            return "mysql+pymysql://" + url[len("mysql://"):]
        if url.startswith("postgres://"):
            return "postgresql+psycopg://" + url[len("postgres://"):]
        if url.startswith("postgresql://"):
            return "postgresql+psycopg://" + url[len("postgresql://"):]
        return url

    @field_validator("cookie_samesite")
    @classmethod
    def _validate_cookie_samesite(cls, value: str) -> str:
        normalized = (value or "lax").strip().lower()
        if normalized not in {"lax", "strict", "none"}:
            raise ValueError("COOKIE_SAMESITE must be lax, strict or none")
        return normalized

    @property
    def cookie_is_secure(self) -> bool:
        # SameSite=None is rejected by browsers unless the cookie is Secure.
        if self.cookie_samesite == "none":
            return True
        if self.cookie_secure is not None:
            return self.cookie_secure
        return self.environment == "production"

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
