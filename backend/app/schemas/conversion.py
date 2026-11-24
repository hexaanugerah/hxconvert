from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel


JobStatus = Literal["queued", "processing", "completed", "failed", "cancelled", "expired"]


class ConversionJobOut(BaseModel):
    id: str
    tool: str
    original_filename: str
    input_format: str
    output_format: str
    status: JobStatus
    stage: str | None = None
    file_size: int
    output_filename: str | None = None
    output_size: int | None = None
    error_message: str | None = None
    warnings: list[str] = []
    options: dict[str, Any] = {}
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    expires_at: datetime | None = None
    download_url: str | None = None
    page_count: int | None = None
    pages: list[dict[str, Any]] = []
    # Batch conversion fields
    batch_results: list[dict[str, Any]] = []
    zip_filename: str | None = None
    has_zip: bool = False
    compare_url: str | None = None


class ComparisonPageOut(BaseModel):
    page: int
    ratio: float
    mean_difference: float = 0.0
    text_similarity: float | None = None
    status: str
    offset: list[int] = []
    original_url: str
    result_url: str
    difference_url: str


class ComparisonOut(BaseModel):
    available: bool
    reason: str | None = None
    dpi: int = 0
    page_count: int = 0
    original_page_count: int = 0
    result_page_count: int = 0
    original_name: str | None = None
    result_name: str | None = None
    average_ratio: float = 0.0
    identical_pages: int = 0
    changed_pages: list[int] = []
    truncated: bool = False
    max_pages: int = 0
    only_original: list[int] = []
    only_result: list[int] = []
    pages: list[ComparisonPageOut] = []


class ConversionListOut(BaseModel):
    items: list[ConversionJobOut]
    total: int
    page: int
    page_size: int


class UsageOut(BaseModel):
    max_upload_mb: int
    max_files_per_job: int
    result_ttl_hours: int
    allow_anonymous: bool
    daily_job_quota: int = 0
    guest_daily_job_quota: int = 0
    daily_megabyte_quota: int = 0


class ConversionStatsOut(BaseModel):
    total: int
    completed: int
    failed: int
    processing: int
    success_rate: float
    processed_bytes: int
    recent: list[ConversionJobOut]
    usage: UsageOut
    used_jobs_24h: int = 0
    used_megabytes_24h: float = 0.0


class ToolOptionOut(BaseModel):
    name: str
    label: str
    type: str
    default: Any = None
    choices: list[dict[str, Any]] = []
    help: str | None = None
    placeholder: str | None = None
    min: int | None = None
    max: int | None = None


class ToolOut(BaseModel):
    id: str
    label: str
    description: str
    category: str
    inputs: list[str]
    output: str
    available: bool
    unavailable_reason: str | None = None
    multiple: bool
    min_files: int
    max_files: int
    processing_note: str | None = None
    options: list[ToolOptionOut]


class HealthOut(BaseModel):
    status: str
    version: str
    database: str
    capabilities: dict[str, Any]