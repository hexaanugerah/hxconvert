"""Inspect source documents and render real per-page previews.

Every page record describes a page that actually exists, either in the source
document or in the PDF produced by the conversion. Preview PNGs are written to
disk; the API only exposes a preview URL for a record whose file exists. Nothing
here invents page counts or preview links.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

import fitz
from PIL import Image, ImageOps

from app.core.config import settings

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".gif"}


@dataclass
class PageRecord:
    page_number: int
    status: str = "completed"  # completed | pending | failed
    width_pt: float | None = None
    height_pt: float | None = None
    orientation: str | None = None
    pixel_width: int | None = None
    pixel_height: int | None = None
    dpi: float | None = None
    preview: str | None = None
    error: str | None = None

    def as_dict(self) -> dict:
        return {key: value for key, value in asdict(self).items() if value is not None}


def _orientation(width: float, height: float) -> str:
    return "landscape" if width > height else "portrait"


def _preview_name(page_number: int) -> str:
    return f"page-{page_number:04d}.png"


def _write_pdf_page_preview(page, preview_path: Path) -> tuple[int, int, float]:
    """Render one PDF page to a PNG preview. Returns (width, height, dpi)."""
    rect = page.rect
    longest = max(float(rect.width), float(rect.height), 1.0)
    zoom = min(settings.preview_dpi / 72, settings.preview_max_width / longest)
    zoom = max(0.2, zoom)
    pixmap = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    pixmap.save(str(preview_path))
    return pixmap.width, pixmap.height, round(zoom * 72, 1)


def _safe_error(exc: Exception) -> str:
    message = str(exc).strip().splitlines()[0] if str(exc).strip() else exc.__class__.__name__
    return message[:300]


def _pdf_records(
    path: Path,
    preview_dir: Path,
    *,
    start_page: int,
    budget: int,
) -> list[PageRecord]:
    records: list[PageRecord] = []
    try:
        document = fitz.open(path)
    except Exception as exc:  # pragma: no cover - defensive
        return [
            PageRecord(
                page_number=start_page,
                status="failed",
                error=f"The document could not be opened for preview: {_safe_error(exc)}",
            )
        ]
    try:
        if document.needs_pass:
            return [
                PageRecord(
                    page_number=start_page,
                    status="failed",
                    error="The document is password protected, so no preview could be rendered.",
                )
            ]
        for index in range(document.page_count):
            page_number = start_page + index
            page = document[index]
            rect = page.rect
            record = PageRecord(
                page_number=page_number,
                width_pt=round(float(rect.width), 2),
                height_pt=round(float(rect.height), 2),
                orientation=_orientation(rect.width, rect.height),
            )
            records.append(record)

            if budget <= 0:
                record.status = "pending"
                continue
            try:
                preview_path = preview_dir / _preview_name(page_number)
                pixel_width, pixel_height, dpi = _write_pdf_page_preview(page, preview_path)
                record.preview = preview_path.name
                record.pixel_width = pixel_width
                record.pixel_height = pixel_height
                record.dpi = dpi
                budget -= 1
            except Exception as exc:
                record.status = "failed"
                record.error = f"Page {page_number} could not be rendered: {_safe_error(exc)}"
    finally:
        document.close()
    return records


def _image_record(path: Path, preview_dir: Path, page_number: int, budget: int) -> PageRecord:
    record = PageRecord(page_number=page_number)
    try:
        with Image.open(path) as opened:
            opened.load()
            image = ImageOps.exif_transpose(opened)
            width, height = image.size
            raw_dpi = image.info.get("dpi")
            if isinstance(raw_dpi, (tuple, list)):
                raw_dpi = raw_dpi[0] if raw_dpi else None
            try:
                dpi = float(raw_dpi) if raw_dpi else 96.0
            except (TypeError, ValueError):
                dpi = 96.0
            if dpi <= 1:
                dpi = 96.0

            record.pixel_width = width
            record.pixel_height = height
            record.dpi = round(dpi, 1)
            record.width_pt = round(width / dpi * 72, 2)
            record.height_pt = round(height / dpi * 72, 2)
            record.orientation = _orientation(width, height)

            if budget <= 0:
                record.status = "pending"
                return record

            thumbnail = image.convert("RGB")
            thumbnail.thumbnail(
                (settings.preview_max_width, int(settings.preview_max_width * 1.5)),
                Image.LANCZOS,
            )
            preview_path = preview_dir / _preview_name(page_number)
            thumbnail.save(preview_path, format="PNG", optimize=True)
            record.preview = preview_path.name
            record.pixel_width, record.pixel_height = width, height
    except Exception:
        record.status = "failed"
        record.error = "The image could not be read for preview."
    return record


def build_page_records(
    inputs: list[Path],
    *,
    output_path: Path | None,
    preview_dir: Path,
) -> list[PageRecord]:
    """Return page records for the produced result or, when it is not paginated,
    for the uploaded source files.

    - PDF result (including image-to-PDF, DOCX-to-PDF): inspect the result so
      the reported page count and previews match what the user downloads.
    - Otherwise inspect PDF/image inputs in their upload order.
    - Unsupported inputs yield no records rather than a fabricated page count.
    """
    preview_dir.mkdir(parents=True, exist_ok=True)
    budget = max(0, settings.preview_max_pages)

    if output_path is not None and output_path.suffix.lower() == ".pdf":
        return _pdf_records(output_path, preview_dir, start_page=1, budget=budget)

    records: list[PageRecord] = []
    for path in inputs:
        suffix = path.suffix.lower()
        if suffix == ".pdf":
            start = len(records) + 1
            chunk = _pdf_records(path, preview_dir, start_page=start, budget=budget)
            records.extend(chunk)
            budget -= sum(1 for record in chunk if record.preview)
        elif suffix in IMAGE_SUFFIXES:
            page_number = len(records) + 1
            record = _image_record(path, preview_dir, page_number, budget)
            if record.preview:
                budget -= 1
            records.append(record)
    return records


def preview_failures(records: list[PageRecord]) -> list[str]:
    """Human readable warnings for pages that could not be rendered."""
    failed = [record for record in records if record.status == "failed"]
    if not failed:
        return []
    numbers = ", ".join(str(record.page_number) for record in failed[:20])
    suffix = "…" if len(failed) > 20 else ""
    return [f"Pratinjau halaman {numbers}{suffix} tidak dapat dibuat; halaman lain tetap diproses."]
