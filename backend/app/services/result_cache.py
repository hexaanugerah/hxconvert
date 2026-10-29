"""Content-hash result cache for single-file conversions.

A conversion is reused only when ALL of the following match:
- SHA-256 of the uploaded file content (never the filename)
- tool id
- resolved conversion options (canonical JSON)
- engine version constant (bump when conversion code changes effect)

Cache files live under storage/cache/<key>/ and expire after
``settings.result_cache_hours``. Every lookup validates that the cached file
still exists. Nothing is shared that was not produced from the exact same
input bytes, so no document can leak between jobs.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import time
from pathlib import Path

from app.core.config import settings

# Bump this when any conversion engine behaviour changes (handlers, mode
# defaults, library behaviour changes) so stale results are never returned.
ENGINE_VERSION = "2026-10-27.1"


def cache_root() -> Path:
    path = settings.storage_dir / "cache"
    path.mkdir(parents=True, exist_ok=True)
    return path


def file_sha256(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def is_eligible(input_path: Path) -> bool:
    try:
        return input_path.stat().st_size <= settings.result_cache_max_file_mb * 1024 * 1024
    except OSError:
        return False


def compute_key(tool_id: str, options: dict, input_sha256: str) -> str:
    canonical = json.dumps(options, sort_keys=True, ensure_ascii=False)
    material = "\n".join((tool_id, ENGINE_VERSION, canonical, input_sha256))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def _entry_dir(key: str) -> Path:
    return cache_root() / key


def lookup(key: str) -> tuple[Path, dict] | None:
    """Return (cached_result_path, meta) when a valid cache entry exists."""
    entry = _entry_dir(key)
    meta_path = entry / "meta.json"
    result_files = [path for path in entry.glob("result.*")] if entry.is_dir() else []
    if not result_files:
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        shutil.rmtree(entry, ignore_errors=True)
        return None

    created = float(meta.get("created_at", 0) or 0)
    if time.time() - created > settings.result_cache_hours * 3600:
        shutil.rmtree(entry, ignore_errors=True)
        return None

    result_path = result_files[0]
    if not result_path.is_file() or result_path.stat().st_size == 0:
        shutil.rmtree(entry, ignore_errors=True)
        return None
    return result_path, meta


def store(key: str, source_result: Path, meta: dict) -> Path | None:
    """Copy a successful result into the cache. Never raises."""
    entry = _entry_dir(key)
    try:
        entry.mkdir(parents=True, exist_ok=True)
        target = entry / f"result{source_result.suffix.lower() or '.bin'}"
        shutil.copy2(source_result, target)
        payload = dict(meta)
        payload["created_at"] = time.time()
        (entry / "meta.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
        return target
    except Exception:  # pragma: no cover - the cache must never break a job
        shutil.rmtree(entry, ignore_errors=True)
        return None


def purge_expired() -> int:
    """Delete cache entries older than the configured lifetime."""
    deadline = settings.result_cache_hours * 3600
    now = time.time()
    removed = 0
    try:
        for entry in cache_root().iterdir():
            if not entry.is_dir():
                continue
            meta_path = entry / "meta.json"
            try:
                created = float(json.loads(meta_path.read_text(encoding="utf-8")).get("created_at", 0))
            except Exception:
                created = entry.stat().st_mtime
            if now - created > deadline:
                shutil.rmtree(entry, ignore_errors=True)
                removed += 1
    except OSError:
        return removed
    return removed
