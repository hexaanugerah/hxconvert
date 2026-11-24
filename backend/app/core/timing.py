"""Structured stage timing for conversion jobs.

Every hot path logs one line per stage so slow stages can be found from logs
instead of guessed. Format (single line, easy to filter):

    job=a1b2c3dd tool=pdf-to-word stage=convert ms=1234.5

Stage names follow the conversion pipeline so runs can be compared:

    upload_file, manifest, conversion, save_result, previews, zip, cache_hit
"""
from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterable, TypeVar

logger = logging.getLogger("docuconvert.timing")

T = TypeVar("T")

# Per-process counters exported by GET /api/v1/metrics. They reset on restart;
# job counts come from the database and stay durable. CPU-bound tools that run
# in a worker process update their own copy, so treat these as this process's
# view (the parent process records queueing, previews, saves and cache hits).
_metrics_lock = threading.Lock()
_stage_count: dict[str, int] = defaultdict(int)
_stage_ms: dict[str, float] = defaultdict(float)
_outcomes: dict[str, int] = defaultdict(int)


def record_outcome(outcome: str) -> None:
    with _metrics_lock:
        _outcomes[outcome] += 1


def stage_metrics() -> tuple[dict[str, int], dict[str, float], dict[str, int]]:
    """Snapshot of (stage counts, stage total ms, job outcomes)."""
    with _metrics_lock:
        return dict(_stage_count), dict(_stage_ms), dict(_outcomes)


def _log(job_id: str | None, tool: str | None, stage: str, elapsed_ms: float, detail: str = "") -> None:
    with _metrics_lock:
        _stage_count[stage] += 1
        _stage_ms[stage] += elapsed_ms
    parts = [f"job={job_id or '-'}", f"tool={tool or '-'}", f"stage={stage}", f"ms={elapsed_ms:.1f}"]
    if detail:
        parts.append(detail)
    logger.info(" ".join(parts))


@contextmanager
def stage_timer(
    stage: str,
    *,
    job_id: str | None = None,
    tool: str | None = None,
    detail: Callable[[], str] | None = None,
):
    """Log how long the wrapped block took.

    Usage:
        with stage_timer("convert", job_id=job.id, tool=job.tool):
            result = tool.handler(context)
    """
    start = time.perf_counter()
    try:
        yield
    finally:
        elapsed = (time.perf_counter() - start) * 1000
        try:
            extra = detail() if detail else ""
        except Exception:  # pragma: no cover - detail must never break logging
            extra = ""
        _log(job_id, tool, stage, elapsed, extra.lstrip())


def measure(
    stage: str,
    fn: Callable[[], T],
    *,
    job_id: str | None = None,
    tool: str | None = None,
    detail: Callable[[T], str] | None = None,
) -> T:
    """Run fn and log its duration plus optional detail computed from the result."""
    start = time.perf_counter()
    try:
        value = fn()
    except Exception:
        _log(job_id, tool, stage, (time.perf_counter() - start) * 1000, "error")
        raise
    elapsed = (time.perf_counter() - start) * 1000
    try:
        extra = detail(value) if detail else ""
    except Exception:  # pragma: no cover
        extra = ""
    _log(job_id, tool, stage, elapsed, extra.lstrip())
    return value


def file_size_mb_attrs(path: Path) -> str:
    try:
        return f"size_mb={path.stat().st_size / (1024 * 1024):.2f}"
    except OSError:
        return ""


def log_stage(
    stage: str,
    elapsed_ms: float,
    *,
    job_id: str | None = None,
    tool: str | None = None,
    detail: str = "",
) -> None:
    """Log one already-measured stage (useful inside async code)."""
    _log(job_id, tool, stage, elapsed_ms, detail)


def summarize(pairs: Iterable[tuple[str, float]]) -> str:
    """Render [(stage, ms), ...] as a compact sortable detail string."""
    return " ".join(f"{name}={value:.0f}ms" for name, value in pairs)


def flatten(stages: dict[str, Any]) -> list[tuple[str, float]]:
    """Turn a {'stage': {'ms': ...}} mapping into (stage, ms) pairs."""
    out = []
    for name, value in stages.items():
        if isinstance(value, dict) and "ms" in value:
            out.append((name, float(value["ms"])))
    return out
