"""DOCX -> PDF through Microsoft Word automation (Windows only).

Word is the reference renderer for Word documents, so exporting through it keeps
the original pagination, fonts, headers/footers, tables, images and floating
objects. Automation runs in a separate short-lived process:

- COM apartment handling stays out of the FastAPI worker threads;
- a hung Word instance can be killed together with its helper process;
- no ``shell=True`` and no user-controlled file names reach a shell.
"""
from __future__ import annotations

from pathlib import Path
import atexit
import importlib.util
import itertools
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from functools import lru_cache

import logging

from docx import Document
from docx.oxml.ns import qn

from app.core.config import BACKEND_DIR, settings
from app.core.errors import ConversionError
from app.services.capabilities import find_winword, word_available
from app.services.office_ops import _shard_indices, _session_count

logger = logging.getLogger("docuconvert.word")

TIMEOUT_SECONDS = 120
# One Word session for a whole batch: per-file cost drops to just open+export,
# instead of paying Word startup/shutdown for every single document.
BATCH_TIMEOUT_PER_FILE = 45
MAX_BATCH_TIMEOUT = 900
# Stagger parallel Word sessions so their cold starts do not all contend at
# the same moment (see convert_parallel_with_word).
_SESSION_STAGGER_SECONDS = 1.0
# Word/AV caches stay warm roughly this long after the last Word session;
# within that window, launching sessions simultaneously is safe (and faster).
_WORD_WARM_WINDOW_SECONDS = 15 * 60
_last_word_activity = 0.0

_POOL_WORKER_PATH = Path(__file__).with_name("word_pool_worker.py")
_POOL_START_TIMEOUT = 45.0
# Word instances stay in RAM (~150 MB each), so shut the pool down after this
# much idle time and pay the startup again on the next batch.
_POOL_IDLE_SECONDS = 20 * 60
_pool_lock = threading.Lock()
_pool: "_WordPool | None" = None


def _word_is_warm() -> bool:
    return (time.monotonic() - _last_word_activity) < _WORD_WARM_WINDOW_SECONDS


def _word_pool_enabled() -> bool:
    value = os.environ.get("DOCUCONVERT_WORD_POOL", "1").strip().lower()
    return value not in {"0", "false", "off"}


def _word_pool_size() -> int:
    raw = os.environ.get("DOCUCONVERT_WORD_POOL_SIZE", "").strip()
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    return min(4, max(2, os.cpu_count() or 2))
_CREATE_NO_WINDOW = 0x08000000
_HELPER_PATH = Path(__file__).with_name("word_com_helper.py")


@dataclass(frozen=True)
class WordBatchOutcome:
    """Result of exporting one file inside a shared Word session."""

    output: Path | None = None
    error: str | None = None


