"""Prometheus-style metrics endpoint.

Rendered as plain text (no prometheus_client dependency): durable counters come
from the database, per-stage timings from the in-process registry in
app.core.timing.
"""
from __future__ import annotations

import hmac
from collections import Counter

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import func, select

from app.core.config import settings
from app.core.database import SessionLocal
from app.core.errors import ForbiddenError
from app.core.timing import stage_metrics
from app.models.conversion import ConversionJob

router = APIRouter(tags=["metrics"])


def _label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _authorized(request: Request) -> bool:
    token = (settings.metrics_token or "").strip()
    if not token:
        return True
    supplied = request.headers.get("x-metrics-token", "").strip()
    if not supplied:
        header = request.headers.get("authorization", "").strip()
        if header.lower().startswith("bearer "):
            supplied = header[7:].strip()
    return hmac.compare_digest(supplied, token)


@router.get("/metrics", response_class=PlainTextResponse, include_in_schema=False)
def metrics(request: Request) -> PlainTextResponse:
    if not _authorized(request):
        raise ForbiddenError("Token metrik tidak valid.")

    lines: list[str] = []
    database_up = 1
    status_counts: Counter[str] = Counter()
    tool_counts: Counter[str] = Counter()
    try:
        with SessionLocal() as db:
            status_counts.update(
                {
                    status: count
                    for status, count in db.execute(
                        select(ConversionJob.status, func.count()).group_by(ConversionJob.status)
                    ).all()
                }
            )
            tool_counts.update(
                {
                    tool: count
                    for tool, count in db.execute(
                        select(ConversionJob.tool, func.count()).group_by(ConversionJob.tool)
                    ).all()
                }
            )
    except Exception:  # pragma: no cover - keep process metrics on DB trouble
        database_up = 0

    counts, totals_ms, outcomes = stage_metrics()

    lines.append("# HELP hx_database_up 1 when the job database answered the scrape.")
    lines.append("# TYPE hx_database_up gauge")
    lines.append(f"hx_database_up {database_up}")

    lines.append("# HELP hx_conversion_jobs Number of stored conversion jobs by status.")
    lines.append("# TYPE hx_conversion_jobs gauge")
    for status in sorted(status_counts):
        lines.append(f'hx_conversion_jobs{{status="{_label(str(status))}"}} {status_counts[status]}')

    active = status_counts.get("queued", 0) + status_counts.get("processing", 0)
    lines.append("# HELP hx_conversion_jobs_active Jobs queued or being processed now.")
    lines.append("# TYPE hx_conversion_jobs_active gauge")
    lines.append(f"hx_conversion_jobs_active {active}")

    lines.append("# HELP hx_conversion_jobs_total Conversion jobs by tool.")
    lines.append("# TYPE hx_conversion_jobs_total gauge")
    for tool in sorted(tool_counts):
        lines.append(f'hx_conversion_jobs_total{{tool="{_label(str(tool))}"}} {tool_counts[tool]}')

    lines.append("# HELP hx_job_outcomes_total Job outcomes recorded by this worker process.")
    lines.append("# TYPE hx_job_outcomes_total counter")
    for outcome in sorted(outcomes):
        lines.append(f'hx_job_outcomes_total{{outcome="{_label(outcome)}"}} {outcomes[outcome]}')

    lines.append("# HELP hx_stage_duration_seconds_total Time spent per conversion stage in this process.")
    lines.append("# TYPE hx_stage_duration_seconds_total counter")
    for stage in sorted(totals_ms):
        lines.append(
            f'hx_stage_duration_seconds_total{{stage="{_label(stage)}"}} {totals_ms[stage] / 1000:.3f}'
        )

    lines.append("# HELP hx_stage_runs_total Number of runs per conversion stage in this process.")
    lines.append("# TYPE hx_stage_runs_total counter")
    for stage in sorted(counts):
        lines.append(f'hx_stage_runs_total{{stage="{_label(stage)}"}} {counts[stage]}')

    return PlainTextResponse(
        "\n".join(lines) + "\n",
        headers={"Cache-Control": "no-store"},
    )
