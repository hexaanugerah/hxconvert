from __future__ import annotations

from datetime import timedelta, timezone
import json
import mimetypes
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import current_identity, owned_job
from app.api.serializers import job_to_out, page_preview_file
from app.core.config import settings
from app.core.database import get_db
from app.core.errors import ExpiredError, ForbiddenError, NotFoundError, ValidationAppError
from app.core.rate_limit import enforce_rate_limit
from app.core.security import get_optional_user
from app.core.storage import (
    OUTPUT_DIR,
    ensure_within,
    job_inputs,
    job_temp_dir,
    remove_job_files,
    remove_job_outputs,
)
from app.models.conversion import ConversionJob
from app.models.user import User, utcnow
from app.repositories.conversions import (
    add_log,
    delete_job,
    identity_filter,
    identity_usage_since,
    job_stats,
    list_jobs,
)
from app.schemas.conversion import (
    ComparisonOut,
    ComparisonPageOut,
    ConversionJobOut,
    ConversionListOut,
    ConversionStatsOut,
    UsageOut,
)
from app.services.job_compare import build_comparison, page_image
from app.services.job_creation import create_job
from app.workers.runner import run_conversion_job

router = APIRouter(prefix="/conversions", tags=["conversions"])

MIME_BY_EXT = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "txt": "text/plain; charset=utf-8",
    "csv": "text/csv; charset=utf-8",
    "html": "text/html; charset=utf-8",
    "md": "text/markdown; charset=utf-8",
    "zip": "application/zip",
    "png": "image/png",
    "jpg": "image/jpeg",
    "webp": "image/webp",
}


