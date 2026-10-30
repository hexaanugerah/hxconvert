"""Headless LibreOffice conversion helper (optional external dependency)."""
from __future__ import annotations

from pathlib import Path
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from app.core.config import settings
from app.core.errors import ConversionError
from app.services.capabilities import find_soffice

TIMEOUT_SECONDS = 120
# One soffice invocation can take a list of files: the batch pays LibreOffice's
# startup once instead of once per file.
BATCH_TIMEOUT_PER_FILE = 45
MAX_BATCH_TIMEOUT = 900
# Stagger parallel soffice sessions so simultaneous cold starts (font cache)
# do not all contend at the same moment.
_SESSION_STAGGER_SECONDS = 0.75
# Profile/font caches stay warm roughly this long after the last soffice run;
# within that window, launching sessions simultaneously is safe (and faster).
_SOFFICE_WARM_WINDOW_SECONDS = 15 * 60
_last_soffice_activity = 0.0


def _soffice_is_warm() -> bool:
    return (time.monotonic() - _last_soffice_activity) < _SOFFICE_WARM_WINDOW_SECONDS

# Reusing profiles avoids LibreOffice's first-start cost (font cache creation,
# ~1-2 s) on every conversion. One profile per potential concurrent worker,
# guarded by a lock, keeps parallel headless runs from fighting over the same
# user installation. Cross-process access is avoided by construction: all
# conversions in this app run inside one process.
_PROFILE_COUNT = min(4, max(1, (settings.conversion_workers or 1)))
_pool_lock = threading.Lock()
_profile_locks: list[threading.Lock] | None = None


def _profile_pool_dir() -> Path:
    directory = settings.storage_dir / "tmp" / "lo-profiles"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _get_profile_locks() -> list[threading.Lock]:
    global _profile_locks
    with _pool_lock:
        if _profile_locks is None:
            _profile_locks = [threading.Lock() for _ in range(_PROFILE_COUNT)]
        return _profile_locks


def _soffice_command(soffice: str, profile_dir: Path, target: str, workdir: Path, inputs: list[Path]) -> list[str]:
    """Build the headless soffice command line."""
    profile_url = profile_dir.as_posix().lstrip("/") if sys.platform.startswith("win") else profile_dir.as_posix()
    return [
        soffice,
        "--headless",
        "--norestore",
        "--nolockcheck",
        "--nodefault",
        "--nofirststartwizard",
        f"-env:UserInstallation=file:///{profile_url}",
        "--convert-to",
        target,
        "--outdir",
        str(workdir),
        *[str(path) for path in inputs],
    ]


def _run_soffice(input_path: Path, target: str, workdir: Path, profile_dir: Path) -> Path:
    soffice = find_soffice()
    if not soffice:
        raise ConversionError(
            "LibreOffice is not installed on this server. Install it or use a tool that does not require it."
        )

    command = _soffice_command(soffice, profile_dir, target, workdir, [input_path])
    creationflags = 0x08000000 if sys.platform.startswith("win") else 0
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
            creationflags=creationflags,
        )
    except subprocess.TimeoutExpired as exc:
        raise ConversionError("LibreOffice conversion timed out.") from exc
    except OSError as exc:
        raise ConversionError("LibreOffice could not be started.") from exc

    expected_extension = target.split(":")[0]
    output = workdir / f"{input_path.stem}.{expected_extension}"
    if not output.exists():
        # Some filters name the output differently; take the newest matching file.
        candidates = sorted(
            workdir.glob(f"*.{expected_extension}"), key=lambda path: path.stat().st_mtime, reverse=True
        )
        if candidates:
            output = candidates[0]
    if not output.exists():
        detail = (result.stderr or result.stdout or "").strip()[:400]
        raise ConversionError(f"LibreOffice failed to produce a {expected_extension.upper()} file. {detail}")
    return output


