"""Batch conversion service for processing multiple files and creating ZIP archives."""
from __future__ import annotations

import hashlib
import logging
import multiprocessing
import os
import shutil
import threading
import zipfile
from concurrent.futures import (
    BrokenExecutor,
    ProcessPoolExecutor,
    ThreadPoolExecutor,
    as_completed,
)
from pathlib import Path, PurePosixPath
from typing import Any, Callable
from dataclasses import dataclass, field

from app.core.config import settings
from app.core.storage import job_output_dir, sanitize_filename
from app.services.base import ConversionContext, ConversionResult
from app.services.registry import get_tool, ToolSpec

logger = logging.getLogger("docuconvert.batch")

# Formats whose bytes are already compressed: recompressing them in the ZIP
# wastes CPU for (almost) no size win. Everything else is deflated.
STORED_EXTENSIONS = {
    ".pdf", ".docx", ".xlsx", ".pptx", ".zip",
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".tif", ".tiff", ".bmp",
}
DEFINED_EXTENSIONS = {".txt", ".csv", ".md", ".markdown", ".html", ".htm", ".xml", ".json"}

# ZIP members up to this size are read into memory in small concurrent
# batches before being written. Larger ones are streamed from disk, so peak
# memory stays bounded however big the results are.
ZIP_BUFFER_MEMBER_MB = 16

# Batch ZIPs are assembled from finished results, so their size follows the
# output format: 20 image-only PDF -> Word files are ~400 MB and 20 full-image
# (300 DPI PNG) exports pass 400 MB too. Above ZIP_WARN_MB the archive is still
# built but the user is told why the download is slow; above ZIP_SKIP_MB the
# archive is refused so the server never produces a multi-gigabyte file that a
# browser cannot download reliably. Individual files stay downloadable.
ZIP_WARN_MB = 250
ZIP_SKIP_MB = 2048

# Tools whose single-file result is a ZIP that contains many outputs. In a
# batch these are expanded into one subfolder per source file.
MULTI_OUTPUT_TOOLS = {"pdf-to-images", "split-pdf", "pptx-to-images", "xlsx-to-csv"}

# Tools whose handler runs an external application (LibreOffice/Word). Their
# heavy work already happens in separate OS processes, so a CPU process pool
# adds spawn overhead for nothing. Every other handler is CPU-bound inside
# Python and only benefits from real process parallelism (threads are limited
# by the GIL for conversions like PDF -> Word).
SUBPROCESS_TOOL_IDS = frozenset(
    {"docx-to-pdf", "xlsx-to-pdf", "pptx-to-pdf", "pptx-to-images"}
)


@dataclass
class BatchFileResult:
    """Result of converting a single file in a batch."""
    index: int
    original_name: str
    success: bool
    output_path: Path | None = None
    output_filename: str | None = None
    output_mime: str | None = None
    error_message: str | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass
class BatchConversionResult:
    """Result of a batch conversion."""
    batch_id: str
    tool_id: str
    total_files: int
    successful: int
    failed: int
    file_results: list[BatchFileResult]
    zip_path: Path | None = None
    zip_filename: str | None = None
    warnings: list[str] = field(default_factory=list)


