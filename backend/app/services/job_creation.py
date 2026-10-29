"""Shared job creation used by every upload endpoint."""
from __future__ import annotations

from datetime import timedelta
import json
import time

from fastapi import Request, Response, UploadFile
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import AuthError, UnsupportedFormatError, ValidationAppError
from app.core.timing import log_stage
from app.core.security import get_or_create_guest_id
from app.core.storage import (
    extension_of,
    job_input_dir,
    job_temp_dir,
    remove_job_files,
    save_upload,
    stored_name,
    write_input_manifest,
)
from app.models.conversion import ConversionJob
from app.models.user import User, utcnow
from app.services.registry import ToolSpec, get_tool, require_available


def validate_options(tool: ToolSpec, raw: dict) -> dict:
    """Keep only declared options and coerce/validate their values."""
    resolved: dict = {}
    for option in tool.options:
        if option.name not in raw or raw[option.name] is None:
            resolved[option.name] = option.default
            continue
        value = raw[option.name]
        if option.type == "checkbox":
            resolved[option.name] = bool(value)
        elif option.type == "number":
            try:
                number = int(float(value))
            except (TypeError, ValueError):
                raise ValidationAppError(f"Opsi '{option.label}' harus berupa angka.")
            if option.min is not None:
                number = max(option.min, number)
            if option.max is not None:
                number = min(option.max, number)
            resolved[option.name] = number
        elif option.type == "select":
            allowed = {choice["value"] for choice in option.choices}
            resolved[option.name] = value if value in allowed else option.default
        else:
            resolved[option.name] = str(value)[:2000]
    return resolved


async def create_job(
    *,
    request: Request,
    response: Response,
    db: Session,
    user: User | None,
    tool_id: str,
    uploads: list[UploadFile],
    raw_options: dict | None = None,
) -> ConversionJob:
    tool = get_tool(tool_id)
    require_available(tool)

    if not uploads:
        raise ValidationAppError("Pilih minimal satu berkas untuk dikonversi.")
    if user is None and not settings.allow_anonymous:
        raise AuthError("Masuk untuk memulai konversi.")

    max_files = min(tool.max_files, settings.max_files_per_job)
    if len(uploads) > max_files:
        raise ValidationAppError(
            f"'{tool.label}' menerima maksimal {max_files} berkas per pekerjaan."
        )
    if len(uploads) < tool.min_files:
        raise ValidationAppError(
            f"'{tool.label}' memerlukan minimal {tool.min_files} berkas."
        )

    options = validate_options(tool, raw_options or {})
    guest_id = None if user else get_or_create_guest_id(request, response)
    if user is None and guest_id is None:
        raise AuthError("Masuk untuk memulai konversi.")

    job = ConversionJob(
        user_id=user.id if user else None,
        guest_id=guest_id,
        tool=tool.id,
        original_filename="",
        input_format="",
        output_format=tool.output,
        status="queued",
        options_json=json.dumps(options),
        expires_at=utcnow() + timedelta(hours=settings.result_ttl_hours),
    )
    db.add(job)
    db.flush()

    entries: list[dict[str, str]] = []
    total_size = 0
    try:
        input_dir = job_input_dir(job.id)
        for upload in uploads:
            ext = extension_of(upload.filename)
            if ext not in tool.inputs:
                raise UnsupportedFormatError(
                    f"'{tool.label}' tidak menerima berkas {ext or 'ini'}. "
                    f"Format yang didukung: {', '.join(tool.inputs)}."
                )
            destination = input_dir / stored_name(upload.filename)
            log_start = time.perf_counter()
            written = await save_upload(upload, destination)
            log_stage(
                "upload_file",
                (time.perf_counter() - log_start) * 1000,
                job_id=job.id,
                tool=tool.id,
                detail=f"size_mb={written / (1024 * 1024):.2f}",
            )
            total_size += written
            # Enforce the total batch size while writing, so an oversized
            # selection is rejected before the remaining files hit the disk.
            max_batch_bytes = settings.max_batch_size_mb * 1024 * 1024
            if total_size > max_batch_bytes:
                raise ValidationAppError(
                    f"Total ukuran batch melebihi batas {settings.max_batch_size_mb} MB. "
                    f"Ukuran total: {total_size / (1024 * 1024):.1f} MB"
                )
            entries.append(
                {
                    "stored": destination.name,
                    "original": (upload.filename or destination.name)[:255],
                }
            )
        if not entries:
            raise ValidationAppError("Tidak ada berkas yang diunggah.")
        
        # Keep the order the user selected; the worker reads it back.
        write_input_manifest(job.id, entries)
        job.original_filename = ", ".join(entry["original"] for entry in entries)[:255]
        job.input_format = extension_of(uploads[0].filename).lstrip(".")
        job.file_size = total_size
        job_temp_dir(job.id)
        db.commit()
    except Exception:
        db.rollback()
        remove_job_files(job.id)
        raise

    db.refresh(job)
    return job