def convert_with_soffice(input_path: Path, target: str, workdir: Path) -> Path:
    """Convert a document to `target` (for example 'pdf') using a pooled profile."""
    locks = _get_profile_locks()
    for lock in locks:
        if lock.acquire(blocking=False):
            try:
                profile = _profile_pool_dir() / f"profile-{locks.index(lock)}"
                profile.mkdir(parents=True, exist_ok=True)
                return _run_soffice(input_path, target, workdir, profile)
            except ConversionError:
                # A corrupt/hung shared profile: fall back to a fresh isolated one.
                profile = workdir / f"lo-profile-{uuid.uuid4().hex[:8]}"
                profile.mkdir(parents=True, exist_ok=True)
                return _run_soffice(input_path, target, workdir, profile)
            finally:
                lock.release()
    # Every pooled profile is busy: wait for the first one instead of spawning
    # another heavyweight instance.
    with locks[0]:
        profile = _profile_pool_dir() / "profile-0"
        profile.mkdir(parents=True, exist_ok=True)
        try:
            return _run_soffice(input_path, target, workdir, profile)
        except ConversionError:
            profile = workdir / f"lo-profile-{uuid.uuid4().hex[:8]}"
            profile.mkdir(parents=True, exist_ok=True)
            return _run_soffice(input_path, target, workdir, profile)


@dataclass(frozen=True)
class SofficeBatchOutcome:
    """Conversion result for one file inside a shared soffice invocation."""

    output: Path | None = None
    error: str | None = None


def _batch_timeout(file_count: int) -> float:
    return min(
        TIMEOUT_SECONDS + BATCH_TIMEOUT_PER_FILE * file_count,
        MAX_BATCH_TIMEOUT,
        max(45, settings.conversion_timeout_seconds - 30),
    )


def _session_count(file_count: int, max_sessions: int | None = None) -> int:
    """How many heavy converter sessions a batch should run at once."""
    configured = max_sessions
    if configured is None:
        configured = max(1, int(settings.conversion_workers or 1))
    if configured <= 1 or file_count <= 1:
        return 1
    # No point in more sessions than files; keep the box usable by not
    # exceeding the CPU budget either.
    return max(1, min(int(configured), os.cpu_count() or 2, file_count))


def _shard_indices(file_count: int, sessions: int) -> list[list[int]]:
    """Round-robin split so heavy files spread evenly across sessions."""
    shards = [list(range(index, file_count, sessions)) for index in range(sessions)]
    return [shard for shard in shards if shard]


