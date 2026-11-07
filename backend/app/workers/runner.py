"""Background job execution and file retention cleanup.

Jobs run in a worker thread with a hard timeout. This is an in-process queue:
adequate for a single-node deployment, and replaceable with Celery/RQ + Redis
for horizontal scaling without changing the API contract.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import timedelta
import json
import logging
import shutil

from sqlalchemy import select

from app.core.config import settings
from app.core.database import SessionLocal
from app.core.errors import AppError
from app.core.storage import (
    job_input_dir,
    job_output_dir,
    job_temp_dir,
    read_input_manifest,
    remove_job_files,
    sanitize_filename,
)
from app.models.conversion import ConversionJob
from app.models.user import utcnow
from app.repositories.conversions import add_log, set_warnings
from app.services.base import ConversionContext
from app.core.timing import measure, stage_timer
from app.services.batch_conversion import process_batch_conversion
from app.services.document_inspection import build_page_records, preview_failures
from app.services.registry import get_tool
from app.services import result_cache

logger = logging.getLogger("docuconvert.worker")

ACTIVE_STATUSES = ("queued", "processing")


def _fail(db, job: ConversionJob, message: str, log_message: str | None = None) -> None:
    job.status = "failed"
    job.stage = "failed"
    job.error_message = message[:1000]
    job.completed_at = utcnow()
    job.expires_at = utcnow() + timedelta(hours=settings.result_ttl_hours)
    add_log(db, job.id, "failed", log_message or message)
    db.commit()


def _ordered_inputs(job_id: str) -> tuple[list, list[str]]:
    """Return uploaded files in the order the user selected them.

    Stored names are random UUIDs, so ``iterdir`` order is meaningless. The
    manifest written during upload is authoritative; legacy jobs fall back to
    a sorted listing.
    """
    input_dir = job_input_dir(job_id)
    manifest = read_input_manifest(job_id)
    inputs = []
    original_names: list[str] = []
    for entry in manifest:
        path = input_dir / entry["stored"]
        if path.is_file():
            inputs.append(path)
            original_names.append(entry["original"])
    if inputs:
        return inputs, original_names

    legacy = sorted(path for path in input_dir.iterdir() if path.is_file())
    return legacy, [path.name for path in legacy]


def _run_single_conversion(
    db,
    job: ConversionJob,
    tool,
    context: ConversionContext,
    inputs: list,
    options: dict,
    workdir,
) -> None:
    """Convert one uploaded file: result cache, execution, previews.

    This is deliberately separate from ``run_conversion_job`` so batch jobs
    never fall through into this path. Previously the code after the batch
    branch ran for every job, referencing a ``context`` that did not exist for
    batch jobs and marking every multi-file conversion as failed.
    """
    # Content-hash result cache: identical file + tool + options reuse
    # the previous conversion instead of recomputing it.
    cache_key = None
    reused = None
    try:
        if len(inputs) == 1 and result_cache.is_eligible(inputs[0]):
            with stage_timer("cache_key", job_id=job.id, tool=job.tool):
                cache_key = result_cache.compute_key(
                    tool.id, options, result_cache.file_sha256(inputs[0])
                )
            reused = result_cache.lookup(cache_key)
    except Exception:
        cache_key = None
        reused = None

    if reused:
        cached_path, cached_meta = reused
        filename = sanitize_filename(
            cached_meta.get("filename") or f"result.{tool.output}"
        )
        if not filename:
            filename = f"result.{tool.output}"
        output_dir = job_output_dir(job.id)
        target = output_dir / filename
        shutil.copy2(cached_path, target)
        job.output_filename = filename
        job.output_path = str(target)
        job.output_size = target.stat().st_size
        job.status = "completed"
        job.stage = "completed"
        job.completed_at = utcnow()
        job.expires_at = utcnow() + timedelta(hours=settings.result_ttl_hours)

        warnings = list(cached_meta.get("warnings") or [])
        try:
            with stage_timer("previews", job_id=job.id, tool=job.tool):
                records = build_page_records(
                    inputs,
                    output_path=target,
                    preview_dir=output_dir / "previews",
                )
        except Exception:
            logger.exception("Page inspection failed for job %s", job.id)
            records = []
            warnings.append("Pratinjau halaman tidak dapat dibuat untuk hasil ini.")
        warnings.extend(preview_failures(records))
        job.page_count = len(records) or None
        job.pages_json = json.dumps([record.as_dict() for record in records]) if records else None

        set_warnings(job, warnings)
        add_log(db, job.id, "completed", f"Reused cached conversion ({job.output_size} bytes).")
        db.commit()

        shutil.rmtree(job_input_dir(job.id), ignore_errors=True)
        shutil.rmtree(workdir, ignore_errors=True)
        return

    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"convert-{job.id[:8]}")
    future = executor.submit(
        measure,
        "conversion",
        lambda: tool.handler(context),
        job_id=job.id,
        tool=job.tool,
    )
    try:
        result = future.result(timeout=settings.conversion_timeout_seconds)
    except FutureTimeout:
        future.cancel()
        _fail(db, job, "Konversi memakan waktu terlalu lama dan dihentikan. Coba berkas yang lebih kecil.")
        remove_job_files(job.id)
        return
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    if not result.path.exists() or result.path.stat().st_size == 0:
        _fail(db, job, "Konverter menghasilkan berkas kosong.")
        remove_job_files(job.id)
        return

    filename = sanitize_filename(result.filename)
    if not filename:
        filename = f"result.{tool.output}"
    output_dir = job_output_dir(job.id)
    target = output_dir / filename
    with stage_timer("save_result", job_id=job.id, tool=job.tool):
        shutil.move(str(result.path), target)

    job.output_filename = filename
    job.output_path = str(target)
    job.output_size = target.stat().st_size
    job.status = "completed"
    job.stage = "completed"
    job.completed_at = utcnow()
    job.expires_at = utcnow() + timedelta(hours=settings.result_ttl_hours)

    # Store the successful result for future identical requests.
    if cache_key is not None:
        with stage_timer("cache_store", job_id=job.id, tool=job.tool):
            result_cache.store(
                cache_key,
                target,
                {"filename": filename, "mime": result.mime, "warnings": result.warnings},
            )

    # Inspect the real result (or the uploaded source) and render one
    # preview per page. Preview failures never fail the conversion.
    warnings = list(result.warnings)
    try:
        with stage_timer("previews", job_id=job.id, tool=job.tool):
            records = build_page_records(
                inputs,
                output_path=target,
                preview_dir=output_dir / "previews",
            )
    except Exception:
        logger.exception("Page inspection failed for job %s", job.id)
        records = []
        warnings.append("Pratinjau halaman tidak dapat dibuat untuk hasil ini.")
    warnings.extend(preview_failures(records))

    job.page_count = len(records) or None
    job.pages_json = json.dumps([record.as_dict() for record in records]) if records else None

    set_warnings(job, warnings)
    add_log(db, job.id, "completed", f"Conversion finished ({job.output_size} bytes).")
    db.commit()


def run_conversion_job(job_id: str) -> None:
    db = SessionLocal()
    job: ConversionJob | None = None
    try:
        job = db.get(ConversionJob, job_id)
        if job is None or job.status not in ACTIVE_STATUSES:
            return

        job.status = "processing"
        job.stage = "converting"
        job.started_at = utcnow()
        add_log(db, job.id, "started", f"Tool '{job.tool}' started.")
        db.commit()

        tool = get_tool(job.tool)
        available, reason = tool.availability()
        if not available:
            _fail(db, job, reason or "This tool is unavailable.", "Tool unavailable at execution time.")
            return

        inputs, original_names = _ordered_inputs(job.id)
        if not inputs:
            _fail(db, job, "Berkas yang diunggah sudah tidak tersedia. Silakan unggah ulang.")
            return

        options = json.loads(job.options_json or "{}")
        workdir = job_temp_dir(job.id)
        
        # Determine if this is a multi-input tool (processes all files at once)
        is_multi_input = tool.multiple
        
        if is_multi_input:
            # Multi-input tools (merge-pdf, merge-docx, image-to-pdf) process all files at once
            context = ConversionContext(
                inputs=inputs,
                original_names=original_names,
                options=options,
                workdir=workdir,
            )

            executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"convert-{job.id[:8]}")
            future = executor.submit(tool.handler, context)
            try:
                result = future.result(timeout=settings.conversion_timeout_seconds)
            except FutureTimeout:
                future.cancel()
                _fail(db, job, "Konversi memakan waktu terlalu lama dan dihentikan. Coba berkas yang lebih kecil.")
                remove_job_files(job.id)
                return
            finally:
                executor.shutdown(wait=False, cancel_futures=True)

            if not result.path.exists() or result.path.stat().st_size == 0:
                _fail(db, job, "Konverter menghasilkan berkas kosong.")
                remove_job_files(job.id)
                return

            filename = sanitize_filename(result.filename)
            if not filename:
                filename = f"result.{tool.output}"
            output_dir = job_output_dir(job.id)
            target = output_dir / filename
            shutil.move(str(result.path), target)

            job.output_filename = filename
            job.output_path = str(target)
            job.output_size = target.stat().st_size
            job.status = "completed"
            job.stage = "completed"
            job.completed_at = utcnow()
            job.expires_at = utcnow() + timedelta(hours=settings.result_ttl_hours)

            # Multi-input results (merged PDFs, image-to-PDF) are also paginated:
            # build the same page records and previews as single-file jobs so the
            # frontend reports real page counts and previews for them too.
            warnings_multi = list(result.warnings)
            try:
                records = build_page_records(
                    inputs,
                    output_path=target,
                    preview_dir=output_dir / "previews",
                )
            except Exception:
                logger.exception("Page inspection failed for job %s", job.id)
                records = []
                warnings_multi.append(
                    "Page previews could not be generated for this result."
                )
            warnings_multi.extend(preview_failures(records))

            job.page_count = len(records) or None
            job.pages_json = json.dumps([record.as_dict() for record in records]) if records else None

            set_warnings(job, warnings_multi)

            batch_results = []
            for i, orig_name in enumerate(original_names):
                batch_results.append({
                    "index": i,
                    "original_name": orig_name,
                    "success": True,
                    "output_filename": filename,
                    "output_mime": result.mime,
                    "error_message": None,
                    "warnings": result.warnings,
                })
            job.batch_results_json = json.dumps(batch_results)

            add_log(db, job.id, "completed", f"Multi-input conversion finished ({job.output_size} bytes).")
            db.commit()
        else:
            # Determine if this is a batch conversion (multiple input files)
            is_batch = len(inputs) > 1
            
            if is_batch:
                # Process each file (bounded worker pool) and create ZIP
                job_id = job.id
                def _stage(stage_name: str) -> None:
                    """Persist a progress stage while the batch job runs."""
                    job.stage = stage_name
                    db.commit()

                batch_result = measure(
                    "batch",
                    lambda: process_batch_conversion(
                        batch_id=job_id,
                        tool_id=job.tool,
                        input_files=inputs,
                        original_names=original_names,
                        options=options,
                        workdir=workdir,
                        on_stage=_stage,
                    ),
                    job_id=job_id,
                    tool=job.tool,
                    detail=lambda r: (
                        f"files={r.total_files} ok={r.successful} "
                        f"failed={r.failed}"
                    ),
                )
                
                if batch_result.successful == 0:
                    _fail(db, job, "Semua berkas gagal dikonversi.")
                    remove_job_files(job.id)
                    return
                
                # Store batch results
                job.batch_results_json = json.dumps([
                    {
                        "index": r.index,
                        "original_name": r.original_name,
                        "success": r.success,
                        "output_filename": r.output_filename,
                        "output_mime": r.output_mime,
                        "error_message": r.error_message,
                        "warnings": r.warnings,
                    }
                    for r in batch_result.file_results
                ])
                
                # If we have a ZIP, use that as the main output
                if batch_result.zip_path and batch_result.zip_path.exists():
                    job.output_filename = batch_result.zip_filename
                    job.output_path = str(batch_result.zip_path)
                    job.output_size = batch_result.zip_path.stat().st_size
                    job.zip_path = str(batch_result.zip_path)
                    job.zip_filename = batch_result.zip_filename
                elif batch_result.successful == 1:
                    # Single successful file - use its output
                    success_result = next(r for r in batch_result.file_results if r.success)
                    job.output_filename = success_result.output_filename
                    job.output_path = str(success_result.output_path) if success_result.output_path else None
                    job.output_size = success_result.output_path.stat().st_size if success_result.output_path and success_result.output_path.exists() else None
                
                job.status = "completed"
                job.stage = "completed"
                job.completed_at = utcnow()
                job.expires_at = utcnow() + timedelta(hours=settings.result_ttl_hours)
                
                # Collect all warnings
                all_warnings = []
                for r in batch_result.file_results:
                    all_warnings.extend(r.warnings)
                if batch_result.failed > 0:
                    all_warnings.append(f"{batch_result.failed} dari {batch_result.total_files} berkas gagal dikonversi.")
                
                set_warnings(job, all_warnings)
                add_log(db, job.id, "completed", f"Batch conversion finished ({batch_result.successful}/{batch_result.total_files} successful).")
                db.commit()
            else:
                # Single file conversion: cache lookup, execution, previews.
                context = ConversionContext(
                    inputs=inputs,
                    original_names=original_names,
                    options=options,
                    workdir=workdir,
                )
                _run_single_conversion(
                    db, job, tool, context, inputs, options, workdir
                )

        shutil.rmtree(job_input_dir(job.id), ignore_errors=True)
        shutil.rmtree(workdir, ignore_errors=True)
    except AppError as exc:
        if job is not None:
            _fail(db, job, exc.message)
    except Exception:
        logger.exception("Conversion job %s failed unexpectedly", job_id)
        if job is not None:
            db.rollback()
            try:
                job = db.get(ConversionJob, job_id)
                if job is not None:
                    _fail(
                        db,
                        job,
                        "Konversi gagal secara tak terduga. Berkas mungkin rusak atau menggunakan fitur yang tidak didukung.",
                    )
            except Exception:  # pragma: no cover
                logger.exception("Could not record failure for job %s", job_id)
        remove_job_files(job_id)
    finally:
        db.close()


def cleanup_expired_jobs() -> int:
    """Mark expired jobs and delete their stored files."""
    db = SessionLocal()
    removed = 0
    try:
        now = utcnow()
        jobs = list(
            db.scalars(
                select(ConversionJob).where(
                    ConversionJob.expires_at.is_not(None),
                    ConversionJob.expires_at < now,
                    ConversionJob.status != "expired",
                )
            ).all()
        )
        for job in jobs:
            remove_job_files(job.id)
            job.status = "expired"
            job.output_path = None
            job.output_size = None
            add_log(db, job.id, "expired", "Files were deleted according to the retention policy.")
            removed += 1
        if jobs:
            db.commit()
    except Exception:  # pragma: no cover
        logger.exception("Cleanup failed")
        db.rollback()
    finally:
        db.close()

    # Expired conversion-results cache entries follow the same retention idea.
    try:
        removed += result_cache.purge_expired()
    except Exception:  # pragma: no cover
        logger.exception("Result cache cleanup failed")
    return removed


async def cleanup_loop() -> None:
    while True:
        await asyncio.sleep(max(60, settings.cleanup_interval_minutes * 60))
        try:
            count = await asyncio.to_thread(cleanup_expired_jobs)
            if count:
                logger.info("Cleaned up %s expired conversion job(s)", count)
        except Exception:  # pragma: no cover
            logger.exception("Expired job cleanup iteration failed")
