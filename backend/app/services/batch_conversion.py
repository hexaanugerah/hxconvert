"""Batch conversion service for processing multiple files and creating ZIP archives."""
from __future__ import annotations

import os
import shutil
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path, PurePosixPath
from typing import Any, Callable
from dataclasses import dataclass, field

from app.core.config import settings
from app.core.errors import ConversionError
from app.core.storage import job_output_dir, sanitize_filename
from app.services.base import ConversionContext, ConversionResult
from app.services.registry import get_tool, ToolSpec

# Formats whose bytes are already compressed: recompressing them in the ZIP
# wastes CPU for (almost) no size win. Everything else is deflated.
STORED_EXTENSIONS = {
    ".pdf", ".docx", ".xlsx", ".pptx", ".zip",
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".tif", ".tiff", ".bmp",
}
DEFINED_EXTENSIONS = {".txt", ".csv", ".md", ".markdown", ".html", ".htm", ".xml", ".json"}

# Tools whose single-file result is a ZIP that contains many outputs. In a
# batch these are expanded into one subfolder per source file.
MULTI_OUTPUT_TOOLS = {"pdf-to-images", "split-pdf", "pptx-to-images", "xlsx-to-csv"}


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
    once only slows down CPU-bound converters through context switching."""
    configured = max(1, int(settings.conversion_workers or 1))
    return max(1, min(configured, os.cpu_count() or 2))


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


def create_batch_zip(
    batch_id: str,
    file_results: list[BatchFileResult],
    tool: ToolSpec,
) -> tuple[Path, str]:
    """
    Create a ZIP archive containing all successful conversion results.

    - Filenames are derived from the original name plus the real output
      extension (never blindly from the tool's declared format).
    - Duplicate source names get a numeric suffix.
    - Tools that return one ZIP with many outputs (PDF -> images, split PDF)
      put those outputs in a subfolder named after the source file.
    - A failure/warning report is added only when it adds information.

    Returns:
        Tuple of (zip_path, zip_filename)
    """
    output_dir = job_output_dir(batch_id)
    zip_filename = "DocuConvert_Results_{start}.zip".format(start=batch_id[:8])
    zip_path = output_dir / zip_filename

    used_files: dict[str, int] = {}
    used_folders: dict[str, int] = {}
    successful = sum(1 for result in file_results if result.success)

    with zipfile.ZipFile(zip_path, "w") as archive:
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
                try:
                    with zipfile.ZipFile(result.output_path) as inner:
                        for info in inner.infolist():
                            if info.is_dir():
                                continue
                            safe_name = _safe_zip_member(info.filename)
                            if not safe_name:
                                continue
                            _write_member(
                                archive,
                                f"{folder}/{safe_name}",
                                inner.read(info),
                            )
                    continue
                except zipfile.BadZipFile:
                    # Corrupt inner archive: fall through and include it as a
                    # regular file so the user still gets the result.
                    pass

            candidate = _unique_name(f"{base_name}{suffix}", used_files)
            _write_file_member(archive, result.output_path, candidate)

        # A report is only useful when something failed or warned.
        failed = [result for result in file_results if not result.success]
        warned = [result for result in file_results if result.warnings]
        if failed or warned:
            report_lines = [
                "Laporan Konversi Batch DocuConvert",
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
            _write_member(
                archive,
                "laporan-konversi.txt",
                "\n".join(report_lines).encode("utf-8"),
            )

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
    tool: ToolSpec,
    options: dict[str, Any],
    workdir: Path,
) -> BatchFileResult:
    """Convert exactly one file; never raises (failures become results)."""
    ctx = ConversionContext(
        inputs=[input_path],
        original_names=[original_name],
        options=options,
        workdir=workdir / f"file_{index}",
    )
    ctx.workdir.mkdir(parents=True, exist_ok=True)

    try:
        result = tool.handler(ctx)

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


def process_batch_conversion(
    batch_id: str,
    tool_id: str,
    input_files: list[Path],
    original_names: list[str],
    options: dict[str, Any],
    workdir: Path,
    on_stage: Callable[[str], None] | None = None,
) -> BatchConversionResult:
    """
    Process a batch conversion of multiple files.

    Independent files run in a bounded worker pool (CPU/RAM aware). Results are
    collected in the original upload order and a ZIP archive is created when
    multiple files succeed.
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

    if pending:
        if len(pending) == 1 or workers == 1:
            for index in pending:
                file_results[index] = _convert_one(
                    index,
                    input_files[index],
                    original_names[index],
                    tool,
                    options,
                    workdir,
                )
        else:
            with ThreadPoolExecutor(
                max_workers=min(workers, len(pending)),
                thread_name_prefix=f"batch-{batch_id[:8]}",
            ) as pool:
                futures = {
                    pool.submit(
                        _convert_one,
                        index,
                        input_files[index],
                        original_names[index],
                        tool,
                        options,
                        workdir,
                    ): index
                    for index in pending
                }
                for future in as_completed(futures):
                    batch_result = future.result()
                    file_results[batch_result.index] = batch_result

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
    # ZIP is built from these copies.
    results_root = job_output_dir(batch_id) / "files"
    for result in file_results:
        if (
            not result.success
            or not result.output_path
            or not result.output_path.exists()
        ):
            continue
        filename = sanitize_filename(
            result.output_filename or result.output_path.name
        )
        if not filename:
            filename = f"hasil_{result.index + 1}{result.output_path.suffix}"
        target_dir = results_root / str(result.index)
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / filename
        try:
            shutil.copy2(result.output_path, target)
        except OSError:
            # Keep the original path; the ZIP can still read it while the
            # temporary work directory is alive.
            continue
        result.output_path = target
        result.output_filename = filename

    successful = sum(1 for r in file_results if r.success)
    failed = sum(1 for r in file_results if not r.success)

    zip_path = None
    zip_filename = None
    if successful > 1:
        if on_stage is not None:
            on_stage("packaging")
        zip_path, zip_filename = create_batch_zip(batch_id, file_results, tool)

    return BatchConversionResult(
        batch_id=batch_id,
        tool_id=tool_id,
        total_files=len(file_results),
        successful=successful,
        failed=failed,
        file_results=file_results,
        zip_path=zip_path,
        zip_filename=zip_filename,
    )