def _worker_count() -> int:
    """Bounded pool: an idle pool wastes nothing, and running every file at
    once only slows down CPU-bound converters through context switching.

    ``settings.conversion_workers <= 0`` means auto: half the cores, capped at
    six, which leaves headroom for the request handler and OCR subprocesses.
    On containers with a CPU quota, set ``CONVERSION_WORKERS`` explicitly
    because ``os.cpu_count()`` can report the host's cores.
    """
    configured = int(settings.conversion_workers or 0)
    if configured <= 0:
        # Auto: half the cores, capped at six. Four workers on an 8-core
        # machine finish a 15 x 100 KB batch in ~2.5 s while keeping pool
        # startup and memory low; spawning one worker per core cost seconds of
        # startup and bought little for small documents.
        cores = os.cpu_count() or 2
        return max(1, min(6, max(2, cores // 2)))
    return max(1, min(configured, os.cpu_count() or 2))


# A persistent process pool for CPU-bound conversions. Spawning a fresh pool
# per batch costs seconds on Windows (every worker re-imports PyMuPDF and
# friends), so the pool is created once and reused across jobs.
_CPU_POOL: ProcessPoolExecutor | None = None
_CPU_POOL_LOCK = threading.Lock()


def _pool_init() -> None:
    """Pre-import the heavy conversion libraries in each pool worker so the
    first file of a batch does not pay their import cost.

    ``pdfplumber`` (table detection, editable PDF -> Word) and the PDF -> Word
    handler module are imported here as well: importing them lazily on the
    first submitted file added seconds to the first batch, because every worker
    paid it while the user was waiting. Each worker imports only the modules
    the submitted handler needs; importing the whole service registry here
    (which pulls openpyxl, python-pptx, xhtml2pdf and friends into every
    process) made pool startup slower than the conversions it was meant to
    accelerate.
    """
    import docx  # noqa: F401
    import fitz  # noqa: F401
    import pdfplumber  # noqa: F401
    from PIL import Image  # noqa: F401

    from app.services import pdf_to_docx  # noqa: F401


def _pool_ready(barrier: Any | None = None) -> int:
    """Worker-side barrier: returns only once every worker in the pool runs."""
    if barrier is not None:
        try:
            barrier.wait(timeout=45)
        except Exception:  # noqa: BLE001 - a timeout must not kill the worker
            return -1
    return os.getpid()


def _prestart_pool(pool: ProcessPoolExecutor, workers: int) -> None:
    """Spawn every worker before the first task reaches the queue.

    ``ProcessPoolExecutor`` grows lazily: it only spawns another process while
    all existing ones are busy. Small conversions finish in ~100 ms, so a
    single fast worker drains a whole batch queue while the remaining workers
    are still importing, serialising the batch. A shared barrier holds every
    submitted task until all workers exist, which forces the pool to full size
    and lets the next real submissions run in parallel.
    """
    if workers <= 1:
        pool.submit(_pool_ready).result(timeout=120)
        return

    manager = None
    try:
        manager = multiprocessing.Manager()
        barrier = manager.Barrier(workers)
        futures = [pool.submit(_pool_ready, barrier) for _ in range(workers)]
        for future in futures:
            future.result(timeout=120)
    finally:
        if manager is not None:
            manager.shutdown()


def warm_cpu_pool() -> None:
    """Create the conversion pool and start all workers ahead of first use.

    Called at application startup so the first real batch does not pay the
    Windows spawn + import cost of six processes.
    """
    if _worker_count() <= 1:
        return
    _cpu_pool(_worker_count())


def _cpu_pool(workers: int) -> ProcessPoolExecutor:
    """Return the process pool, fully started, recreating it after a crash."""
    global _CPU_POOL
    with _CPU_POOL_LOCK:
        if _CPU_POOL is None:
            pool = ProcessPoolExecutor(
                max_workers=workers, initializer=_pool_init
            )
            try:
                _prestart_pool(pool, workers)
            except Exception:  # noqa: BLE001 - fall back to lazy workers
                logger.warning(
                    "Could not pre-start all conversion workers", exc_info=True
                )
            _CPU_POOL = pool
        return _CPU_POOL


def _drop_cpu_pool() -> None:
    """Forget the process pool so a new one is created for the next batch."""
    global _CPU_POOL
    with _CPU_POOL_LOCK:
        if _CPU_POOL is not None:
            try:
                _CPU_POOL.shutdown(wait=False, cancel_futures=True)
            except Exception:  # noqa: BLE001 - shutdown may already be broken
                pass
            _CPU_POOL = None


def _hash_file(path: Path) -> str | None:
    """SHA-256 of a file streamed in chunks; ``None`` if it cannot be read."""
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def _hash_files(paths: list[Path]) -> dict[int, str]:
    """Hash files concurrently.

    New jobs arrive with digests computed during upload. This fallback is for
    legacy jobs and direct API calls: hashing 15 small files sequentially took
    seconds on Windows when the files were first read, while the conversions
    themselves were faster, so the reads run in parallel here.
    """
    if not paths:
        return {}
    workers = max(1, min(_worker_count(), len(paths), 8))
    results: dict[int, str] = {}
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="batch-hash") as pool:
        futures = {pool.submit(_hash_file, path): index for index, path in enumerate(paths)}
        for future in as_completed(futures):
            digest = future.result()
            if digest is not None:
                results[futures[future]] = digest
    return results


def _zip_method(ext: str) -> tuple[int, int]:
    """(compress_type, compresslevel) for one result file."""
    if ext in STORED_EXTENSIONS:
        return zipfile.ZIP_STORED, None
    if ext in DEFINED_EXTENSIONS:
        return zipfile.ZIP_DEFLATED, settings.zip_compression_level
    return zipfile.ZIP_DEFLATED, settings.zip_compression_level


def _unique_name(candidate: str, used: dict[str, int]) -> str:
    """Return a unique archive member name, adding _1, _2, ... on collisions."""
    if candidate not in used:
        used[candidate] = 0
        return candidate
    stem = Path(candidate).stem
    suffix = Path(candidate).suffix
    counter = used[candidate] + 1
    while True:
        alternative = f"{stem}_{counter}{suffix}"
        if alternative not in used:
            used[candidate] = counter
            used[alternative] = 0
            return alternative
        counter += 1


def _safe_zip_member(name: str) -> str:
    """Sanitise an inner ZIP member path so it cannot escape its folder."""
    parts = [
        part
        for part in PurePosixPath(name.replace("\\", "/")).parts
        if part not in ("", ".", "..", "/")
    ]
    return "/".join(parts)


def _write_member(archive: zipfile.ZipFile, name: str, data: bytes) -> None:
    ext = Path(name).suffix.lower()
    compress_type, compresslevel = _zip_method(ext)
    if compresslevel is None:
        archive.writestr(name, data, compress_type=compress_type)
    else:
        archive.writestr(
            name,
            data,
            compress_type=compress_type,
            compresslevel=compresslevel,
        )


def _write_file_member(
    archive: zipfile.ZipFile, path: Path, name: str
) -> None:
    ext = Path(name).suffix.lower()
    compress_type, compresslevel = _zip_method(ext)
    if compresslevel is None:
        archive.write(path, name, compress_type=compress_type)
    else:
        archive.write(
            path,
            name,
            compress_type=compress_type,
            compresslevel=compresslevel,
        )


def _is_zip(path: Path) -> bool:
    """True when ``path`` is a readable ZIP archive."""
    try:
        with zipfile.ZipFile(path):
            pass
    except (zipfile.BadZipFile, OSError):
        return False
    return True


def _uses_external_renderer(tool_id: str, options: dict[str, Any]) -> bool:
    """Whether the handler's heavy work already runs in another OS process.

    DOCX -> PDF is the one tool whose engine can change at request time: the
    explicit Word engine and a host with LibreOffice spawn an external process
    (threads are fine), while the deterministic Python renderer is GIL-bound and
    needs the process pool to use more than one core.
    """
    if tool_id not in SUBPROCESS_TOOL_IDS:
        return False
    if tool_id == "docx-to-pdf":
        engine = str(options.get("engine", "auto") or "auto").strip().lower()
        if engine == "python":
            return False
        if engine == "word":
            return True
        from app.services.capabilities import libreoffice_available

        return libreoffice_available()
    return True


def _bufferable(path: Path) -> bool:
    """Whether a member may be read into memory for concurrent writing."""
    try:
        return path.stat().st_size <= ZIP_BUFFER_MEMBER_MB * 1024 * 1024
    except OSError:
        return False


def _write_inner_zip(
    archive: zipfile.ZipFile, path: Path, folder: str
) -> None:
    """Expand one inner ZIP into its own folder of the outer archive."""
    with zipfile.ZipFile(path) as inner:
        for info in inner.infolist():
            if info.is_dir():
                continue
            safe_name = _safe_zip_member(info.filename)
            if not safe_name:
                continue
            _write_member(archive, f"{folder}/{safe_name}", inner.read(info))


def _write_members(
    archive: zipfile.ZipFile,
    members: list[tuple[str, Path | None, bytes | None, str | None]],
) -> None:
    """Write members in order while reading small files concurrently.

    A ZIP archive is one stream, so payloads must be written sequentially.
    Reading twenty results one by one, however, is mostly latency (file
    opens, on-access virus scanning, network home directories). Reads are
    batched across a small thread pool instead; the batch size and the
    per-member cap keep the buffered amount bounded.
    """
    workers = max(1, min(_worker_count(), len(members) or 1))
    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="batch-zip")
    try:
        for start in range(0, len(members), workers):
            chunk = members[start : start + workers]
            pending: list[
                tuple[str, Path | None, bytes | None, str | None, Any]
            ] = []
            for name, path, data, folder in chunk:
                if (
                    folder is not None
                    or data is not None
                    or path is None
                    or not _bufferable(path)
                ):
                    pending.append((name, path, data, folder, None))
                    continue
                pending.append(
                    (name, path, data, folder, pool.submit(path.read_bytes))
                )

            for name, path, data, folder, future in pending:
                if folder is not None and path is not None:
                    _write_inner_zip(archive, path, folder)
                elif future is not None:
                    _write_member(archive, name, future.result())
                elif data is not None:
                    _write_member(archive, name, data)
                elif path is not None:
                    _write_file_member(archive, path, name)
    finally:
        pool.shutdown(wait=True, cancel_futures=True)


def create_batch_zip(
    batch_id: str,
    file_results: list[BatchFileResult],
    tool: ToolSpec,
    warnings: list[str] | None = None,
) -> tuple[Path, str] | None:
    """
    Create a ZIP archive containing all successful conversion results.

    - Filenames are derived from the original name plus the real output
      extension (never blindly from the tool's declared format).
    - Duplicate source names get a numeric suffix.
    - Tools that return one ZIP with many outputs (PDF -> images, split PDF)
      put those outputs in a subfolder named after the source file.
    - A failure/warning report is added only when it adds information.
    - The planned payload size is checked first: a huge archive (large image
      exports) gets a warning, and an absurd one is skipped entirely.

    Returns:
        Tuple of (zip_path, zip_filename), or ``None`` when the payload
        exceeds ``ZIP_SKIP_MB`` and the archive was not built.
    """
    output_dir = job_output_dir(batch_id)
    zip_filename = "HXconvertX_Results_{start}.zip".format(start=batch_id[:8])
    zip_path = output_dir / zip_filename

    used_files: dict[str, int] = {}
    used_folders: dict[str, int] = {}
    successful = sum(1 for result in file_results if result.success)

    # Plan the archive before writing it: names are assigned in order, then
    # the writer can read several members concurrently. Each entry is
    # (member name, source path, in-memory data, inner-ZIP folder).
    members: list[tuple[str, Path | None, bytes | None, str | None]] = []
    for result in file_results:
        if not result.success or not result.output_path or not result.output_path.exists():
            continue

        base_name = sanitize_filename(Path(result.original_name).stem) or f"berkas_{result.index + 1}"
        suffix = Path(
            result.output_filename or result.output_path.name
        ).suffix or f".{tool.output}"

        # One source file -> an inner ZIP with many outputs: expand it into
        # a dedicated subfolder so page images do not collide.
        if (
            tool.id in MULTI_OUTPUT_TOOLS
            and suffix.lower() == ".zip"
            and successful > 1
        ):
            folder = _unique_name(base_name, used_folders)
            if _is_zip(result.output_path):
                members.append(("", result.output_path, None, folder))
                continue
            # Corrupt inner archive: fall through and include it as a regular
            # file so the user still gets the result.

        candidate = _unique_name(f"{base_name}{suffix}", used_files)
        members.append((candidate, result.output_path, None, None))

    # A report is only useful when something failed or warned.
    failed = [result for result in file_results if not result.success]
    warned = [result for result in file_results if result.warnings]
    if failed or warned:
        report_lines = [
            "Laporan Konversi Batch HXconvertX",
            f"ID Batch: {batch_id}",
            f"Alat: {tool.label} ({tool.id})",
            f"Total berkas: {len(file_results)}",
            f"Berhasil: {successful}",
            f"Gagal: {len(failed)}",
            "",
            "Rincian per berkas:",
        ]
        for result in file_results:
            status = "BERHASIL" if result.success else "GAGAL"
            report_lines.append(f"  [{status}] {result.original_name}")
            if not result.success and result.error_message:
                report_lines.append(f"    Kesalahan: {result.error_message}")
            for warning in result.warnings:
                report_lines.append(f"    Peringatan: {warning}")
        report_lines.append("")
        report_lines.append(
            "Berkas yang gagal tidak disertakan dalam arsip ini."
        )
        members.append(
            (
                "laporan-konversi.txt",
                None,
                "\n".join(report_lines).encode("utf-8"),
                None,
            )
        )

    planned_bytes = 0
    for _, path, data, _folder in members:
        if data is not None:
            planned_bytes += len(data)
        elif path is not None:
            try:
                planned_bytes += path.stat().st_size
            except OSError:
                pass
    planned_mb = planned_bytes / (1024 * 1024)
    if planned_bytes > ZIP_SKIP_MB * 1024 * 1024:
        if warnings is not None:
            warnings.append(
                f"Berkas ZIP tidak dibuat karena total hasil ±{planned_mb:.0f} MB "
                f"melebihi batas {ZIP_SKIP_MB} MB. Setiap berkas tetap dapat "
                "diunduh satu per satu."
            )
        return None
    if planned_bytes > ZIP_WARN_MB * 1024 * 1024 and warnings is not None:
        warnings.append(
            f"Total hasil batch ±{planned_mb:.0f} MB, sehingga berkas ZIP akan "
            "berukuran besar dan unduhannya lambat. Unduh per berkas bila lebih "
            "nyaman."
        )

    with zipfile.ZipFile(zip_path, "w") as archive:
        _write_members(archive, members)

    return zip_path, zip_filename


def _from_conversion_result(
    index: int, original_name: str, result: ConversionResult
) -> BatchFileResult:
    """Turn a handler result (from a batch fast path) into a batch entry."""
    try:
        if result.path.exists() and result.path.stat().st_size > 0:
            return BatchFileResult(
                index=index,
                original_name=original_name,
                success=True,
                output_path=result.path,
                output_filename=result.filename,
                output_mime=result.mime,
                warnings=result.warnings,
            )
    except OSError:
        pass
    return BatchFileResult(
        index=index,
        original_name=original_name,
        success=False,
        error_message="Konverter menghasilkan berkas kosong.",
    )


def _convert_one(
    index: int,
    input_path: Path,
    original_name: str,
    handler: Callable[[ConversionContext], ConversionResult],
    options: dict[str, Any],
    workdir: Path,
) -> BatchFileResult:
    """Convert exactly one file; never raises (failures become results).

    Only the handler callable is sent to the worker, not the whole ``ToolSpec``:
    unpickling a ToolSpec imports the registry module and every conversion
    service, which cost seconds per worker before the first file converted.
    """
    ctx = ConversionContext(
        inputs=[input_path],
        original_names=[original_name],
        options=options,
        workdir=workdir / f"file_{index}",
    )
    ctx.workdir.mkdir(parents=True, exist_ok=True)

    try:
        result = handler(ctx)

        if result.path.exists() and result.path.stat().st_size > 0:
            return BatchFileResult(
                index=index,
                original_name=original_name,
                success=True,
                output_path=result.path,
                output_filename=result.filename,
                output_mime=result.mime,
                warnings=result.warnings,
            )
        return BatchFileResult(
            index=index,
            original_name=original_name,
            success=False,
            error_message="Konverter menghasilkan berkas kosong.",
        )
    except Exception as exc:  # noqa: BLE001 - one bad file must not kill the batch
        return BatchFileResult(
            index=index,
            original_name=original_name,
            success=False,
            error_message=str(exc)[:500],
        )


def _convert_indices(
    batch_id: str,
    indices: list[int],
    input_files: list[Path],
    original_names: list[str],
    tool: ToolSpec,
    options: dict[str, Any],
    workdir: Path,
    workers: int,
) -> dict[int, BatchFileResult]:
    """Convert the given indices; parallel when there is more than one.

    Process pool for handlers that are CPU-bound in Python (the GIL keeps
    threads serial there); the thread fallback remains for subprocess tools
    and for recovering whatever a broken process pool could not finish.
    """
    results: dict[int, BatchFileResult] = {}
    if not indices:
        return results

    handler = tool.handler

    def inline(missing: list[int]) -> None:
        for index in missing:
            results[index] = _convert_one(
                index,
                input_files[index],
                original_names[index],
                handler,
                options,
                workdir,
            )

    if len(indices) == 1 or workers == 1:
        inline(indices)
        return results

    if not _uses_external_renderer(tool.id, options):
        try:
            pool = _cpu_pool(workers)
        except Exception:  # noqa: BLE001 - e.g. unpicklable options: use threads
            pool = None
        if pool is not None:
            futures: dict[Any, int] = {}
            try:
                for index in indices:
                    future = pool.submit(
                        _convert_one,
                        index,
                        input_files[index],
                        original_names[index],
                        handler,
                        options,
                        workdir,
                    )
                    futures[future] = index
                for future in as_completed(futures):
                    index = futures[future]
                    try:
                        results[index] = future.result()
                    except Exception:  # noqa: BLE001 - worker died: retry inline
                        continue
            except BrokenExecutor:
                # A worker crashed mid-flight: rebuild the pool for the next
                # batch and let the thread fallback finish these files.
                _drop_cpu_pool()
            missing = [index for index in indices if index not in results]
            inline(missing)
            return results

    with ThreadPoolExecutor(
        max_workers=min(workers, len(indices)),
        thread_name_prefix=f"batch-{batch_id[:8]}",
    ) as pool:
        futures = {
            pool.submit(
                _convert_one,
                index,
                input_files[index],
                original_names[index],
                handler,
                options,
                workdir,
            ): index
            for index in indices
        }
        for future in as_completed(futures):
            batch_result = future.result()
            results[batch_result.index] = batch_result
    return results


def _persist_result(result: BatchFileResult, results_root: Path) -> None:
    """Copy one result to its permanent per-file download location."""
    if not result.success or not result.output_path or not result.output_path.exists():
        return
    filename = sanitize_filename(
        result.output_filename or result.output_path.name
    )
    if not filename:
        filename = f"hasil_{result.index + 1}{result.output_path.suffix}"
    target_dir = results_root / str(result.index)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / filename
    try:
        # Metadata is irrelevant for downloads; copyfile avoids the extra
        # stat calls copy2 performs per file.
        shutil.copyfile(result.output_path, target)
    except OSError:
        # Keep the original path; the ZIP can still read it while the
        # temporary work directory is alive.
        return
    result.output_path = target
    result.output_filename = filename


def process_batch_conversion(
    batch_id: str,
    tool_id: str,
    input_files: list[Path],
    original_names: list[str],
    options: dict[str, Any],
    workdir: Path,
    on_stage: Callable[[str], None] | None = None,
    digests: list[str | None] | None = None,
) -> BatchConversionResult:
    """
    Process a batch conversion of multiple files.

    Independent files run in a bounded worker pool (CPU/RAM aware). Results are
    collected in the original upload order and a ZIP archive is created when
    multiple files succeed. ``digests`` are SHA-256 values computed while the
    uploads were written; when supplied, duplicate detection needs no extra
    read of the source files.
    """
    tool = get_tool(tool_id)
    workers = _worker_count()
    file_results: list[BatchFileResult] = [None] * len(input_files)  # type: ignore[list-item]

    pending = list(range(len(input_files)))

    # Tools with a batch handler can share expensive resources (a Word session
    # for DOCX -> PDF). Everything not produced there falls back to the normal
    # per-file handler, so failures stay isolated.
    if len(input_files) > 1 and workers > 1 and tool.batch_handler is not None:
        contexts = [
            ConversionContext(
                inputs=[input_path],
                original_names=[original_name],
                options=options,
                workdir=workdir / f"file_{index}",
            )
            for index, (input_path, original_name) in enumerate(
                zip(input_files, original_names)
            )
        ]
        try:
            converted = tool.batch_handler(contexts)
        except Exception:  # noqa: BLE001 - fall back to per-file conversion
            converted = []
        for index, result in enumerate(converted):
            if index >= len(input_files) or result is None:
                continue
            file_results[index] = _from_conversion_result(
                index, original_names[index], result
            )
            pending.remove(index)

    duplicate_rep: dict[int, int] = {}
    if len(pending) > 1:
        # Identical files are converted once and their results reused. New jobs
        # arrive with upload-time digests; only files missing one are read here.
        known: dict[int, str | None] = {}
        if digests is not None and len(digests) == len(input_files):
            known = {index: digests[index] for index in pending}
        missing = [index for index in pending if not known.get(index)]
        if missing:
            computed = _hash_files([input_files[index] for index in missing])
            for position, index in enumerate(missing):
                known[index] = computed.get(position)

        seen_digests: dict[str, int] = {}
        for index in pending:
            digest = known.get(index)
            if not digest:
                continue
            first_index = seen_digests.setdefault(digest, index)
            if first_index != index:
                duplicate_rep[index] = first_index

    representatives = [index for index in pending if index not in duplicate_rep]
    clones = [index for index in pending if index in duplicate_rep]

    converted_results = _convert_indices(
        batch_id,
        representatives,
        input_files,
        original_names,
        tool,
        options,
        workdir,
        workers,
    )
    for index, batch_result in converted_results.items():
        file_results[index] = batch_result

    for clone in clones:
        rep = duplicate_rep[clone]
        rep_result = file_results[rep]
        if (
            rep_result is not None
            and rep_result.success
            and rep_result.output_path
            and rep_result.output_path.exists()
        ):
            # The clone shares the representative's bytes, but its download
            # name must follow its own source name. Inheriting the
            # representative's filename made every duplicate download appear
            # as the first file (e.g. fifteen "batch-01.docx" entries).
            suffix = Path(
                rep_result.output_filename or rep_result.output_path.name
            ).suffix
            base = sanitize_filename(Path(original_names[clone]).stem) or (
                f"berkas_{clone + 1}"
            )
            file_results[clone] = BatchFileResult(
                index=clone,
                original_name=original_names[clone],
                success=True,
                output_path=rep_result.output_path,
                output_filename=f"{base}{suffix}",
                output_mime=rep_result.output_mime,
                warnings=rep_result.warnings,
            )
        else:
            file_results[clone] = BatchFileResult(
                index=clone,
                original_name=original_names[clone],
                success=False,
                error_message=(
                    "Berkas identik dengan berkas lain dalam batch ini yang "
                    "juga gagal dikonversi."
                ),
            )

    file_results = [
        result
        if result is not None
        else BatchFileResult(
            index=i, original_name=original_names[i], success=False,
            error_message="Konversi tidak menghasilkan berkas.",
        )
        for i, result in enumerate(file_results)
    ]

    # Persist successful results outside the temporary work directory so each
    # one can be downloaded individually after the batch finishes. The batch
    # ZIP is built from these copies. Copies run concurrently because twenty
    # sequential file copies are dominated by per-file latency, not bandwidth.
    results_root = job_output_dir(batch_id) / "files"
    to_persist = [
        result
        for result in file_results
        if result.success and result.output_path and result.output_path.exists()
    ]
    if to_persist:
        with ThreadPoolExecutor(
            max_workers=max(1, min(_worker_count(), len(to_persist))),
            thread_name_prefix="batch-copy",
        ) as copy_pool:
            for _ in copy_pool.map(
                lambda result: _persist_result(result, results_root), to_persist
            ):
                pass

    successful = sum(1 for r in file_results if r.success)
    failed = sum(1 for r in file_results if not r.success)

    zip_path = None
    zip_filename = None
    batch_warnings: list[str] = []
    if successful > 1:
        if on_stage is not None:
            on_stage("packaging")
        archive = create_batch_zip(
            batch_id, file_results, tool, warnings=batch_warnings
        )
        if archive is not None:
            zip_path, zip_filename = archive

    return BatchConversionResult(
        batch_id=batch_id,
        tool_id=tool_id,
        total_files=len(file_results),
        successful=successful,
        failed=failed,
        file_results=file_results,
        zip_path=zip_path,
        zip_filename=zip_filename,
        warnings=batch_warnings,
    )
