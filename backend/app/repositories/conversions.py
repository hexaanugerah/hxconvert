"""Database access for conversion jobs and logs."""
from __future__ import annotations

import json

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models.conversion import ConversionJob, ConversionLog


def add_log(db: Session, job_id: str, event_type: str, message: str) -> None:
    db.add(ConversionLog(conversion_job_id=job_id, event_type=event_type[:32], message=message[:2000]))


def get_job(db: Session, job_id: str) -> ConversionJob | None:
    return db.get(ConversionJob, job_id)


def identity_filter(user_id: str | None, guest_id: str | None):
    if user_id:
        return ConversionJob.user_id == user_id
    if guest_id:
        return ConversionJob.guest_id == guest_id
    return ConversionJob.id.is_(None)


def can_access(job: ConversionJob, user_id: str | None, guest_id: str | None) -> bool:
    if job.user_id:
        return bool(user_id) and job.user_id == user_id
    if job.guest_id:
        return bool(guest_id) and job.guest_id == guest_id
    return False


def list_jobs(
    db: Session,
    *,
    user_id: str | None,
    guest_id: str | None,
    page: int = 1,
    page_size: int = 20,
    status: str | None = None,
    tool: str | None = None,
    query: str | None = None,
) -> tuple[list[ConversionJob], int]:
    condition = identity_filter(user_id, guest_id)
    filters = [condition]
    if status:
        filters.append(ConversionJob.status == status)
    if tool:
        filters.append(ConversionJob.tool == tool)
    if query:
        like = f"%{query.strip()}%"
        filters.append(
            or_(
                ConversionJob.original_filename.ilike(like),
                ConversionJob.output_filename.ilike(like),
                ConversionJob.tool.ilike(like),
            )
        )

    total = db.scalar(select(func.count(ConversionJob.id)).where(*filters)) or 0
    statement = (
        select(ConversionJob)
        .where(*filters)
        .order_by(ConversionJob.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    items = list(db.scalars(statement).all())
    return items, total


def identity_usage_since(
    db: Session,
    *,
    user_id: str | None,
    guest_id: str | None,
    since,
    exclude_job_id: str | None = None,
) -> tuple[int, int]:
    """Number of jobs and total uploaded bytes for an identity since ``since``.

    ``exclude_job_id`` skips a job that is already in the session (the one being
    checked after its upload was written) so it does not count itself.
    """
    conditions = [identity_filter(user_id, guest_id), ConversionJob.created_at >= since]
    if exclude_job_id:
        conditions.append(ConversionJob.id != exclude_job_id)
    jobs = db.scalar(select(func.count(ConversionJob.id)).where(*conditions)) or 0
    size = db.scalar(
        select(func.coalesce(func.sum(ConversionJob.file_size), 0)).where(*conditions)
    ) or 0
    return int(jobs), int(size)


def job_stats(db: Session, *, user_id: str | None, guest_id: str | None) -> dict:
    condition = identity_filter(user_id, guest_id)
    total = db.scalar(select(func.count(ConversionJob.id)).where(condition)) or 0
    completed = db.scalar(
        select(func.count(ConversionJob.id)).where(condition, ConversionJob.status == "completed")
    ) or 0
    failed = db.scalar(
        select(func.count(ConversionJob.id)).where(condition, ConversionJob.status == "failed")
    ) or 0
    processing = db.scalar(
        select(func.count(ConversionJob.id)).where(condition, ConversionJob.status.in_(("queued", "processing")))
    ) or 0
    processed_bytes = db.scalar(
        select(func.coalesce(func.sum(ConversionJob.output_size), 0)).where(condition, ConversionJob.status == "completed")
    ) or 0
    return {
        "total": total,
        "completed": completed,
        "failed": failed,
        "processing": processing,
        "processed_bytes": int(processed_bytes),
    }


def delete_job(db: Session, job: ConversionJob) -> None:
    db.delete(job)
    db.commit()


def set_warnings(job: ConversionJob, warnings: list[str]) -> None:
    job.warnings_json = json.dumps(warnings[:25]) if warnings else None
