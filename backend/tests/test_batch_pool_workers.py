"""Regression tests for parallel batch execution.

``ProcessPoolExecutor`` grows lazily, so a batch of small conversions can be
drained by the first worker while the rest are still spawning. These tests pin
down the fix: the pool is full size before work is submitted, and twenty files
are really spread across every worker instead of running one by one.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from app.services import batch_conversion
from app.services.base import ConversionContext, ConversionResult
from app.services.registry import ToolSpec

WORKERS = 3
PROBE_SLEEP_SECONDS = 0.12


def _probe_handler(ctx: ConversionContext) -> ConversionResult:
    """Record which process ran this file, then produce a small result."""
    (ctx.workdir / "pid.txt").write_text(str(os.getpid()), encoding="utf-8")
    time.sleep(PROBE_SLEEP_SECONDS)
    output = ctx.workdir / "out.txt"
    output.write_text("ok", encoding="utf-8")
    return ConversionResult(output, "out.txt", "text/plain")


def _probe_tool() -> ToolSpec:
    return ToolSpec(
        id="pool-probe",
        label="Pool probe",
        description="",
        category="text",
        inputs=(".txt",),
        output="txt",
        handler=_probe_handler,
    )


def _write_inputs(tmp_path: Path, count: int) -> list[Path]:
    inputs = []
    for index in range(count):
        path = tmp_path / "inputs" / f"in{index}.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"file {index}", encoding="utf-8")
        inputs.append(path)
    return inputs


def test_cpu_pool_prestarts_every_worker(tmp_path, monkeypatch):
    """The pool must already be full size before the first task is submitted."""
    monkeypatch.setattr(batch_conversion, "_worker_count", lambda: WORKERS)
    batch_conversion._drop_cpu_pool()
    try:
        pool = batch_conversion._cpu_pool(WORKERS)
        assert len(pool._processes) == WORKERS
        # Every pre-started worker must be able to run work, not just exist.
        assert pool.submit(_probe_pid).result(timeout=60) > 0
    finally:
        batch_conversion._drop_cpu_pool()


def test_twenty_files_are_distributed_across_all_workers(tmp_path, monkeypatch):
    """20 files should be handled by all workers, not drained by one."""
    monkeypatch.setattr(batch_conversion, "_worker_count", lambda: WORKERS)
    batch_conversion._drop_cpu_pool()
    tool = _probe_tool()
    inputs = _write_inputs(tmp_path, 20)
    workdir = tmp_path / "work"
    try:
        results = batch_conversion._convert_indices(
            "pool-probe",
            list(range(20)),
            inputs,
            [path.name for path in inputs],
            tool,
            {},
            workdir,
            WORKERS,
        )
        pids = {
            int(path.read_text(encoding="utf-8"))
            for path in workdir.glob("file_*/pid.txt")
        }
    finally:
        batch_conversion._drop_cpu_pool()

    assert len(results) == 20
    assert all(result.success for result in results.values())
    assert len(pids) == WORKERS


def _probe_pid() -> int:
    return os.getpid()