def convert_with_word(input_path: Path, workdir: Path) -> Path:
    """Export a DOCX to PDF using the installed Microsoft Word."""
    if not word_available():
        raise ConversionError("Microsoft Word is not available on this server.")

    output = workdir / f"{input_path.stem}.pdf"
    output.unlink(missing_ok=True)
    command = [
        sys.executable,
        str(_HELPER_PATH),
        str(input_path.resolve()),
        str(output.resolve()),
    ]
    process = subprocess.Popen(
        command,
        cwd=str(BACKEND_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        creationflags=_CREATE_NO_WINDOW if sys.platform.startswith("win") else 0,
    )
    try:
        stdout, stderr = process.communicate(timeout=TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        _kill_process_tree(process.pid)
        process.communicate()
        raise ConversionError("Microsoft Word export timed out.")
    except OSError as exc:
        raise ConversionError("Microsoft Word could not be started.") from exc

    if process.returncode != 0:
        detail = (stderr or stdout or "").strip().splitlines()
        message = detail[-1][:400] if detail else "unknown error"
        raise ConversionError(f"Microsoft Word failed to export the PDF. {message}")
    if not output.is_file() or output.stat().st_size < 100:
        raise ConversionError("Microsoft Word did not produce a PDF file.")
    with output.open("rb") as handle:
        if not handle.read(5).startswith(b"%PDF"):
            raise ConversionError("Microsoft Word produced an invalid PDF file.")
    return output


def _valid_pdf(path: Path) -> bool:
    try:
        if not path.is_file() or path.stat().st_size < 100:
            return False
        with path.open("rb") as handle:
            return handle.read(5).startswith(b"%PDF")
    except OSError:
        return False


def convert_batch_with_word(
    jobs: list[tuple[Path, Path]], workdir: Path
) -> list[WordBatchOutcome]:
    """Export many DOCX files sequentially through one Word instance.

    Starting Word costs ~2 s and quitting it another ~2.5 s per file, which
    dominates small documents. Reusing a single instance removes that overhead
    while keeping the isolation (one helper process, killable on timeout).
    Per-file failures are reported in the returned outcomes; only an
    infrastructure failure raises, so the caller can fall back.
    """
    return _word_batch_session(jobs, workdir, 0)


def _word_batch_session(
    jobs: list[tuple[Path, Path]], workdir: Path, session_index: int
) -> list[WordBatchOutcome]:
    """One manifest + one Word instance for a list of files."""
    global _last_word_activity
    _last_word_activity = time.monotonic()
    if not word_available():
        raise ConversionError("Microsoft Word is not available on this server.")
    if not jobs:
        return []
    if not sys.platform.startswith("win"):  # pragma: no cover - Word is Windows-only
        raise ConversionError("Microsoft Word automation is only available on Windows.")

    workdir.mkdir(parents=True, exist_ok=True)
    manifest_path = workdir / f"word_batch_manifest_{session_index}.json"
    results_path = workdir / f"word_batch_results_{session_index}.json"
    results_path.unlink(missing_ok=True)
    for _, output in jobs:
        output.unlink(missing_ok=True)
    manifest_path.write_text(
        json.dumps(
            {
                "jobs": [
                    {"input": str(input_path.resolve()), "output": str(output.resolve())}
                    for input_path, output in jobs
                ]
            }
        ),
        encoding="utf-8",
    )

    command = [
        sys.executable,
        str(_HELPER_PATH),
        "--batch",
        str(manifest_path),
        str(results_path),
    ]
    process = subprocess.Popen(
        command,
        cwd=str(BACKEND_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        creationflags=_CREATE_NO_WINDOW,
    )
    timeout = min(
        TIMEOUT_SECONDS + BATCH_TIMEOUT_PER_FILE * len(jobs),
        MAX_BATCH_TIMEOUT,
        max(45, settings.conversion_timeout_seconds - 30),
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_process_tree(process.pid)
        process.communicate()
        raise ConversionError("Microsoft Word batch export timed out.")
    except OSError as exc:
        raise ConversionError("Microsoft Word could not be started.") from exc

    entries: list[dict] | None = None
    if results_path.is_file():
        try:
            payload = json.loads(results_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = None
        if isinstance(payload, dict) and isinstance(payload.get("results"), list):
            entries = payload["results"]

    if entries is None:
        detail = (stderr or stdout or "").strip().splitlines()
        message = detail[-1][:400] if detail else "unknown error"
        raise ConversionError(f"Microsoft Word failed to export the batch. {message}")

    return [
        _pool_entry_outcome(entries[index] if index < len(entries) else None, output)
        for index, (_, output) in enumerate(jobs)
    ]


def convert_parallel_with_word(
    jobs: list[tuple[Path, Path]], workdir: Path, max_sessions: int | None = None
) -> list[WordBatchOutcome]:
    """Export many DOCX files through several Word instances running at once.

    The persistent pool of warm Word instances is tried first: it removes the
    per-batch Word startup/shutdown cost entirely. When the pool is not
    available (disabled, non-Windows, Word registry damaged), the legacy path
    below spawns short-lived sessions — still faster than one Word per file.
    """
    if not jobs:
        return []
    pool_outcomes = convert_with_word_pool(jobs, workdir)
    if pool_outcomes is not None:
        return pool_outcomes

    sessions = _session_count(len(jobs), max_sessions)
    if sessions <= 1:
        return convert_batch_with_word(jobs, workdir)

    shards = _shard_indices(len(jobs), sessions)
    outcomes: list[WordBatchOutcome | None] = [None] * len(jobs)

    def run_session(position: int, shard_jobs: list, delay: float) -> list:
        # Stagger only while Word state is cold: simultaneous cold starts
        # contend on font caches/AV. Once the server has processed Word
        # documents recently, the cache is warm and instant launch is faster.
        if delay > 0:
            time.sleep(delay)
        return _word_batch_session(shard_jobs, workdir / f"session_{position}", position)

    with ThreadPoolExecutor(max_workers=len(shards)) as pool:
        futures = {
            pool.submit(
                run_session,
                position,
                [jobs[index] for index in globals_],
                0.0 if _word_is_warm() else position * _SESSION_STAGGER_SECONDS,
            ): globals_
            for position, globals_ in enumerate(shards)
        }
        for future in as_completed(futures):
            globals_ = futures[future]
            try:
                shard_outcomes = future.result()
            except ConversionError:
                # The whole session failed; leave entries empty so the caller
                # falls back to the per-file handler for those files.
                shard_outcomes = [WordBatchOutcome()] * len(globals_)
            for index, outcome in zip(globals_, shard_outcomes):
                outcomes[index] = outcome
    return [outcome for outcome in outcomes if outcome is not None]


class _PoolWorker:
    """One long-lived helper process holding one warm Word instance."""

    _ids = itertools.count(1)

    def __init__(self) -> None:
        self.responses: queue.Queue[dict] = queue.Queue()
        self.proc: subprocess.Popen | None = None

    def start(self) -> None:
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        self.proc = subprocess.Popen(
            [sys.executable, str(_POOL_WORKER_PATH)],
            cwd=str(BACKEND_DIR),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            creationflags=_CREATE_NO_WINDOW if sys.platform.startswith("win") else 0,
            env=env,
        )
        reader = threading.Thread(target=_pool_read_loop, args=(self,), daemon=True)
        reader.start()
        item = self.responses.get(timeout=_POOL_START_TIMEOUT)
        if item.get("event") != "ready" or self.proc.poll() is not None:
            self.close()
            raise ConversionError("Word pool worker failed to start.")

    def request(self, jobs: list[dict[str, str]], timeout: float) -> list[dict]:
        """Send one batch and wait for the reply line."""
        proc = self.proc
        if proc is None or proc.poll() is not None:
            raise ConversionError("Word pool worker is not running.")
        request_id = next(_PoolWorker._ids)
        try:
            proc.stdin.write(json.dumps({"id": request_id, "jobs": jobs}) + "\n")
            proc.stdin.flush()
        except (OSError, ValueError) as exc:
            raise ConversionError("Word pool worker pipe broke.") from exc
        try:
            item = self.responses.get(timeout=timeout)
        except queue.Empty as exc:
            raise ConversionError("Word pool worker timed out.") from exc
        if item.get("event") == "dead":
            raise ConversionError("Word pool worker exited unexpectedly.")
        payload = item.get("payload")
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            raise ConversionError("Word pool worker sent a malformed response.")
        return payload["results"]

    def closed(self) -> bool:
        proc = self.proc
        return proc is None or proc.poll() is not None

    def close(self) -> None:
        """Stop the worker; closing stdin lets it quit Word gracefully."""
        proc = self.proc
        if proc is None:
            return
        try:
            if proc.stdin is not None and not proc.stdin.closed:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=8)
        except Exception:
            _kill_process_tree(proc.pid)


def _pool_read_loop(worker: _PoolWorker) -> None:  # pragma: no cover - thread
    proc = worker.proc
    if proc is None or proc.stdout is None:
        return
    for line in proc.stdout:
        text = line.strip()
        if text == "READY":
            worker.responses.put({"event": "ready"})
            continue
        try:
            payload = json.loads(text)
        except ValueError:
            continue
        worker.responses.put({"event": "result", "payload": payload})
    worker.responses.put({"event": "dead"})


class _WordPool:
    """A fixed set of warm Word workers shared across batch conversions."""

    def __init__(self, size: int) -> None:
        self.size = size
        self.free: queue.Queue[_PoolWorker] = queue.Queue()
        self.last_used = time.monotonic()

    def touch(self) -> None:
        self.last_used = time.monotonic()

    def start(self) -> None:
        failures: list[str] = []

        def spawn(index: int) -> None:
            try:
                if index:
                    time.sleep(0.3 * index)
                worker = _PoolWorker()
                worker.start()
                self.free.put(worker)
            except Exception as exc:
                failures.append(str(exc))

        threads = [
            threading.Thread(target=spawn, args=(index,), daemon=True)
            for index in range(self.size)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        if self.free.qsize() == 0:
            raise ConversionError(
                "Word pool could not start: " + (failures[0] if failures else "unknown error")
            )

    def release(self, worker: _PoolWorker | None) -> None:
        if worker is None:
            return
        if not worker.closed():
            worker.responses = queue.Queue()  # drop any stale queued lines
            self.free.put(worker)
            self.touch()
        else:
            self.replace()

    def replace(self) -> None:
        """Spawn a fresh worker in the background to refill the pool."""

        def spawn() -> None:
            try:
                worker = _PoolWorker()
                worker.start()
                self.free.put(worker)
                self.touch()
            except Exception:
                pass  # next acquire will see an empty pool and fail fast

        threading.Thread(target=spawn, daemon=True).start()

    def acquire(self) -> _PoolWorker:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                worker = self.free.get(timeout=2)
            except queue.Empty:
                continue
            if not worker.closed():
                self.touch()
                return worker
            self.replace()
        raise ConversionError("Word pool has no available workers.")

    def shutdown(self) -> None:
        while True:
            try:
                worker = self.free.get_nowait()
            except queue.Empty:
                worker = None
            if worker is None:
                break
            worker.close()


def _ensure_word_pool() -> _WordPool:
    global _pool
    with _pool_lock:
        if _pool is not None:
            return _pool
        pool = _WordPool(size=_word_pool_size())
        pool.start()
        _pool = pool
        atexit.register(_shutdown_word_pool)
        threading.Thread(target=_pool_janitor, args=(pool,), daemon=True).start()
        return pool


def _shutdown_word_pool() -> None:  # pragma: no cover - process exit path
    global _pool
    if _pool is not None:
        _pool.shutdown()
        _pool = None


def _pool_janitor(pool: _WordPool) -> None:  # pragma: no cover - thread
    """Close workers after a long idle period; the next batch respawns them."""
    global _pool
    while True:
        time.sleep(120)
        if time.monotonic() - pool.last_used < _POOL_IDLE_SECONDS:
            continue
        if pool.free.qsize() != pool.size:
            continue
        pool.touch()  # avoid re-checking while workers drain
        pool.shutdown()
        with _pool_lock:
            if _pool is pool:
                _pool = None
        return


def _pool_entry_outcome(entry: object, output: Path) -> WordBatchOutcome:
    if not isinstance(entry, dict) or not entry.get("ok"):
        error = (
            str(entry.get("error"))
            if isinstance(entry, dict)
            else "Microsoft Word did not report a result for this file."
        )
        return WordBatchOutcome(error=(error or "Microsoft Word failed to export this file.")[:400])
    if not _valid_pdf(output):
        return WordBatchOutcome(error="Microsoft Word did not produce a valid PDF file.")
    return WordBatchOutcome(output=output)


def convert_with_word_pool(
    jobs: list[tuple[Path, Path]], workdir: Path
) -> list[WordBatchOutcome] | None:
    """Export files through the pool of warm Word instances.

    Returns ``None`` when the pool is not usable right now (disabled,
    non-Windows, Word missing) so the caller can fall back to short-lived
    sessions without treating it as a conversion error.
    """
    global _last_word_activity
    if not jobs:
        return []
    if not _word_pool_enabled() or not word_available():
        return None
    if not sys.platform.startswith("win"):  # pragma: no cover - Word is Windows-only
        return None
    _last_word_activity = time.monotonic()
    Path(workdir).mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    try:
        pool = _ensure_word_pool()
    except ConversionError as exc:
        logger.info("Word pool unavailable, using legacy sessions: %s", exc)
        return None

    shards = _shard_indices(len(jobs), pool.size)
    outcomes: list[WordBatchOutcome | None] = [None] * len(jobs)

    def run(position: int, globals_: list[int]) -> list[WordBatchOutcome]:
        shard_jobs = [jobs[index] for index in globals_]
        worker: _PoolWorker | None = None
        request_started = time.monotonic()
        try:
            worker = pool.acquire()
            entries = worker.request(
                [
                    {
                        "input": str(input_path.resolve()),
                        "output": str(output.resolve()),
                    }
                    for input_path, output in shard_jobs
                ],
                timeout=min(
                    TIMEOUT_SECONDS + BATCH_TIMEOUT_PER_FILE * len(shard_jobs),
                    MAX_BATCH_TIMEOUT,
                    max(45, settings.conversion_timeout_seconds - 30),
                ),
            )
            logger.info(
                "Word pool request ok: %d file(s) in %.1fs",
                len(shard_jobs),
                time.monotonic() - request_started,
            )
        except ConversionError as exc:
            logger.warning("Word pool request failed: %s", exc)
            # A hung or exited worker must never be offered again: shut it
            # down and spawn a replacement while conversion falls back for
            # these files.
            if worker is not None:
                worker.close()
                pool.replace()
            else:
                pool.touch()
            return [WordBatchOutcome()] * len(shard_jobs)
        pool.release(worker)
        results: list[WordBatchOutcome] = []
        for slot, (_, output) in enumerate(shard_jobs):
            entry = entries[slot] if slot < len(entries) else None
            results.append(_pool_entry_outcome(entry, output))
        return results

    with ThreadPoolExecutor(max_workers=len(shards)) as executor:
        futures = {
            executor.submit(run, position, globals_): globals_
            for position, globals_ in enumerate(shards)
        }
        for future in as_completed(futures):
            globals_ = futures[future]
            shard_outcomes = future.result()
            for index, outcome in zip(globals_, shard_outcomes):
                outcomes[index] = outcome
    return [outcome for outcome in outcomes if outcome is not None]


def _kill_process_tree(pid: int) -> None:
    if sys.platform.startswith("win"):
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                timeout=15,
                creationflags=_CREATE_NO_WINDOW,
            )
        except Exception:  # pragma: no cover - best effort cleanup
            pass
    else:
        try:
            process = subprocess.Popen(["pkill", "-TERM", "-P", str(pid)])
            process.wait(timeout=10)
        except Exception:  # pragma: no cover
            pass


@lru_cache(maxsize=1)
def _installed_fonts() -> set[str]:
    names: set[str] = set()
    if not sys.platform.startswith("win"):
        return names
    import winreg

    keys = (
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"),
    )
    for root, path in keys:
        try:
            with winreg.OpenKey(root, path) as key:
                index = 0
                while True:
                    try:
                        value_name, value_data, _ = winreg.EnumValue(key, index)
                    except OSError:
                        break
                    index += 1
                    for candidate in (value_name, str(value_data)):
                        names.add(_normalise_font(candidate))
        except OSError:
            continue

    fonts_dir = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    if fonts_dir.is_dir():
        for entry in fonts_dir.iterdir():
            if entry.suffix.lower() in {".ttf", ".otf", ".ttc"}:
                names.add(_normalise_font(entry.stem))
    names.discard("")
    return names


def _normalise_font(name: str) -> str:
    value = Path(str(name)).stem.lower()
    value = re.sub(r"\s*\((truetype|opentype|all res|type 1)\)\s*$", "", value)
    value = re.sub(r"\s+(regular|book|roman)$", "", value)
    return value.strip()


def _document_fonts(path: Path) -> set[str]:
    try:
        document = Document(str(path))
    except Exception:
        return set()
    names: set[str] = set()
    for part in document.part.package.parts:
        element = getattr(part, "element", None)
        if element is None:
            continue
        try:
            for run_fonts in element.iter(qn("w:rFonts")):
                for attribute in (
                    "w:ascii",
                    "w:hAnsi",
                    "w:eastAsia",
                    "w:cs",
                ):
                    value = run_fonts.get(qn(attribute))
                    if value and not value.endswith("Theme"):
                        names.add(value.strip())
        except Exception:  # pragma: no cover - malformed package part
            continue
    return {name for name in names if name}


def font_warnings(path: Path) -> list[str]:
    """Report fonts that Word will silently substitute."""
    if not sys.platform.startswith("win"):
        return []
    try:
        import zipfile

        with zipfile.ZipFile(path) as archive:
            if any(name.startswith("word/fonts/") for name in archive.namelist()):
                return []
    except Exception:  # pragma: no cover
        pass
    used = _document_fonts(path)
    if not used:
        return []
    installed = _installed_fonts()
    missing = []
    for font in sorted(used):
        normalised = _normalise_font(font)
        if not normalised:
            continue
        if normalised in installed:
            continue
        if any(name.startswith(f"{normalised} ") for name in installed):
            continue
        missing.append(font)
    if not missing:
        return []
    return [
        "Font berikut tidak tersedia di sistem sehingga Microsoft Word menggantinya: "
        + ", ".join(missing[:10])
        + ". Penggantian font dapat mengubah panjang baris dan jumlah halaman."
    ]
