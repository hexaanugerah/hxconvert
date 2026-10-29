from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.core.storage import OUTPUT_DIR
from app.models.conversion import ConversionJob
from app.models.user import utcnow
from app.schemas.conversion import ConversionJobOut


def _load_list(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        data = json.loads(value)
    except (TypeError, ValueError):
        return []
    return [str(item) for item in data] if isinstance(data, list) else []


def _load_dict(value: str | None) -> dict:
    if not value:
        return {}
    try:
        data = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _load_pages(value: str | None) -> list[dict]:
    """Parse stored page metadata (a JSON list of objects), ordered by page."""
    if not value:
        return []
    try:
        data = json.loads(value)
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    pages: list[dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            number = int(item.get("page_number"))
        except (TypeError, ValueError):
            continue
        page = dict(item)
        page["page_number"] = number
        pages.append(page)
    pages.sort(key=lambda item: item["page_number"])
    return pages


def _load_batch_results(value: str | None) -> list[dict[str, Any]]:
    """Parse stored batch conversion results."""
    if not value:
        return []
    try:
        data = json.loads(value)
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return data


def _is_expired(job: ConversionJob) -> bool:
    if job.status == "expired":
        return True
    if job.expires_at is None:
        return False
    expires = job.expires_at
    if expires.tzinfo is None:
        from datetime import timezone

        expires = expires.replace(tzinfo=timezone.utc)
    return expires < utcnow()


def output_directory(job: ConversionJob) -> Path:
    if job.output_path:
        return Path(job.output_path).parent
    return OUTPUT_DIR / job.id


def page_preview_file(job: ConversionJob, page_number: int) -> Path | None:
    """Return the preview file recorded for a page, if it really exists."""
    for page in _load_pages(job.pages_json):
        if page["page_number"] != page_number:
            continue
        preview = page.get("preview")
        if not isinstance(preview, str) or not preview:
            return None
        candidate = output_directory(job) / "previews" / Path(preview).name
        return candidate if candidate.is_file() else None
    return None


def _page_out(job: ConversionJob, page: dict) -> dict:
    preview = page.get("preview")
    preview_url = None
    if isinstance(preview, str) and preview:
        if page_preview_file(job, page["page_number"]) is not None:
            preview_url = (
                f"/api/v1/conversions/{job.id}/pages/{page['page_number']}/preview"
            )
    payload: dict = {
        "page_number": page["page_number"],
        "status": page.get("status") or "pending",
        "preview_url": preview_url,
    }
    for key in (
        "width_pt",
        "height_pt",
        "orientation",
        "pixel_width",
        "pixel_height",
        "dpi",
        "error",
    ):
        if page.get(key) is not None:
            payload[key] = page[key]
    return payload


def job_to_out(job: ConversionJob) -> ConversionJobOut:
    downloadable = job.status == "completed" and bool(job.output_path) and not _is_expired(job)
    pages = [_page_out(job, page) for page in _load_pages(job.pages_json)]
    page_count = job.page_count if job.page_count and job.page_count > 0 else (len(pages) or None)
    batch_results = _load_batch_results(job.batch_results_json)
    for result in batch_results:
        if (
            downloadable
            and result.get("success")
            and result.get("output_filename")
        ):
            result["download_url"] = (
                f"/api/v1/conversions/{job.id}/files/"
                f"{int(result.get('index', 0))}/download"
            )

    # Never echo secret options (PDF passwords) back through the API.
    options = _load_dict(job.options_json)
    for key in list(options):
        if "password" in key.lower():
            options[key] = "********"

    return ConversionJobOut(
        id=job.id,
        tool=job.tool,
        original_filename=job.original_filename,
        input_format=job.input_format,
        output_format=job.output_format,
        status=job.status,  # type: ignore[arg-type]
        stage=job.stage or job.status,
        file_size=job.file_size,
        output_filename=job.output_filename,
        output_size=job.output_size,
        error_message=job.error_message,
        warnings=_load_list(job.warnings_json),
        options=options,
        created_at=job.created_at,
        started_at=job.started_at,
        completed_at=job.completed_at,
        expires_at=job.expires_at,
        download_url=f"/api/v1/conversions/{job.id}/download" if downloadable else None,
        page_count=page_count,
        pages=pages,
        batch_results=batch_results,
        zip_filename=job.zip_filename,
        has_zip=bool(job.zip_path),
    )
