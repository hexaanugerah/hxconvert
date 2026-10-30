"""PowerPoint tools. Rendering requires LibreOffice, which is checked at runtime."""
from __future__ import annotations

from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import fitz
from pptx import Presentation

from app.core.errors import ConversionError
from app.services.base import ConversionContext, ConversionResult
from app.services.capabilities import libreoffice_available
from app.services.office_ops import convert_parallel_with_soffice, convert_with_soffice

PDF_MIME = "application/pdf"


def _validate(path: Path) -> None:
    try:
        Presentation(str(path))
    except Exception as exc:
        raise ConversionError("The PowerPoint file could not be opened. It may be damaged.") from exc


def _prepare_batch(
    contexts: list[ConversionContext], output_name: str
) -> tuple[list[int], list[tuple[Path, Path]]]:
    """Validate inputs and build (index, (input, output)) data for the batch."""
    indices: list[int] = []
    jobs: list[tuple[Path, Path]] = []
    for index, ctx in enumerate(contexts):
        ctx.workdir.mkdir(parents=True, exist_ok=True)
        try:
            _validate(ctx.input)
        except ConversionError:
            continue  # per-file handler reports this error properly
        indices.append(index)
        jobs.append((ctx.input, ctx.workdir / output_name))
    return indices, jobs


def _fill_pdf_results(
    contexts: list[ConversionContext],
    indices: list[int],
    outcomes: list,
) -> list[ConversionResult | None]:
    results: list[ConversionResult | None] = [None] * len(contexts)
    for pointer, outcome in zip(indices, outcomes):
        if outcome.output is None:
            continue
        ctx = contexts[pointer]
        stem = Path(ctx.original_name).stem or "presentation"
        results[pointer] = ConversionResult(outcome.output, f"{stem}.pdf", PDF_MIME)
    return results


def pptx_to_pdf(ctx: ConversionContext) -> ConversionResult:
    _validate(ctx.input)
    output = convert_with_soffice(ctx.input, "pdf", ctx.workdir)
    filename = f"{Path(ctx.original_name).stem or 'presentation'}.pdf"
    return ConversionResult(output, filename, PDF_MIME)


def pptx_to_pdf_batch(
    contexts: list[ConversionContext],
) -> list[ConversionResult | None]:
    """Batch fast path: convert every presentation in one soffice invocation.

    ``None`` entries fall back to the normal per-file handler.
    """
    if len(contexts) < 2 or not libreoffice_available():
        return [None] * len(contexts)
    indices, jobs = _prepare_batch(contexts, "document.pdf")
    try:
        outcomes = convert_parallel_with_soffice(
            jobs, "pdf", contexts[0].workdir.parent
        )
    except ConversionError:
        return [None] * len(contexts)
    return _fill_pdf_results(contexts, indices, outcomes)


def pptx_to_images(ctx: ConversionContext) -> ConversionResult:
    _validate(ctx.input)
    dpi = max(72, min(300, int(ctx.options.get("dpi", 120) or 120)))
    pdf_path = convert_with_soffice(ctx.input, "pdf", ctx.workdir)
    zip_path = ctx.workdir / "slides.zip"
    doc = fitz.open(pdf_path)
    try:
        with ZipFile(zip_path, "w", ZIP_DEFLATED) as archive:
            for index, page in enumerate(doc):
                pixmap = page.get_pixmap(matrix=fitz.Matrix(dpi / 72, dpi / 72), alpha=False)
                archive.writestr(f"slide-{index + 1:03d}.png", pixmap.tobytes("png"))
    finally:
        doc.close()
    return ConversionResult(zip_path, "slides.zip", "application/zip")


def pptx_to_images_batch(
    contexts: list[ConversionContext],
) -> list[ConversionResult | None]:
    """Batch fast path: one soffice invocation, then in-process image rendering.

    ``None`` entries fall back to the normal per-file handler.
    """
    if len(contexts) < 2 or not libreoffice_available():
        return [None] * len(contexts)
    indices, jobs = _prepare_batch(contexts, "document.pdf")
    try:
        outcomes = convert_parallel_with_soffice(
            jobs, "pdf", contexts[0].workdir.parent
        )
    except ConversionError:
        return [None] * len(contexts)

    results: list[ConversionResult | None] = [None] * len(contexts)
    for pointer, outcome in zip(indices, outcomes):
        if outcome.output is None:
            continue
        ctx = contexts[pointer]
        try:
            dpi = max(72, min(300, int(ctx.options.get("dpi", 120) or 120)))
            zip_path = ctx.workdir / "slides.zip"
            doc = fitz.open(outcome.output)
            try:
                with ZipFile(zip_path, "w", ZIP_DEFLATED) as archive:
                    for index, page in enumerate(doc):
                        pixmap = page.get_pixmap(
                            matrix=fitz.Matrix(dpi / 72, dpi / 72), alpha=False
                        )
                        archive.writestr(f"slide-{index + 1:03d}.png", pixmap.tobytes("png"))
            finally:
                doc.close()
        except Exception:  # noqa: BLE001 - let the per-file handler handle it
            continue
        results[pointer] = ConversionResult(zip_path, "slides.zip", "application/zip")
    return results
