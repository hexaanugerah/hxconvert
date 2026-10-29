"""Headless LibreOffice conversion helper (optional external dependency)."""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import threading
import uuid

from app.core.config import settings
from app.core.errors import ConversionError
from app.services.capabilities import find_soffice

TIMEOUT_SECONDS = 120

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


def _soffice_command(soffice: str, profile_dir: Path, target: str, workdir: Path, input_path: Path) -> list[str]:
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
        str(input_path),
    ]


def _run_soffice(input_path: Path, target: str, workdir: Path, profile_dir: Path) -> Path:
    soffice = find_soffice()
    if not soffice:
        raise ConversionError(
            "LibreOffice is not installed on this server. Install it or use a tool that does not require it."
        )

    command = _soffice_command(soffice, profile_dir, target, workdir, input_path)
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