def _parse_options(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        raise ValidationAppError("Opsi konversi harus berupa JSON yang valid.")
    if not isinstance(data, dict):
        raise ValidationAppError("Opsi konversi harus berupa objek JSON.")
    return data


def _expiry_passed(job: ConversionJob) -> bool:
    if not job.expires_at:
        return False
    expires = job.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return expires < utcnow()


def usage_out() -> UsageOut:
    return UsageOut(
        max_upload_mb=settings.max_upload_mb,
        max_files_per_job=settings.max_files_per_job,
        result_ttl_hours=settings.result_ttl_hours,
        allow_anonymous=settings.allow_anonymous,
        daily_job_quota=settings.daily_job_quota,
        guest_daily_job_quota=settings.guest_daily_job_quota,
        daily_megabyte_quota=settings.daily_megabyte_quota,
    )


@router.post("", response_model=ConversionJobOut, status_code=202)
async def create_conversion(
    background_tasks: BackgroundTasks,
    request: Request,
    response: Response,
    tool: str = Form(...),
    options: str = Form("{}"),
    files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
    user: User | None = Depends(get_optional_user),
    _rate=Depends(enforce_rate_limit),
):
    job = await create_job(
        request=request,
        response=response,
        db=db,
        user=user,
        tool_id=tool,
        uploads=files,
        raw_options=_parse_options(options),
    )
    background_tasks.add_task(run_conversion_job, job.id)
    return job_to_out(job)


@router.get("", response_model=ConversionListOut)
def history(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status: str | None = Query(
        None, pattern="^(queued|processing|completed|failed|cancelled|expired)$"
    ),
    tool: str | None = Query(None, max_length=64),
    q: str | None = Query(None, max_length=120),
    identity: tuple[str | None, str | None] = Depends(current_identity),
    db: Session = Depends(get_db),
):
    user_id, guest_id = identity
    items, total = list_jobs(
        db,
        user_id=user_id,
        guest_id=guest_id,
        page=page,
        page_size=page_size,
        status=status,
        tool=tool,
        query=q,
    )
    return ConversionListOut(
        items=[job_to_out(job) for job in items],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/stats", response_model=ConversionStatsOut)
def stats(
    identity: tuple[str | None, str | None] = Depends(current_identity),
    db: Session = Depends(get_db),
):
    user_id, guest_id = identity
    summary = job_stats(db, user_id=user_id, guest_id=guest_id)
    recent_items, _ = list_jobs(db, user_id=user_id, guest_id=guest_id, page=1, page_size=5)
    used_jobs, used_bytes = identity_usage_since(
        db,
        user_id=user_id,
        guest_id=guest_id,
        since=utcnow() - timedelta(hours=24),
    )
    finished = summary["completed"] + summary["failed"]
    return ConversionStatsOut(
        **summary,
        success_rate=round(summary["completed"] / finished * 100, 1) if finished else 0.0,
        recent=[job_to_out(job) for job in recent_items],
        usage=usage_out(),
        used_jobs_24h=used_jobs,
        used_megabytes_24h=round(used_bytes / (1024 * 1024), 2),
    )


@router.get("/{job_id}", response_model=ConversionJobOut)
def get_conversion(job: ConversionJob = Depends(owned_job)):
    return job_to_out(job)


@router.get("/{job_id}/download")
def download_conversion(
    job: ConversionJob = Depends(owned_job),
    db: Session = Depends(get_db),
):
    if job.status == "expired" or _expiry_passed(job):
        if job.status != "expired":
            job.status = "expired"
            db.commit()
        remove_job_files(job.id)
        raise ExpiredError("Tautan unduhan sudah kedaluwarsa. Silakan konversi ulang berkas.")

    if job.status == "failed":
        raise ValidationAppError(
            job.error_message or "Konversi gagal, sehingga tidak ada hasil untuk diunduh."
        )
    if job.status != "completed" or not job.output_path:
        raise ForbiddenError("Hasil belum siap.")

    path = Path(job.output_path)
    if not path.is_file():
        raise ExpiredError("Berkas hasil sudah tidak tersedia. Silakan konversi ulang.")
    try:
        path = ensure_within(path, OUTPUT_DIR)
    except Exception:
        raise ExpiredError("Berkas hasil sudah tidak tersedia.")

    extension = path.suffix.lstrip(".").lower()
    media_type = MIME_BY_EXT.get(extension) or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return FileResponse(
        path,
        media_type=media_type,
        filename=job.output_filename or path.name,
        headers={"Cache-Control": "no-store"},
    )


@router.get("/{job_id}/files/{file_index}/download")
def download_batch_file(
    file_index: int,
    job: ConversionJob = Depends(owned_job),
    db: Session = Depends(get_db),
):
    """Download one successful file from a multi-file batch.

    Batch conversions keep the combined ZIP as the primary result, but each
    successful file is also stored individually so it can be retrieved
    without unpacking the archive.
    """
    if job.status == "expired" or _expiry_passed(job):
        if job.status != "expired":
            job.status = "expired"
            db.commit()
        remove_job_files(job.id)
        raise ExpiredError("Tautan unduhan sudah kedaluwarsa. Silakan konversi ulang berkas.")

    if job.status == "failed":
        raise ValidationAppError(
            job.error_message or "Konversi gagal, sehingga tidak ada hasil untuk diunduh."
        )
    if job.status != "completed" or not job.batch_results_json:
        raise NotFoundError("Tidak ada hasil per berkas untuk konversi ini.")

    try:
        results = json.loads(job.batch_results_json)
    except ValueError:
        results = []
    entry = next(
        (
            item
            for item in results
            if int(item.get("index", -1)) == file_index
        ),
        None,
    )
    if not entry or not entry.get("success") or not entry.get("output_filename"):
        raise NotFoundError("Berkas hasil ini tidak tersedia.")

    filename = Path(str(entry["output_filename"])).name
    path = OUTPUT_DIR / job.id / "files" / str(file_index) / filename
    if not path.is_file():
        raise ExpiredError("Berkas hasil ini sudah tidak tersedia. Silakan konversi ulang.")
    try:
        path = ensure_within(path, OUTPUT_DIR)
    except Exception:
        raise NotFoundError("Berkas hasil ini tidak tersedia.")

    extension = path.suffix.lstrip(".").lower()
    media_type = MIME_BY_EXT.get(extension) or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return FileResponse(
        path,
        media_type=media_type,
        filename=filename,
        headers={"Cache-Control": "no-store"},
    )


@router.get("/{job_id}/download-zip")
def download_batch_zip(
    job: ConversionJob = Depends(owned_job),
    db: Session = Depends(get_db),
):
    """Download the ZIP archive for a batch conversion."""
    if job.status == "expired" or _expiry_passed(job):
        if job.status != "expired":
            job.status = "expired"
            db.commit()
        remove_job_files(job.id)
        raise ExpiredError("Tautan unduhan sudah kedaluwarsa. Silakan konversi ulang berkas.")

    if job.status == "failed":
        raise ValidationAppError(
            job.error_message or "Konversi gagal, sehingga tidak ada hasil untuk diunduh."
        )
    if job.status != "completed" or not job.zip_path:
        raise ForbiddenError("Tidak ada arsip ZIP untuk konversi ini.")

    path = Path(job.zip_path)
    if not path.is_file():
        raise ExpiredError("Arsip ZIP sudah tidak tersedia. Silakan konversi ulang.")
    try:
        path = ensure_within(path, OUTPUT_DIR)
    except Exception:
        raise ExpiredError("Arsip ZIP sudah tidak tersedia.")

    return FileResponse(
        path,
        media_type="application/zip",
        filename=job.zip_filename or "HXconvertX_Results.zip",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/{job_id}/pages/{page_number}/preview")
def page_preview(
    page_number: int,
    job: ConversionJob = Depends(owned_job),
    db: Session = Depends(get_db),
):
    """Serve the real rendered preview for one page of this job.

    Only files recorded during processing and still present on disk are served;
    there is no URL for a page that was never rendered.
    """
    if job.status == "expired" or _expiry_passed(job):
        if job.status != "expired":
            job.status = "expired"
            db.commit()
        remove_job_files(job.id)
        raise ExpiredError("Pratinjau sudah kedaluwarsa. Silakan konversi ulang berkas.")

    if page_number < 1:
        raise NotFoundError("Halaman tidak ditemukan.")
    path = page_preview_file(job, page_number)
    if path is None:
        raise NotFoundError("Pratinjau tidak tersedia untuk halaman ini.")
    try:
        path = ensure_within(path, OUTPUT_DIR)
    except Exception:
        raise NotFoundError("Pratinjau tidak tersedia untuk halaman ini.")

    return FileResponse(
        path,
        media_type="image/png",
        headers={"Cache-Control": "private, max-age=300"},
    )


@router.get("/{job_id}/compare", response_model=ComparisonOut)
def visual_comparison(
    job: ConversionJob = Depends(owned_job),
    db: Session = Depends(get_db),
):
    """Original vs result, page by page.

    The comparison is computed on first request and cached in the job directory;
    ``available: false`` explains why a job cannot be compared instead of failing.
    """
    if job.status == "expired" or _expiry_passed(job):
        if job.status != "expired":
            job.status = "expired"
            db.commit()
        remove_job_files(job.id)
        raise ExpiredError("Hasil sudah kedaluwarsa, sehingga perbandingan dihapus.")

    summary = build_comparison(job)
    base = f"/api/v1/conversions/{job.id}/compare/pages"
    pages = [
        ComparisonPageOut(
            page=page["page"],
            ratio=page["ratio"],
            mean_difference=page.get("mean_difference", 0.0),
            text_similarity=page.get("text_similarity"),
            status=page["status"],
            offset=page.get("offset") or [],
            original_url=f"{base}/{page['page']}/original",
            result_url=f"{base}/{page['page']}/result",
            difference_url=f"{base}/{page['page']}/difference",
        )
        for page in summary.get("pages", [])
    ]
    return ComparisonOut(
        available=bool(summary.get("available")),
        reason=summary.get("reason"),
        dpi=summary.get("dpi", 0),
        page_count=summary.get("page_count", 0),
        original_page_count=summary.get("original_page_count", 0),
        result_page_count=summary.get("result_page_count", 0),
        original_name=summary.get("original_name"),
        result_name=summary.get("result_name"),
        average_ratio=summary.get("average_ratio", 0.0),
        identical_pages=summary.get("identical_pages", 0),
        changed_pages=summary.get("changed_pages", []),
        truncated=bool(summary.get("truncated")),
        max_pages=summary.get("max_pages", 0),
        only_original=summary.get("only_original", []),
        only_result=summary.get("only_result", []),
        pages=pages,
    )


@router.get("/{job_id}/compare/pages/{page_number}/{kind}")
def comparison_image(
    page_number: int,
    kind: str,
    job: ConversionJob = Depends(owned_job),
):
    """Serve one cached comparison image: original, result or difference."""
    path = page_image(job.id, page_number, kind)
    if path is None:
        raise NotFoundError("Gambar perbandingan tidak tersedia untuk halaman ini.")
    return FileResponse(
        path,
        media_type="image/png",
        headers={"Cache-Control": "private, max-age=300"},
    )


@router.post("/{job_id}/cancel", response_model=ConversionJobOut)
def cancel_conversion(
    job: ConversionJob = Depends(owned_job),
    db: Session = Depends(get_db),
):
    """Request cancellation. A queued job stops immediately; a running one
    finishes inside its engine but its output is discarded by the worker."""
    if job.status in {"completed", "failed", "cancelled", "expired"}:
        raise ValidationAppError("Pekerjaan ini sudah selesai dan tidak dapat dibatalkan.")

    job.cancel_requested = True
    if job.status == "queued":
        job.status = "cancelled"
        job.stage = "cancelled"
        job.completed_at = utcnow()
        job.expires_at = utcnow() + timedelta(hours=settings.result_ttl_hours)
        # Keep the uploads so the owner can retry the job later.
        remove_job_outputs(job.id)
    add_log(db, job.id, "cancel_requested", "Cancellation requested by the owner.")
    db.commit()
    db.refresh(job)
    return job_to_out(job)


@router.post("/{job_id}/retry", response_model=ConversionJobOut, status_code=202)
def retry_conversion(
    background_tasks: BackgroundTasks,
    job: ConversionJob = Depends(owned_job),
    db: Session = Depends(get_db),
):
    """Queue a failed or cancelled job again from its retained uploads."""
    if job.status not in {"failed", "cancelled"}:
        raise ValidationAppError(
            "Hanya pekerjaan yang gagal atau dibatalkan yang dapat diulang."
        )

    inputs, _names, _digests = job_inputs(job.id)
    if not inputs:
        raise ValidationAppError(
            "Berkas sumber sudah dihapus karena hasilnya kedaluwarsa. Unggah ulang berkas "
            "untuk mencoba lagi."
        )

    job.status = "queued"
    job.stage = "queued"
    job.cancel_requested = False
    job.error_message = None
    job.warnings_json = None
    job.output_filename = None
    job.output_path = None
    job.output_size = None
    job.zip_path = None
    job.zip_filename = None
    job.batch_results_json = None
    job.page_count = None
    job.pages_json = None
    job.started_at = None
    job.completed_at = None
    job.expires_at = utcnow() + timedelta(hours=settings.result_ttl_hours)
    remove_job_outputs(job.id)
    job_temp_dir(job.id)
    add_log(db, job.id, "retried", "Job queued again by the owner.")
    db.commit()
    db.refresh(job)

    background_tasks.add_task(run_conversion_job, job.id)
    return job_to_out(job)


@router.delete("/{job_id}", status_code=204)
def delete_conversion(
    job: ConversionJob = Depends(owned_job),
    db: Session = Depends(get_db),
):
    remove_job_files(job.id)
    delete_job(db, job)
    return Response(status_code=204)


@router.delete("", status_code=200)
def clear_history(
    identity: tuple[str | None, str | None] = Depends(current_identity),
    db: Session = Depends(get_db),
):
    user_id, guest_id = identity
    jobs = list(db.scalars(select(ConversionJob).where(identity_filter(user_id, guest_id))).all())
    for job in jobs:
        remove_job_files(job.id)
        db.delete(job)
    db.commit()
    return {"deleted": len(jobs)}