def _move_output(source: Path, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.unlink(missing_ok=True)
    shutil.move(str(source), str(destination))
    return destination


def convert_batch_with_soffice(
    jobs: list[tuple[Path, Path]],
    target: str,
    workdir: Path,
    lock: threading.Lock | None = None,
) -> list[SofficeBatchOutcome]:
    """Convert several files through one single LibreOffice invocation.

    ``jobs`` are ``(input, output)`` pairs. Files that LibreOffice cannot
    convert are reported in their outcome instead of aborting the rest, so the
    caller can fall back to the per-file handler for exactly those files.
    Only infrastructure failures (LibreOffice missing, hang) raise.
    """
    soffice = find_soffice()
    if not soffice:
        raise ConversionError(
            "LibreOffice is not installed on this server. Install it or use a tool that does not require it."
        )
    if not jobs:
        return []
    global _last_soffice_activity
    _last_soffice_activity = time.monotonic()

    expected_extension = target.split(":")[0]
    workdir.mkdir(parents=True, exist_ok=True)
    # Staging with unique names avoids the "same stem -> overwritten output"
    # risk of a shared output directory.
    stage = workdir / "stage"
    out_dir = workdir / "out"
    stage.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    staged: list[Path] = []
    for index, (input_path, _) in enumerate(jobs):
        staged_path = stage / f"batch-{index:04d}{input_path.suffix.lower()}"
        shutil.copyfile(input_path, staged_path)
        staged.append(staged_path)

    # The profile lock is held for the duration of the whole invocation, so
    # concurrent jobs do not fight over the same LibreOffice profile.
    locks = _get_profile_locks()
    selected = lock if lock is not None else locks[0]

    def run_with_profile(profile_dir: Path):
        profile_url = profile_dir.as_posix().lstrip("/")
        full_command = [
            soffice,
            "--headless",
            "--norestore",
            "--nolockcheck",
            "--nodefault",
            "--nofirststartwizard",
            f"-env:UserInstallation=file:///{profile_url}",
            "--convert-to",
            target,
            "--outdir",
            str(out_dir),
            *[str(path) for path in staged],
        ]
        creationflags = 0x08000000 if sys.platform.startswith("win") else 0
        try:
            return subprocess.run(
                full_command,
                capture_output=True,
                text=True,
                timeout=_batch_timeout(len(jobs)),
                creationflags=creationflags,
            )
        except subprocess.TimeoutExpired as exc:
            raise ConversionError("LibreOffice batch conversion timed out.") from exc
        except OSError as exc:
            raise ConversionError("LibreOffice could not be started.") from exc

    with selected:
        profile = _profile_pool_dir() / f"profile-{locks.index(selected)}"
        profile.mkdir(parents=True, exist_ok=True)
        try:
            result = run_with_profile(profile)
        except ConversionError:
            # A corrupt/hung shared profile: retry once with a fresh isolated one.
            profile = workdir / f"lo-profile-{uuid.uuid4().hex[:8]}"
            profile.mkdir(parents=True, exist_ok=True)
            result = run_with_profile(profile)

    outcomes: list[SofficeBatchOutcome] = []
    for index, (_, output) in enumerate(jobs):
        produced = out_dir / f"batch-{index:04d}.{expected_extension}"
        if produced.exists() and produced.stat().st_size > 0:
            outcomes.append(SofficeBatchOutcome(output=_move_output(produced, output)))
            continue
        outcomes.append(
            SofficeBatchOutcome(error=f"LibreOffice failed to convert this file to {expected_extension.upper()}.")
        )
    return outcomes


def convert_parallel_with_soffice(
    jobs: list[tuple[Path, Path]],
    target: str,
    workdir: Path,
    max_sessions: int | None = None,
) -> list[SofficeBatchOutcome]:
    """Convert several files through several LibreOffice processes at once.

    LibreOffice startup (font cache, first run ~1-2 s) is paid once per
    session while the actual conversions run concurrently. Each session gets
    its own pooled profile lock, mirroring the per-worker profile isolation.
    """
    if not jobs:
        return []
    sessions = _session_count(len(jobs), max_sessions)
    locks = _get_profile_locks()
    if sessions <= 1 or len(locks) <= 1:
        return convert_batch_with_soffice(jobs, target, workdir)

    shards = _shard_indices(len(jobs), sessions)
    outcomes: list[SofficeBatchOutcome | None] = [None] * len(jobs)

    def run_session(position: int, shard_jobs: list, delay: float):
        # Stagger only while LibreOffice state is cold: simultaneous cold
        # starts contend on font-cache building. Warm profiles launch at once.
        if delay > 0:
            time.sleep(delay)
        return convert_batch_with_soffice(
            shard_jobs, target, workdir / f"session_{position}", lock=locks[position % len(locks)]
        )

    with ThreadPoolExecutor(max_workers=len(shards)) as pool:
        futures = {
            pool.submit(
                run_session,
                position,
                [jobs[index] for index in globals_],
                0.0 if _soffice_is_warm() else position * _SESSION_STAGGER_SECONDS,
            ): globals_
            for position, globals_ in enumerate(shards)
        }
        for future in as_completed(futures):
            globals_ = futures[future]
            try:
                shard_outcomes = future.result()
            except ConversionError:
                shard_outcomes = [SofficeBatchOutcome()] * len(globals_)
            for index, outcome in zip(globals_, shard_outcomes):
                outcomes[index] = outcome
    return [outcome for outcome in outcomes if outcome is not None]
