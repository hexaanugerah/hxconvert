from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel


JobStatus = Literal["queued", "processing", "completed", "failed", "expired"]


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


class ConversionStatsOut(BaseModel):
    total: int
    completed: int
    failed: int
    processing: int
    success_rate: float
    processed_bytes: int
    recent: list[ConversionJobOut]
    usage: UsageOut


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