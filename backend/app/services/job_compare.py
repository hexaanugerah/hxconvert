"""Lazy original-vs-result visual comparison for one conversion job.

The page renders both sides to PDF at the same DPI, compares them page by page
with :mod:`app.services.visual_compare`, and caches the rendered PNGs plus a
small JSON summary inside the job's output directory, so a comparison is only
computed when someone actually opens it and is reused afterwards.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
import shutil
import tempfile
import threading

from app.core.config import settings
from app.core.errors import ConversionError
from app.core.storage import OUTPUT_DIR, ensure_within, job_inputs, job_temp_dir
from app.models.conversion import ConversionJob
from app.services.base import ConversionContext
from app.services.document_inspection import IMAGE_SUFFIXES
from app.services.docx_ops import docx_to_pdf
from app.services.excel_ops import xlsx_to_pdf
from app.services.html_ops import html_file_to_pdf, markdown_to_pdf, text_to_pdf
from app.services.image_ops import images_to_pdf
from app.services.pptx_ops import pptx_to_pdf
from app.services.visual_compare import compare_pdfs

logger = logging.getLogger("docuconvert.compare")

COMPARE_DIRNAME = "compare"
SUMMARY_NAME = "summary.json"
PAGE_KINDS = ("original", "result", "difference")
OFFICE_SUFFIXES = {".docx", ".xlsx", ".pptx"}
RENDERED_OUTPUTS = OFFICE_SUFFIXES | {".pdf"}
RENDERED_INPUTS = RENDERED_OUTPUTS | IMAGE_SUFFIXES | {".txt", ".md", ".markdown", ".html", ".htm"}

_render_handlers = {
    ".docx": docx_to_pdf,
    ".xlsx": xlsx_to_pdf,
    ".pptx": pptx_to_pdf,
    ".txt": text_to_pdf,
    ".md": markdown_to_pdf,
    ".markdown": markdown_to_pdf,
    ".html": html_file_to_pdf,
    ".htm": html_file_to_pdf,
}

_locks_guard = threading.Lock()
_locks: dict[str, threading.Lock] = {}


class ComparisonUnavailable(Exception):
    """The job cannot be compared; ``message`` is user-facing Indonesian."""


def _lock_for(job_id: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(job_id, threading.Lock())


def compare_dir(job_id: str) -> Path:
    return OUTPUT_DIR / job_id / COMPARE_DIRNAME


def page_filename(page: int, kind: str) -> str:
    return f"page-{page:04d}-{kind}.png"


def page_image(job_id: str, page: int, kind: str) -> Path | None:
    if kind not in PAGE_KINDS:
        return None
    path = compare_dir(job_id) / page_filename(page, kind)
    if not path.is_file():
        return None
    return ensure_within(path, OUTPUT_DIR)


def load_summary(job_id: str) -> dict | None:
    path = compare_dir(job_id) / SUMMARY_NAME
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("available") else None


def _clear(job_id: str) -> None:
    shutil.rmtree(compare_dir(job_id), ignore_errors=True)


def _as_pdf(path: Path, name: str, workdir: Path, label: str) -> Path:
    """Return a PDF for a supported source, converting office/image inputs."""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return path
    workdir.mkdir(parents=True, exist_ok=True)
    if suffix in IMAGE_SUFFIXES:
        result = images_to_pdf(
            ConversionContext(inputs=[path], original_names=[name], options={}, workdir=workdir)
        )
        return result.path
    handler = _render_handlers.get(suffix)
    if handler is None:
        raise ComparisonUnavailable(
            f"Berkas {label} berformat {suffix.lstrip('.') or 'tak dikenal'} tidak dapat "
            "dirender menjadi halaman."
        )
    try:
        result = handler(
            ConversionContext(inputs=[path], original_names=[name], options={}, workdir=workdir)
        )
    except ConversionError as exc:
        raise ComparisonUnavailable(
            f"Berkas {label} tidak dapat dirender untuk dibandingkan: {exc.message}"
        ) from exc
    return result.path


def _unavailable(reason: str) -> dict:
    return {"available": False, "reason": reason, "pages": []}


def _summary(job: ConversionJob, report, *, original_name: str, result_name: str) -> dict:
    pages = []
    for page in report.pages:
        pages.append(
            {
                "page": page.page,
                "ratio": round(page.ratio, 6),
                "mean_difference": round(page.mean_difference, 2),
                "text_similarity": (
                    round(page.text_similarity, 4)
                    if page.text_similarity is not None
                    else None
                ),
                "status": page.status,
                "offset": [page.offset[0], page.offset[1]],
                "original_file": page_filename(page.page, "original"),
                "result_file": page_filename(page.page, "result"),
                "difference_file": page_filename(page.page, "difference"),
            }
        )

    return {
        "available": True,
        "reason": None,
        "dpi": report.dpi,
        "page_count": len(pages),
        "original_page_count": report.first_page_count,
        "result_page_count": report.second_page_count,
        "original_name": original_name,
        "result_name": result_name,
        "average_ratio": round(report.average_ratio, 6),
        "identical_pages": sum(1 for page in report.pages if page.identical),
        "changed_pages": report.changed_pages,
        "truncated": bool(report.truncated_count),
        "max_pages": settings.compare_max_pages,
        "only_original": report.only_first,
        "only_result": report.only_second,
        "pages": pages,
    }


def build_comparison(job: ConversionJob) -> dict:
    """Return the cached comparison or compute, cache and return a new one."""
    with _lock_for(job.id):
        cached = load_summary(job.id)
        if cached:
            return cached

        if job.status != "completed" or not job.output_path:
            return _unavailable("Hasil konversi belum siap untuk dibandingkan.")
        if job.zip_path or job.batch_results_json:
            return _unavailable(
                "Banding visual tersedia untuk konversi satu berkas. "
                "Unduh ZIP dan bandingkan berkasnya secara terpisah."
            )

        output_path = Path(job.output_path)
        if not output_path.is_file():
            return _unavailable("Berkas hasil sudah tidak tersedia.")
        if output_path.suffix.lower() not in RENDERED_OUTPUTS:
            # Text/CSV/archive results have no page layout of their own; a
            # rendered approximation would only produce misleading differences.
            return _unavailable(
                f"Hasil berformat {output_path.suffix.lstrip('.') or 'tak dikenal'} "
                "tidak memiliki tata letak halaman, sehingga tidak dapat "
                "dibandingkan secara visual."
            )

        inputs, names, _ = job_inputs(job.id)
        if not inputs:
            return _unavailable(
                "Berkas sumber sudah tidak tersedia, sehingga perbandingan tidak "
                "dapat dibuat."
            )
        if len(inputs) > 1:
            return _unavailable("Banding visual hanya tersedia untuk satu berkas sumber.")

        original_name = names[0] if names else inputs[0].name
        result_name = job.output_filename or output_path.name

        workdir = Path(tempfile.mkdtemp(prefix="compare-", dir=job_temp_dir(job.id)))
        try:
            original_pdf = _as_pdf(inputs[0], original_name, workdir / "original", "asli")
            result_pdf = _as_pdf(output_path, result_name, workdir / "result", "hasil")

            report = compare_pdfs(
                original_pdf,
                result_pdf,
                dpi=settings.compare_dpi,
                first_name=original_name,
                second_name=result_name,
                max_pages=settings.compare_max_pages,
            )
            if not report.pages:
                return _unavailable(
                    "Tidak ada halaman yang dapat dibandingkan dari kedua berkas."
                )

            target = compare_dir(job.id)
            shutil.rmtree(target, ignore_errors=True)
            target.mkdir(parents=True, exist_ok=True)
            for page in report.pages:
                (target / page_filename(page.page, "original")).write_bytes(page.original_png)
                (target / page_filename(page.page, "result")).write_bytes(page.result_png)
                (target / page_filename(page.page, "difference")).write_bytes(
                    page.difference_png
                )

            summary = _summary(
                job, report, original_name=original_name, result_name=result_name
            )
            (target / SUMMARY_NAME).write_text(json.dumps(summary), encoding="utf-8")
            return summary
        except ComparisonUnavailable as exc:
            _clear(job.id)
            return _unavailable(str(exc))
        except Exception as exc:
            logger.exception("Comparison failed for job %s", job.id)
            _clear(job.id)
            return _unavailable(
                "Banding visual gagal dibuat: "
                f"{(str(exc).strip().splitlines() or [exc.__class__.__name__])[0][:200]}"
            )
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
