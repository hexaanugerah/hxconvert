"""PDF -> DOCX with content-aware conversion.

Conversion rules:
- Scanned/image-only pages -> full-page images in Word (no OCR)
- Design/slide pages (text laid over artwork) -> full-page images, so the
  original composition is preserved exactly instead of being reflowed
- Digital text pages -> editable Word text
- Mixed pages -> layout preservation (pdf2docx)
- "Full Image" mode -> all pages as full-page images (300 DPI PNG)
- "Fast" mode -> all pages as lower-resolution JPEG images (no layout analysis)

Automatic OCR is DISABLED by default. OCR only runs when explicitly requested.

Performance notes:
- The document is analysed in ONE pass; per-page text blocks, image rects and
  vector-drawing statistics are cached and reused by every later stage.
- Full-image mode renders each page exactly once and embeds it immediately,
  avoiding intermediate PIL decode/re-encode/PNG-optimize roundtrips.
- pdfplumber (table detection) opens the file once per document, not once per page.
"""
from __future__ import annotations

import copy
import io
import logging
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import fitz
from docx import Document
from docx.enum.section import WD_ORIENT, WD_SECTION
from docx.enum.table import (
    WD_CELL_VERTICAL_ALIGNMENT,
    WD_ROW_HEIGHT_RULE,
    WD_TABLE_ALIGNMENT,
)
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK, WD_TAB_ALIGNMENT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Emu, Inches, Pt
from PIL import Image, ImageOps

from app.core.errors import ConversionError
from app.services.base import ConversionContext, ConversionResult
from app.services.capabilities import ocr_available
from app.services.ocr import ocr_pdf_page
from app.services.pdf_ops import _open_pdf, MAX_PAGES

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
logger = logging.getLogger(__name__)
logger.debug("pdf_to_docx layout detector v2 loaded")

BULLET_RE = re.compile(r"^(\s*)([•·▪‣⁃\-\*]\s+)(.*)$")
NUMBER_RE = re.compile(r"^(\s*)(\d+[\.\)]\s+)(.*)$")
# A form label ends with the colon; the value lives in the next table cell.
LABEL_COLON_RE = re.compile(r"^(?P<label>[^:]{1,40}?)\s*:\s*$")

HEADING_STYLE = {1: "Heading 1", 2: "Heading 2", 3: "Heading 3"}

# Fast mode: rasterising at 300 DPI (full-image mode) is what makes an
# image-heavy PDF cost several CPU-seconds per page. 72 DPI renders 17x fewer
# pixels than 300 DPI and still gives a readable screen preview; JPEG keeps the
# resulting DOCX small. Measured on 20 twelve-page photo PDFs: 26.8 s in auto
# mode vs 4.5 s in fast mode. ``fast_dpi`` raises the resolution when sharper
# output matters more than speed.
FAST_DEFAULT_DPI = 72
FAST_JPEG_QUALITY = 72


def _fast_dpi(ctx: ConversionContext) -> int:
    try:
        value = int(ctx.options.get("fast_dpi", FAST_DEFAULT_DPI) or FAST_DEFAULT_DPI)
    except (TypeError, ValueError):
        value = FAST_DEFAULT_DPI
    return max(72, min(200, value))


def _clean(text: str) -> str:
    """Normalize whitespace in text."""
    return re.sub(r"\s+", " ", text).strip()


def _line_text(line: dict) -> str:
    """Extract text from a line dict."""
    return "".join(span.get("text", "") for span in line.get("spans", []))


def _is_bold(span: dict) -> bool:
    flags = span.get("flags", 0)
    return bool(flags & 16)


def _is_italic(span: dict) -> bool:
    flags = span.get("flags", 0)
    return bool(flags & 2)


def _is_superscript(span: dict) -> bool:
    flags = span.get("flags", 0)
    return bool(flags & 4)


# --------------------------------------------------------------------------
# One-pass page analysis
# --------------------------------------------------------------------------


@dataclass
class PageInfo:
    """Everything the later stages need about one page, gathered in one pass."""

    index: int
    width: float
    height: float
    text_length: int
    blocks: list[dict]
    image_count: int
    dominant_image: bool
    significant_vector: bool
    complex_table: bool = False
    design_page: bool = False
    cls: str = field(default="")


def _has_complex_table(
    page: fitz.Page,
    page_area: float,
    text_length: int,
    drawings: list[dict],
) -> bool:
    """Detect a form/table that needs layout preservation instead of table reflow.

    pdf2docx keeps the original geometry, while the editable mode rebuilds
    tables from detected cell text. Rebuilding works for simple tables, but a
    wide form with merged cells and many columns loses its column widths and
    can overflow onto extra pages. Only large, wide tables are treated as
    complex; ordinary document tables stay in editable mode.
    """
    if text_length < 80:
        return False

    # A form grid is made of long horizontal and vertical vector lines. This
    # cheap check works even when optional table-detection helpers are missing
    # in the runtime image.
    page_width = page.rect.width
    page_height = page.rect.height
    long_horizontal = 0
    long_vertical = 0
    vertical_positions: set[float] = set()
    for drawing in drawings:
        for item in drawing.get("items", []):
            if item[0] != "l":
                continue
            start, end = item[1], item[2]
            dx = abs(end.x - start.x)
            dy = abs(end.y - start.y)
            if dx >= page_width * 0.25 and dy <= 2:
                long_horizontal += 1
            elif dy >= page_height * 0.15 and dx <= 2:
                long_vertical += 1
                vertical_positions.add(round(start.x, 1))
    # Four or more vertical rules mean a wide multi-column grid. A
    # two-column label/value form only has three and is better rebuilt as a
    # native Word table, where the label column can keep its real width.
    if long_horizontal >= 4 and len(vertical_positions) >= 5:
        logger.debug(
            "pdf_to_docx page=%s text=%s drawings=%s long_h=%s long_v=%s complex=True (grid)",
            page.number + 1,
            text_length,
            len(drawings),
            long_horizontal,
            long_vertical,
        )
        return True

    # Fallback for borderless tables: ask PyMuPDF, but only when there are
    # enough vector objects to suggest a structured page.
    if len(drawings) < 4:
        logger.debug(
            "pdf_to_docx page=%s text=%s drawings=%s long_h=%s long_v=%s complex=False (no grid)",
            page.number + 1,
            text_length,
            len(drawings),
            long_horizontal,
            long_vertical,
        )
        return False
    try:
        tables = page.find_tables()
    except Exception as exc:
        logger.debug(
            "pdf_to_docx page=%s find_tables failed: %s",
            page.number + 1,
            exc,
        )
        return False
    for table in tables.tables:
        columns = table.col_count or 0
        rows = table.row_count or 0
        area = fitz.Rect(table.bbox).get_area()
        # A two-column label/value form (the common SAP-style header) is
        # rebuilt more faithfully as a native Word table. Only wide forms with
        # several columns need pdf2docx's geometry-preserving layout engine.
        if area >= page_area * 0.30 and (
            columns >= 5 or (columns >= 3 and rows >= 6)
        ):
            logger.debug(
                "pdf_to_docx page=%s table=%sx%s area=%.2f complex=True (table)",
                page.number + 1,
                columns,
                rows,
                area / page_area,
            )
            return True
    logger.debug(
        "pdf_to_docx page=%s text=%s drawings=%s long_h=%s long_v=%s complex=False",
        page.number + 1,
        text_length,
        len(drawings),
        long_horizontal,
        long_vertical,
    )
    return False


def _is_design_page(
    page_area: float,
    text_length: int,
    blocks: list[dict],
    large_visual_rects: list[fitz.Rect],
) -> bool:
    """Detect slide/screenshot pages whose text is drawn on top of artwork.

    Canva/Figma exports, UI mockups and infographics place labels over photos
    and coloured panels. Rebuilding those layers as Word tables and floating
    text boxes moves every element, so the page must stay a single image to
    look the same. Ordinary documents, including form grids, have their text
    on the page background instead of inside large pictures or panels.
    """
    text_blocks = [block for block in blocks if block.get("type") == 0]
    if text_length < 15 or len(text_blocks) < 3 or not large_visual_rects:
        return False

    text_area = sum(
        fitz.Rect(block["bbox"]).get_area() for block in text_blocks
    )
    if text_area >= page_area * 0.35:
        return False

    inside = 0
    for block in text_blocks:
        rect = fitz.Rect(block["bbox"])
        if any(
            (rect & visual).get_area() > 0.5 * rect.get_area()
            for visual in large_visual_rects
        ):
            inside += 1
    return inside >= 3 and inside / len(text_blocks) >= 0.7


def _classify(
    page_area: float,
    meaningful_text: int,
    largest_image_area: float,
    images: list[tuple[int, Any]],
    significant_vector: bool,
    dominant_image: bool,
    complex_table: bool = False,
) -> str:
    """Classify a page's content type (identical rules to the previous
    ``_classify_page_content`` implementation):

    - "image-only": dominated by a large raster or has no meaningful content
    - "text-only": extractable digital text as the dominant content
    - "mixed": text plus real images/vector art
    - "vector": substantial filled vector art is the primary content
    """
    if dominant_image or (meaningful_text < 15 and not significant_vector and not images):
        return "image-only"
    # A large form table is "mixed": editable table reflow would destroy its
    # column geometry, so auto mode should use pdf2docx layout preservation.
    if complex_table:
        return "mixed"
    if meaningful_text > 80 and not dominant_image and not significant_vector:
        return "text-only"
    if significant_vector and meaningful_text < 60 and not dominant_image:
        return "vector"
    if meaningful_text > 15 and (images or significant_vector):
        return "mixed"
    if meaningful_text > 20 and not dominant_image:
        return "text-only"
    return "mixed"


def _analyze_pages(doc: fitz.Document) -> list[PageInfo]:
    """Read text blocks, image rectangles and vector statistics for every page
    exactly once. This replaces the previous repeated reads of the PDF
    (classification pass + body-font pass + header/footer pass + editable
    re-extraction each called ``get_text`` again)."""
    infos: list[PageInfo] = []
    for page_index in range(doc.page_count):
        page = doc[page_index]
        rect = page.rect
        page_area = max(rect.get_area(), 1.0)

        text = page.get_text("text").strip()
        blocks = [
            block
            for block in page.get_text("dict").get("blocks", [])
            if block.get("type") in (0, 1)
        ]

        largest_image_area = 0.0
        large_visual_rects: list[fitz.Rect] = []
        images = page.get_images(full=True)
        for img in images:
            for image_rect in page.get_image_rects(img[0]):
                area = image_rect.get_area()
                if area > largest_image_area:
                    largest_image_area = area
                if area >= page_area * 0.10:
                    large_visual_rects.append(image_rect)
        dominant_image = largest_image_area >= page_area * 0.65 and largest_image_area > 0

        significant_vector = False
        drawings = page.get_drawings()
        for drawing in drawings:
            drawing_rect = fitz.Rect(drawing["rect"])
            if drawing_rect.width >= 24 and drawing_rect.height >= 24:
                fill = drawing.get("fill")
                if fill and any(channel < 0.95 for channel in fill):
                    if drawing_rect.get_area() > page_area * 0.15:
                        significant_vector = True
                    # A large coloured panel/photo frame is a visual layer that
                    # text can sit on. Near-white fills and full-page
                    # backgrounds are excluded so ordinary pages do not match.
                    if page_area * 0.03 <= drawing_rect.get_area() < page_area * 0.90:
                        large_visual_rects.append(drawing_rect)
        complex_table = _has_complex_table(page, page_area, len(text), drawings)
        design_page = _is_design_page(
            page_area, len(text), blocks, large_visual_rects
        )
        del drawings  # only the computed flags are needed later

        infos.append(
            PageInfo(
                index=page_index,
                width=float(rect.width),
                height=float(rect.height),
                text_length=len(text),
                blocks=blocks,
                image_count=len(images),
                dominant_image=dominant_image,
                significant_vector=significant_vector,
                complex_table=complex_table,
                design_page=design_page,
                cls=_classify(
                    page_area,
                    len(text),
                    largest_image_area,
                    images,
                    significant_vector,
                    dominant_image,
                    complex_table,
                ),
            )
        )
    return infos


def _pages_size(doc: fitz.Document) -> None:
    pass


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def convert(ctx: ConversionContext) -> ConversionResult:
    """Main entry point for PDF to Word conversion.

    Modes:
    - "auto" (default): classify each page and convert appropriately
    - "layout": pdf2docx for all pages (preserves layout)
    - "editable": extract text for all pages (no OCR unless explicitly requested)
    - "full-image": render all pages as full-page images (no text extraction, no OCR)
    - "fast": render every page as a compressed JPEG at a low DPI. It skips the
      pdf2docx layout analysis (the slowest stage) and trades editability and
      fine detail for speed, which is what large batches of image-heavy PDFs need.
    """
    mode = str(ctx.options.get("mode", "auto") or "auto").lower()
    ocr_mode = str(ctx.options.get("ocr", "never") or "never").lower()

    doc = _open_pdf(ctx.input)
    try:
        if mode in {"full-image", "fast"}:
            pages_info = [
                (index + 1, float(doc[index].rect.width), float(doc[index].rect.height))
                for index in range(doc.page_count)
            ]
            total = doc.page_count
            if mode == "fast":
                return _convert_image_only(
                    ctx,
                    doc,
                    pages_info,
                    total,
                    dpi=_fast_dpi(ctx),
                    jpeg_quality=FAST_JPEG_QUALITY,
                )
            return _convert_image_only(ctx, doc, pages_info, total)

        page_analysis = _analyze_pages(doc)
        page_classes = [info.cls for info in page_analysis]
        page_count = doc.page_count
        source_image_count = sum(info.image_count for info in page_analysis)

        force_ocr = ocr_mode == "always"

        if mode == "layout":
            result = _convert_layout_mode(ctx, [], page_count, source_image_count)
        elif mode == "editable":
            result = _convert_editable(
                ctx, doc, page_analysis, force_ocr=force_ocr
            )
        else:
            result = _convert_auto_mode(
                ctx,
                doc,
                page_analysis,
                page_classes,
                page_count,
                source_image_count,
                force_ocr,
            )
        return result
    finally:
        doc.close()


def _convert_auto_mode(
    ctx: ConversionContext,
    doc: fitz.Document,
    page_analysis: list[PageInfo],
    page_classes: list[str],
    page_count: int,
    source_image_count: int,
    force_ocr: bool,
) -> ConversionResult:
    """Smart per-page conversion:

    - design/slide pages -> full-page images (the text is part of the artwork)
    - image-only pages -> full-page images
    - text-only pages -> editable text extraction
    - mixed/vector pages -> layout preservation via pdf2docx
    """
    design_pages = [info for info in page_analysis if info.design_page]
    visual_count = len(
        {
            info.index
            for info in page_analysis
            if info.design_page or info.cls == "image-only"
        }
    )
    if design_pages and visual_count >= math.ceil(page_count * 0.75):
        pages_info = [
            (info.index + 1, info.width, info.height) for info in page_analysis
        ]
        result = _convert_image_only(ctx, doc, pages_info, page_count)
        result.warnings.insert(
            0,
            "PDF ini berisi halaman desain/visual: teks menyatu dengan gambar dan "
            "bidang warna. Mode Otomatis merender setiap halaman sebagai gambar agar "
            "tampilan sama persis seperti PDF; teks tidak dapat diedit. Gunakan alat "
            "'PDF Pindai ke Word (OCR)' bila teks perlu diedit.",
        )
        return result

    # Mixed document with scanned pages: layout mode preserves everything visually.
    if "image-only" in page_classes and (
        "text-only" in page_classes or "mixed" in page_classes or "vector" in page_classes
    ):
        scanned_pages = [
            index + 1 for index, cls in enumerate(page_classes) if cls == "image-only"
        ]
        result = _convert_layout_mode(
            ctx, scanned_pages, page_count, source_image_count
        )
        _warn_complex_tables(result, page_analysis)
        return result

    # All pages are image-only: reuse the already analysed document, don't re-read it.
    if all(cls == "image-only" for cls in page_classes):
        pages_info = [
            (index + 1, info.width, info.height) for index, info in enumerate(page_analysis)
        ]
        return _convert_image_only(ctx, doc, pages_info, page_count)

    # All pages are text-only.
    if all(cls == "text-only" for cls in page_classes):
        return _convert_editable(ctx, doc, page_analysis, force_ocr=force_ocr)

    # Mixed text/mixed/vector (no image-only) - layout mode. Pages without any
    # meaningful text layer but with raster content are reported the same way
    # scanned pages are, because their text will not be editable without OCR.
    scanned_pages = [
        info.index + 1
        for info in page_analysis
        if info.text_length < 15 and info.image_count > 0
    ]
    result = _convert_layout_mode(ctx, scanned_pages, page_count, source_image_count)
    _warn_complex_tables(result, page_analysis)
    return result


def _warn_complex_tables(
    result: ConversionResult, page_analysis: list[PageInfo]
) -> None:
    """Explain why a form/table page was routed to the layout engine."""
    complex_pages = [info.index + 1 for info in page_analysis if info.complex_table]
    if not complex_pages:
        return
    result.warnings.insert(
        0,
        "Halaman "
        + ", ".join(str(page) for page in complex_pages[:15])
        + ("…" if len(complex_pages) > 15 else "")
        + " berisi tabel/form kompleks. Mode Otomatis memakai mesin tata letak "
        "agar posisi kolom dan ukuran halaman tetap sama seperti PDF asli.",
    )


def _convert_image_only(
    ctx: ConversionContext,
    doc: fitz.Document,
    pages_info: list[tuple[int, float, float]],
    total_pages: int,
    *,
    dpi: int = 300,
    jpeg_quality: int | None = None,
) -> ConversionResult:
    """Full-image / fast mode: render every PDF page as a full-page image in one
    Word document.

    This path is intentionally OCR-free and text-extraction-free. The PDF was
    opened once by the caller; pages are rendered one at a time, embedded into
    the document, and released from memory before the next page is rendered.

    ``dpi`` and ``jpeg_quality`` are the fast-mode knobs: a lower resolution and
    JPEG encoding avoid the PNG cost without any layout analysis. ``jpeg_quality
    = None`` keeps the lossless 300 DPI PNG behaviour of full-image mode.
    """
    import zipfile

    output = ctx.workdir / "converted.docx"
    warnings: list[str] = []

    pages_to_render = min(total_pages, MAX_PAGES)
    if total_pages > MAX_PAGES:
        warnings.append(
            f"Hanya {MAX_PAGES} halaman pertama yang dikonversi (batas keamanan)."
        )
    if jpeg_quality is not None:
        warnings.append(
            f"Mode cepat merender setiap halaman sebagai gambar JPEG {dpi} DPI. "
            "Konversi jauh lebih cepat, tetapi teks tidak dapat diedit dan detail "
            "kecil dapat berkurang; pilih mode tata letak atau edit bila teks perlu diedit."
        )
    pages_info = pages_info[:pages_to_render]

    document = Document()
    # python-docx starts from a template without body paragraphs, so the first
    # picture paragraph is created explicitly. A leading empty paragraph would
    # push the first full-page image past the bottom of a zero-margin page and
    # add a blank page before every image.
    paragraph = document.add_paragraph()
    paragraph.paragraph_format.space_before = Pt(0)
    paragraph.paragraph_format.space_after = Pt(0)
    paragraph.paragraph_format.line_spacing = 1.0
    run = paragraph.add_run()

    current_width = current_height = 0.0
    embedded = 0

    for index, (_number, w, h) in enumerate(pages_info):
        if index == 0:
            _apply_page_geometry(
                document.sections[0], w, h, (0.0, 0.0, 0.0, 0.0)
            )
            current_width, current_height = w, h
        elif abs(w - current_width) > 0.5 or abs(h - current_height) > 0.5:
            # Page size changed: start a new section with the new geometry.
            section = document.add_section(WD_SECTION.NEW_PAGE)
            _apply_page_geometry(section, w, h, (0.0, 0.0, 0.0, 0.0))
            paragraph = document.add_paragraph()
            paragraph.paragraph_format.space_before = Pt(0)
            paragraph.paragraph_format.space_after = Pt(0)
            paragraph.paragraph_format.line_spacing = 1.0
            run = paragraph.add_run()
            current_width, current_height = w, h
        else:
            # A page break inside the same run keeps the next image on a fresh
            # page without introducing an empty paragraph or section.
            run.add_break(WD_BREAK.PAGE)

        page = doc[_number - 1]
        try:
            scale = dpi / 72
            pixmap = page.get_pixmap(
                matrix=fitz.Matrix(scale, scale), alpha=False
            )
            # Word accepts PNG and JPEG bytes produced by PyMuPDF directly: skip
            # the PIL decode -> re-encode -> optimize roundtrip, which tripled
            # the CPU cost of every page.
            if jpeg_quality is None:
                image_bytes = pixmap.tobytes("png")
            else:
                image_bytes = pixmap.tobytes("jpg", jpg_quality=jpeg_quality)
            del pixmap
        except Exception as exc:
            warnings.append(f"Halaman {_number} gagal dirender: {exc}")
            continue
        try:
            # The section has zero margins, so the page box is exactly the
            # original page size. The image keeps the PDF aspect ratio.
            run.add_picture(
                io.BytesIO(image_bytes),
                width=Inches(w / 72),
                height=Inches(h / 72),
            )
            embedded += 1
        except Exception as exc:
            warnings.append(
                f"Gambar halaman {_number} tidak dapat disisipkan ke DOCX: {exc}"
            )
        finally:
            del image_bytes  # release the rendered page before the next one

    if not embedded:
        raise ConversionError(
            "Tidak dapat merender halaman PDF menjadi gambar."
        )

    document.save(output)

    media_in_docx = 0
    try:
        with zipfile.ZipFile(output) as archive:
            media_in_docx = len(
                [name for name in archive.namelist() if name.startswith("word/media/")]
            )
    except Exception:
        pass

    if media_in_docx < embedded:
        warnings.append(
            f"Hanya {media_in_docx} dari {embedded} halaman yang berhasil disematkan "
            "sebagai gambar dalam DOCX."
        )

    return ConversionResult(
        output,
        f"{Path(ctx.original_name).stem or 'document'}.docx",
        DOCX_MIME,
        warnings,
    )


def _unique_table_cells(table):
    """Yield every cell once; python-docx repeats merged cells per grid column."""
    seen: set[int] = set()
    for row in table.rows:
        for cell in row.cells:
            key = id(cell._tc)
            if key in seen:
                continue
            seen.add(key)
            yield cell
            for nested in cell.tables:
                yield from _unique_table_cells(nested)


def _layout_paragraphs(document):
    yield from document.paragraphs
    for table in document.tables:
        for cell in _unique_table_cells(table):
            yield from cell.paragraphs


# pdf2docx maps Calibri text to Carlito, its metric-compatible clone. Carlito
# is often missing on Windows, where Word then substitutes Cambria: every run
# becomes wider and labels that used to fit before a tab stop overflow.
# Calibri is the safer Windows name; LibreOffice maps it back to Carlito.
LAYOUT_FONT_ALIASES = {"Carlito": "Calibri"}


def _apply_layout_font_aliases(document) -> bool:
    changed = False
    for paragraph in _layout_paragraphs(document):
        for run in paragraph.runs:
            properties = run._r.rPr
            if properties is None:
                continue
            fonts = properties.find(qn("w:rFonts"))
            if fonts is None:
                continue
            for attribute in ("w:ascii", "w:hAnsi", "w:eastAsia", "w:cs"):
                value = fonts.get(qn(attribute))
                replacement = LAYOUT_FONT_ALIASES.get(value)
                if replacement and value != replacement:
                    fonts.set(qn(attribute), replacement)
                    changed = True
    return changed


def _colon_line(paragraph) -> tuple[int, int, bool] | None:
    """Return (colon index, label end, has tab) for a ``Label: value`` line.

    Only short labels qualify, so ordinary prose containing a colon is left
    alone. ``label_end`` points past the last label character; spaces and tabs
    used to pad the colon are excluded.
    """
    text = paragraph.text
    if not text or "\n" in text:
        return None
    colon = text.find(":")
    if colon < 1 or colon > 40:
        return None
    label = text[:colon]
    value = text[colon + 1 :].strip()
    if not label.strip():
        return None
    if not value and not text.rstrip().endswith(":"):
        return None
    stripped = label.strip()
    if not stripped[0].isalnum() or "://" in text:
        return None
    if colon > 0 and colon + 1 < len(text):
        if text[colon - 1].isdigit() and text[colon + 1].isdigit():
            return None  # time such as 08:00
    label_end = colon
    while label_end > 0 and text[label_end - 1] in " \t":
        label_end -= 1
    if label_end == 0:
        return None
    return colon, label_end, "\t" in label


def _colon_groups(paragraphs) -> list[list[tuple[object, tuple[int, int, bool]]]]:
    """Group consecutive ``Label: value`` paragraphs inside one container."""
    groups: list[list[tuple[object, tuple[int, int, bool]]]] = []
    current: list[tuple[object, tuple[int, int, bool]]] = []
    for paragraph in paragraphs:
        info = _colon_line(paragraph)
        if info is None:
            if len(current) >= 2:
                groups.append(current)
            current = []
        else:
            current.append((paragraph, info))
    if len(current) >= 2:
        groups.append(current)
    return groups


def _append_run_copy(paragraph, source_run, text: str) -> None:
    run = paragraph.add_run()
    if source_run._r.rPr is not None:
        run._r.insert(0, copy.deepcopy(source_run._r.rPr))
    run.text = text


def _rewrite_colon_paragraph(paragraph, colon: int, label_end: int) -> bool:
    """Replace the padding before a colon with one tab and keep the formatting."""
    if paragraph._p.xpath(
        ".//w:hyperlink | .//w:fldSimple | .//w:drawing | .//w:object | .//w:instrText"
    ):
        return False
    text = paragraph.text
    runs = paragraph.runs
    ranges: list[tuple[int, int, object]] = []
    offset = 0
    for run in runs:
        value = run.text
        ranges.append((offset, offset + len(value), run))
        offset += len(value)
    if offset != len(text):
        return False
    colon_run = next(
        (run for start, end, run in ranges if start <= colon < end), None
    )
    if colon_run is None:
        return False

    for run in list(runs):
        run._r.getparent().remove(run._r)

    for start, end, run in ranges:
        left, right = max(start, 0), min(end, label_end)
        if left < right:
            _append_run_copy(paragraph, run, text[left:right])

    _append_run_copy(paragraph, colon_run, "\t")

    raw_after = text[colon:]
    needs_space = len(raw_after) > 1 and not raw_after[1].isspace()
    first = True
    for start, end, run in ranges:
        left, right = max(start, colon), min(end, len(text))
        if left >= right:
            continue
        chunk = text[left:right]
        if first:
            if needs_space:
                chunk = chunk[:1] + " " + chunk[1:]
            first = False
        _append_run_copy(paragraph, run, chunk)
    return True


def _has_tab_stop(paragraph, position: int) -> bool:
    for stop in paragraph.paragraph_format.tab_stops:
        if stop.position is not None and abs(int(stop.position) - position) <= 635:
            return True
    return False


def _set_tab_stop(paragraph, position: int) -> None:
    paragraph.paragraph_format.tab_stops.clear_all()
    paragraph.paragraph_format.tab_stops.add_tab_stop(Emu(position))


# pdf2docx creates table cells with zero margins. A value cell whose text
# begins with ':' can therefore be drawn on top of the cell border. Keep that
# text just inside the border; the shift is under two points wide.
BORDER_TEXT_INSET = Emu(45720)  # 0.05 inch
UNSAFE_TAB_RESERVE = Emu(109728)  # 0.12 inch


def _cell_text_width(paragraph) -> int | None:
    """Width of the table cell holding ``paragraph``, in EMU, if known."""
    element = paragraph._p.getparent()
    while element is not None and element.tag != qn("w:tc"):
        element = element.getparent()
    if element is None:
        return None
    properties = element.find(qn("w:tcPr"))
    if properties is None:
        return None
    width = properties.find(qn("w:tcW"))
    if width is None or width.get(qn("w:type")) not in (None, "dxa"):
        return None
    try:
        twips = int(width.get(qn("w:w")) or 0)
    except (TypeError, ValueError):
        return None
    if twips <= 0:
        return None
    # One twip is 635 EMU.
    return twips * 635


def _inset_border_colon_text(document) -> bool:
    """Move value cells that start with ':' away from the cell border."""
    changed = False
    for table in document.tables:
        for cell in _unique_table_cells(table):
            for paragraph in cell.paragraphs:
                if not paragraph.text.lstrip().startswith(":"):
                    continue
                current = paragraph.paragraph_format.left_indent
                current_emu = int(current) if current is not None else 0
                if current_emu < BORDER_TEXT_INSET:
                    paragraph.paragraph_format.left_indent = BORDER_TEXT_INSET
                    changed = True
    return changed


def _align_colon_columns(document) -> bool:
    """Keep label/value colons in one vertical column.

    pdf2docx inserts a tab stop when a PDF stores the label and its value as
    separate text blocks. When both are one text line (common in generated
    forms), the colon is left in the text flow and Word renders it a few points
    away from the neighbouring rows. In a container that already has at least
    one tab-aligned ``Label: value`` line, every other such line is moved onto
    the same tab stop.
    """
    containers = [document.paragraphs]
    for table in document.tables:
        for cell in _unique_table_cells(table):
            containers.append(cell.paragraphs)

    changed = False
    for paragraphs in containers:
        for group in _colon_groups(paragraphs):
            positions = [
                int(stop.position)
                for paragraph, _info in group
                for stop in paragraph.paragraph_format.tab_stops
                if stop.position is not None
            ]
            if not positions:
                continue
            target = Counter(positions).most_common(1)[0][0]
            unsafe = False
            for paragraph, _info in group:
                width = _cell_text_width(paragraph)
                if width is not None and target > width - UNSAFE_TAB_RESERVE:
                    # The tab would land on the cell border; leave the row alone.
                    unsafe = True
                    break
            if unsafe:
                continue
            for paragraph, (colon, label_end, has_tab) in group:
                if has_tab:
                    if not _has_tab_stop(paragraph, target):
                        _set_tab_stop(paragraph, target)
                        changed = True
                    continue
                if _rewrite_colon_paragraph(paragraph, colon, label_end):
                    _set_tab_stop(paragraph, target)
                    changed = True
    return changed


def _convert_layout_mode(
    ctx: ConversionContext,
    scanned_pages: list[int],
    page_count: int,
    source_image_count: int,
) -> ConversionResult:
    """Convert with pdf2docx, which preserves page geometry and visual objects."""
    from pdf2docx import Converter

    output = ctx.workdir / "converted.docx"
    converter = Converter(str(ctx.input))
    try:
        converter.convert(str(output))
    finally:
        converter.close()

    if not output.is_file() or output.stat().st_size < 1000:
        raise ConversionError("The layout engine produced an empty Word file.")
    try:
        document = Document(str(output))
    except Exception as exc:
        raise ConversionError(
            "The layout engine produced an unreadable Word file."
        ) from exc

    text = "".join(paragraph.text for paragraph in document.paragraphs).strip()
    if not text and not document.tables and not document.inline_shapes:
        raise ConversionError("The layout engine did not preserve any readable content.")

    # pdf2docx sets page dimensions but leaves the orientation flag at portrait.
    changed = False
    for section in document.sections:
        desired = (
            WD_ORIENT.LANDSCAPE
            if section.page_width > section.page_height
            else WD_ORIENT.PORTRAIT
        )
        if section.orientation != desired:
            section.orientation = desired
            changed = True
    if _apply_layout_font_aliases(document):
        changed = True
    if _align_colon_columns(document):
        changed = True
    if _inset_border_colon_text(document):
        changed = True
    if changed:
        document.save(output)

    import zipfile

    media: list[str] = []
    try:
        with zipfile.ZipFile(output) as archive:
            media = [
                name for name in archive.namelist() if name.startswith("word/media/")
            ]
    except Exception:
        pass

    warnings = [
        "Mode tata letak memakai pdf2docx: ukuran halaman, posisi teks, tabel, dan "
        "gambar dipertahankan. Teks tetap dapat diedit, tetapi disusun dalam kotak teks "
        "mengambang sehingga alur paragraf dapat berbeda dari mode edit.",
        "Font PDF dipetakan ke font Word terdekat; font asli tidak disematkan.",
    ]
    if scanned_pages:
        warnings.append(
            "Halaman "
            + ", ".join(str(page) for page in scanned_pages[:15])
            + ("…" if len(scanned_pages) > 15 else "")
            + " tidak memiliki lapisan teks yang memadai. Halaman tersebut dipertahankan "
            "secara visual; gunakan alat 'PDF Pindai ke Word (OCR)' agar teksnya dapat diedit."
        )
    if source_image_count and not media:
        warnings.append(
            "PDF sumber berisi gambar, tetapi tidak ada gambar yang tersimpan di DOCX hasil "
            "mesin tata letak. Gunakan alat 'PDF Pindai ke Word (OCR)' sebagai alternatif."
        )
    if len(scanned_pages) == page_count:
        warnings.append(
            "Seluruh dokumen tampak hasil pindai. Untuk Word yang dapat diedit sepenuhnya, "
            "gunakan alat 'PDF Pindai ke Word (OCR)'."
        )
    return ConversionResult(
        output,
        f"{Path(ctx.original_name).stem or 'document'}.docx",
        DOCX_MIME,
        warnings,
    )


def convert_ocr(ctx: ConversionContext) -> ConversionResult:
    doc = _open_pdf(ctx.input)
    try:
        analysis = _analyze_pages(doc)
        return _convert_editable(ctx, doc, analysis, force_ocr=True)
    finally:
        doc.close()


def _convert_editable(
    ctx: ConversionContext,
    doc: fitz.Document,
    page_analysis: list[PageInfo],
    *,
    force_ocr: bool,
) -> ConversionResult:
    warnings: list[str] = [
        "Mode edit: teks diekstrak menjadi paragraf Word yang dapat Anda ubah langsung; "
        "lebar kolom tabel dipertahankan dari PDF, tetapi pemenggalan baris dan posisi "
        "paragraf bisa sedikit berbeda dari tampilan aslinya."
    ]
    if doc.page_count > MAX_PAGES:
        page_limit = MAX_PAGES
        warnings.append(
            f"Hanya {page_limit} halaman pertama yang diproses (batas keamanan)."
        )
    else:
        page_limit = doc.page_count

    # OCR only runs when explicitly set to "always" or force_ocr=True
    ocr_mode = "always" if force_ocr else str(ctx.options.get("ocr", "never")).lower()
    language = str(ctx.options.get("language", "eng"))
    keep_tables = bool(ctx.options.get("tables", True)) and not force_ocr
    page_breaks = bool(ctx.options.get("page_breaks", False))
    body_size = _body_font_size(page_analysis)
    headers = _header_footer_texts(page_analysis)
    used_ocr_pages: list[int] = []
    two_column_pages: list[int] = []
    copied_images = 0

    document = Document()
    section = document.sections[0]
    first = page_analysis[0]
    _apply_page_geometry(
        section,
        first.width,
        first.height,
        _detect_margins(first),
    )
    current_size = (first.width, first.height)
    document.core_properties.title = Path(ctx.original_name).stem

    tables_source = _TableSource(ctx.input)

    try:
        for info in page_analysis[:page_limit]:
            page_index = info.index
            page = doc[page_index]
            width, height = info.width, info.height
            geometry_changed = (
                abs(width - current_size[0]) > 2 or abs(height - current_size[1]) > 2
            )
            if page_index > 0:
                if geometry_changed:
                    section = document.add_section(WD_SECTION.NEW_PAGE)
                    _apply_page_geometry(
                        section, width, height, _detect_margins(info)
                    )
                    current_size = (width, height)
                elif page_breaks:
                    document.add_paragraph().add_run().add_break(WD_BREAK.PAGE)
            available_width_inches = max(
                1.0,
                (width - section.left_margin.pt - section.right_margin.pt) / 72,
            )
            available_height_inches = max(
                1.0,
                (height - section.top_margin.pt - section.bottom_margin.pt) / 72,
            )

            needs_ocr = force_ocr or ocr_mode == "always"
            if ocr_mode == "never" and not force_ocr:
                needs_ocr = False

            if needs_ocr:
                text = page.get_text("text").strip()
                if not (text or ocr_available()):
                    warnings.append(
                        f"Halaman {page_index + 1} tampaknya hasil pindai dan tidak "
                        "memiliki lapisan teks. OCR tidak tersedia, sehingga halaman "
                        "ini dilewati."
                    )
                    continue
                try:
                    text = ocr_pdf_page(page, language=language)
                except Exception as exc:
                    warnings.append(f"OCR gagal pada halaman {page_index + 1}: {exc}")
                    continue
                if not text:
                    warnings.append(
                        f"OCR tidak menemukan teks pada halaman {page_index + 1}."
                    )
                    continue
                used_ocr_pages.append(page_index + 1)
                for content, kind in _paragraphs_from_ocr(text):
                    if kind == "li":
                        match = BULLET_RE.match(content) or NUMBER_RE.match(content)
                        if match:
                            style = (
                                "List Number"
                                if NUMBER_RE.match(content)
                                else "List Bullet"
                            )
                            document.add_paragraph(match.group(2), style=style)
                        else:
                            document.add_paragraph(content, style="List Bullet")
                    else:
                        document.add_paragraph(content)
                continue

            blocks = info.blocks

            tables: list[DetectedTable] = []
            if keep_tables:
                try:
                    tables = tables_source.collect(page_index)
                except Exception:
                    warnings.append(
                        f"Tabel pada halaman {page_index + 1} tidak dapat dianalisis."
                    )

            text_blocks = [block for block in blocks if block.get("type") == 0]
            left = [
                block
                for block in text_blocks
                if fitz.Rect(block["bbox"]).x1 <= width / 2 + 4
            ]
            right = [
                block
                for block in text_blocks
                if fitz.Rect(block["bbox"]).x0 >= width / 2 - 4
            ]
            two_column = len(left) >= 2 and len(right) >= 2 and not tables
            if two_column:
                two_column_pages.append(page_index + 1)

            if two_column:
                ordered = sorted(left, key=lambda b: b["bbox"][1]) + sorted(
                    right, key=lambda b: b["bbox"][1]
                )
            else:
                ordered = sorted(blocks, key=lambda b: b["bbox"][1])

            table_rects = [fitz.Rect(table.bbox) for table in tables]
            emitted: set[int] = set()

            if not two_column:
                interleaved: list[tuple[float, str, object]] = []
                for block in ordered:
                    interleaved.append(
                        (block["bbox"][1], block.get("type", 0), block)
                    )
                for index, table in enumerate(tables):
                    interleaved.append((table.bbox[1], 2, (index, table)))
                interleaved.sort(key=lambda item: item[0])
                ordered = [item[2] for item in interleaved]

            for item in ordered:
                if isinstance(item, tuple):
                    index, table = item
                    if index in emitted:
                        continue
                    emitted.add(index)
                    _add_table(
                        document,
                        table,
                        max(1.0, width / 72 - 0.05),
                        (table.bbox[0] - section.left_margin.pt) / 72,
                    )
                    continue

                block = item
                if block.get("type") == 1:
                    if _add_image(
                        document,
                        block,
                        available_width_inches,
                        available_height_inches,
                        page,
                        page_index + 1,
                        warnings,
                    ):
                        copied_images += 1
                    continue

                block_rect = fitz.Rect(block["bbox"])
                if table_rects and any(
                    (block_rect & rect).get_area()
                    > 0.5 * max(block_rect.get_area(), 1)
                    for rect in table_rects
                ):
                    continue
                _add_text_block(document, block, body_size, headers, warnings)
    finally:
        tables_source.close()

    if used_ocr_pages:
        warnings.insert(
            0,
            "Teks pada halaman "
            + ", ".join(str(page) for page in used_ocr_pages[:20])
            + " dihasilkan melalui OCR sehingga mungkin memiliki kesalahan pengenalan.",
        )
    if two_column_pages:
        warnings.append(
            "Halaman "
            + ", ".join(str(page) for page in two_column_pages[:15])
            + " terdeteksi sebagai dua kolom; urutan bacaan mungkin tidak sempurna."
        )

    output = ctx.workdir / "converted.docx"
    document.save(output)

    return ConversionResult(
        output,
        f"{Path(ctx.original_name).stem or 'document'}.docx",
        DOCX_MIME,
        warnings,
    )


def _line_runs(line: dict) -> list[tuple[str, bool, bool, float, bool]]:
    runs: list[tuple[str, bool, bool, float, bool]] = []
    for span in line.get("spans", []):
        text = span.get("text", "")
        if not text:
            continue
        runs.append(
            (
                text,
                _is_bold(span),
                _is_italic(span),
                float(span.get("size", 11)),
                _is_superscript(span),
            )
        )
    return runs


def _join_line_texts(lines: list[str]) -> str:
    result = ""
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if not result:
            result = line
        elif result.endswith("-") and not result.endswith("--"):
            result = result[:-1] + line
        else:
            result = f"{result} {line}"
    return _clean(result)


def _body_font_size(page_analysis: list[PageInfo]) -> float:
    sizes: Counter[float] = Counter()
    for info in page_analysis:
        for block in info.blocks:
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    text = span.get("text", "").strip()
                    if len(text) >= 12:
                        sizes[round(float(span.get("size", 11)) * 2) / 2] += len(text)
    if not sizes:
        return 11.0
    return sizes.most_common(1)[0][0]


def _header_footer_texts(page_analysis: list[PageInfo]) -> set[str]:
    """Find recurring running headers/footers so they are not duplicated."""
    occurrences: Counter[str] = Counter()
    sampled = 0
    for info in page_analysis:
        height = info.height
        candidates: set[str] = set()
        for block in info.blocks:
            if block.get("type") != 0:
                continue
            rect = fitz.Rect(block["bbox"])
            text = _clean(" ".join(_line_text(line) for line in block.get("lines", [])))
            if not text or len(text) > 120:
                continue
            if rect.y1 < height * 0.075 or rect.y0 > height * 0.925:
                candidates.add(text)
        for text in candidates:
            occurrences[text] += 1
        sampled += 1
    if sampled < 3:
        return set()
    threshold = max(3, int(sampled * 0.6))
    return {text for text, count in occurrences.items() if count >= threshold}


# Borders that can appear on a detected table. An empty ``DetectedTable.borders``
# tuple means the source grid could not be inspected and a full grid is used.
TABLE_BORDER_SIDES = ("top", "left", "bottom", "right", "insideH", "insideV")


@dataclass
class DetectedTable:
    """A table found by pdfplumber, with the geometry Word needs to keep it."""

    bbox: tuple[float, float, float, float]
    rows: list[list[str]]
    # Source column widths in PDF points, left to right. Empty when the
    # detector could not provide a usable grid.
    column_widths: list[float] = field(default_factory=list)
    # Source row heights in points, top to bottom. Empty when unknown.
    row_heights: list[float] = field(default_factory=list)
    # Borders that actually exist in the PDF. This keeps a form box with one
    # vertical divider from gaining a full grid of extra row lines.
    borders: tuple[str, ...] = ()


class _TableSource:
    """pdfplumber opened once per document.

    The previous implementation opened the whole PDF for every page to look
    for tables, which multiplied I/O the longer the document was.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._pdf = None

    def collect(self, page_index: int) -> list[DetectedTable]:
        import pdfplumber

        if self._pdf is None:
            self._pdf = pdfplumber.open(str(self.path))
        if page_index >= len(self._pdf.pages):
            return []
        page = self._pdf.pages[page_index]
        tables: list[DetectedTable] = []
        for table in page.find_tables():
            raw_rows = table.extract()
            cleaned_rows: list[list[str]] = []
            kept_indices: list[int] = []
            for row_index, row in enumerate(raw_rows):
                values = ["" if cell is None else str(cell).strip() for cell in row]
                if any(values):
                    cleaned_rows.append(values)
                    kept_indices.append(row_index)
            if not cleaned_rows:
                continue
            columns = max(len(row) for row in cleaned_rows)
            if columns < 2:
                continue

            row_heights: list[float] = []
            if len(kept_indices) == len(raw_rows):
                try:
                    row_heights = [
                        max(0.0, float(row.bbox[3]) - float(row.bbox[1]))
                        for row in table.rows
                    ]
                except (AttributeError, IndexError, TypeError, ValueError):
                    row_heights = []

            # A form box with one vertical divider but no row rules is read by
            # pdfplumber as a single tall row with multi-line cells. Rebuild
            # the logical rows from the text positions so the form stays a
            # table instead of collapsing into loose paragraphs.
            if len(cleaned_rows) == 1:
                rebuilt = _split_merged_table_row(page, table)
                if rebuilt is not None:
                    cleaned_rows, row_heights = rebuilt
            if len(cleaned_rows) < 2:
                continue

            widths: list[float] = []
            try:
                widths = [
                    max(0.0, float(column.bbox[2]) - float(column.bbox[0]))
                    for column in table.columns
                ]
            except (AttributeError, IndexError, TypeError, ValueError):
                widths = []
            if len(widths) != columns or sum(widths) <= 0:
                widths = []
            tables.append(
                DetectedTable(
                    bbox=tuple(table.bbox),
                    rows=cleaned_rows,
                    column_widths=widths,
                    row_heights=row_heights,
                    borders=_detect_table_borders(page, table.bbox),
                )
            )
        return tables

    def close(self) -> None:
        if self._pdf is not None:
            try:
                self._pdf.close()
            except Exception:  # pragma: no cover - defensive
                pass
            self._pdf = None


def _split_merged_table_row(
    page, table
) -> tuple[list[list[str]], list[float]] | None:
    """Rebuild rows for a table pdfplumber merged into one tall row.

    Form boxes often have an outer border and a single vertical divider but no
    horizontal rules. pdfplumber then returns one row whose cells contain every
    label or value on separate lines. Grouping the words by their vertical
    position recovers the real rows, including rows whose value is empty.
    """
    if len(table.rows) != 1:
        return None
    cell_boxes = list(table.rows[0].cells)
    # Only two-column label/value forms are safe to split this way; a wide
    # merged table may legitimately contain multi-line cells in one row.
    if len(cell_boxes) != 2 or len(table.columns) != 2:
        return None

    entries: list[tuple[float, int, str]] = []
    for column_index, cell_bbox in enumerate(cell_boxes):
        try:
            words = page.crop(cell_bbox, strict=False).extract_words()
        except Exception:
            return None
        lines: list[tuple[float, list[dict]]] = []
        for word in sorted(words, key=lambda w: (float(w["top"]), float(w["x0"]))):
            top = float(word["top"])
            if lines and abs(top - lines[-1][0]) <= 3:
                lines[-1][1].append(word)
            else:
                lines.append((top, [word]))
        for top, line_words in lines:
            text = " ".join(
                word["text"]
                for word in sorted(line_words, key=lambda w: float(w["x0"]))
            ).strip()
            if text:
                entries.append((top, column_index, text))

    if len(entries) < 4:
        return None
    entries.sort(key=lambda entry: (entry[0], entry[1]))

    logical: list[tuple[float, dict[int, str]]] = []
    for top, column_index, text in entries:
        if logical and abs(top - logical[-1][0]) <= 4:
            row = logical[-1][1]
            row[column_index] = (row.get(column_index, "") + " " + text).strip()
        else:
            logical.append((top, {column_index: text}))
    if len(logical) < 2:
        return None

    rows: list[list[str]] = []
    heights: list[float] = []
    table_bottom = float(table.bbox[3])
    for index, (top, values) in enumerate(logical):
        rows.append([values.get(column, "") for column in range(len(cell_boxes))])
        if index + 1 < len(logical):
            height = logical[index + 1][0] - top
        else:
            height = table_bottom - top
        heights.append(max(10.0, min(height, 96.0)))
    return rows, heights


def _detect_table_borders(page, bbox) -> tuple[str, ...]:
    """Return the source borders that exist around and inside ``bbox``."""
    try:
        x0, top, x1, bottom = (float(value) for value in bbox)
        edges = list(page.edges)
    except (AttributeError, TypeError, ValueError):
        return TABLE_BORDER_SIDES
    if x1 <= x0 or bottom <= top or not edges:
        return TABLE_BORDER_SIDES

    tolerance = 3.0
    width = x1 - x0
    height = bottom - top
    found: set[str] = set()
    for edge in edges:
        try:
            ex0 = float(edge["x0"])
            ex1 = float(edge["x1"])
            etop = float(edge["top"])
            ebottom = float(edge["bottom"])
        except (KeyError, TypeError, ValueError):
            continue
        horizontal = abs(ebottom - etop) <= tolerance
        vertical = abs(ex1 - ex0) <= tolerance
        spans_width = ex0 <= x0 + tolerance and ex1 >= x1 - tolerance
        spans_height = etop <= top + tolerance and ebottom >= bottom - tolerance
        if horizontal and ex1 - ex0 >= width * 0.5:
            if spans_width and abs(etop - top) <= tolerance:
                found.add("top")
            if spans_width and abs(etop - bottom) <= tolerance:
                found.add("bottom")
            if top + tolerance < etop < bottom - tolerance:
                found.add("insideH")
        elif vertical and ebottom - etop >= height * 0.5:
            if spans_height and abs(ex0 - x0) <= tolerance:
                found.add("left")
            if spans_height and abs(ex0 - x1) <= tolerance:
                found.add("right")
            if x0 + tolerance < ex0 < x1 - tolerance:
                found.add("insideV")
    if not found:
        return TABLE_BORDER_SIDES
    return tuple(side for side in TABLE_BORDER_SIDES if side in found)


def _block_text(block: dict) -> tuple[str, list[tuple[str, bool, bool, float, bool]]]:
    lines = []
    runs: list[tuple[str, bool, bool, float, bool]] = []
    for line in block.get("lines", []):
        text = _line_text(line)
        if text:
            lines.append(text)
            runs.extend(_line_runs(line))
    return _join_line_texts(lines), runs


def _classify_text_block_for_heading(
    text: str,
    max_size: float,
    body_size: float,
    bold: bool,
    line_count: int,
) -> tuple[str, int | None]:
    """Return ('p' | 'h' | 'li', level) for a block of text."""
    bullet = BULLET_RE.match(text)
    numbered = NUMBER_RE.match(text)
    if bullet or numbered:
        return "li", 1 if numbered else 0
    if line_count <= 3 and len(text) <= 120:
        ratio = max_size / body_size if body_size else 1
        if ratio >= 1.45:
            return "h", 1
        if ratio >= 1.24:
            return "h", 2
        if ratio >= 1.1 and bold:
            return "h", 3
        if ratio >= 1.6:
            return "h", 1
    return "p", None


def _add_runs(
    paragraph,
    runs: list[tuple[str, bool, bool, float, bool]],
    *,
    bold_all: bool = False,
) -> None:
    for text, bold, italic, size, superscript in runs:
        if not text:
            continue
        run = paragraph.add_run(text)
        run.bold = True if bold_all else bold
        run.italic = italic
        if superscript:
            run.font.superscript = True
        if size and 6 <= size <= 48:
            run.font.size = Pt(round(size * 2) / 2)


# Word's built-in table grid adds a wide default padding. These values keep
# text close to the source position while still leaving the cell border clear.
TABLE_LEFT_MARGIN_TWIPS = 60
TABLE_RIGHT_MARGIN_TWIPS = 40


def _table_column_widths(
    detected: DetectedTable, columns: int, max_width_inches: float
) -> list[float]:
    """Column widths in points, taken from the PDF unless they do not fit."""
    max_width_pt = max(36.0, max_width_inches * 72)
    widths = [float(width) for width in detected.column_widths]
    if len(widths) != columns or sum(widths) <= 0:
        fallback = float(detected.bbox[2]) - float(detected.bbox[0])
        if fallback <= 0:
            fallback = max_width_pt
        fallback = min(fallback, max_width_pt)
        return [fallback / columns] * columns
    widths = [max(18.0, width) for width in widths]
    total = sum(widths)
    if total > max_width_pt:
        scale = max_width_pt / total
        widths = [width * scale for width in widths]
    return widths


def _is_label_value_table(rows: list[list[str]]) -> bool:
    """True when the first column holds labels for the second column.

    Some PDFs keep the colon in the label cell (``Label :``), others draw the
    colon as the first character of the value cell (``: value``). Both are the
    same form layout and should be rebuilt the same way.
    """
    value_lines = [
        line.strip()
        for row in rows
        for line in (row[1] if len(row) > 1 else "").splitlines()
        if line.strip()
    ]
    values_have_colons = len(value_lines) >= 2 and sum(
        1 for line in value_lines if line.startswith(":")
    ) >= math.ceil(len(value_lines) * 0.6)

    lines = 0
    labels = 0
    for row in rows:
        value = row[0] if row else ""
        for line in value.splitlines():
            line = line.strip()
            if not line:
                continue
            lines += 1
            if LABEL_COLON_RE.match(line) or values_have_colons:
                labels += 1
    return labels >= 2 and labels >= math.ceil(lines * 0.6)


def _set_table_layout(table, total_width_pt: float, indent_pt: float) -> None:
    """Pin the table to the source width and position (fixed layout)."""
    tbl_pr = table._tbl.tblPr
    tbl_width = tbl_pr.find(qn("w:tblW"))
    if tbl_width is not None:
        tbl_width.set(qn("w:type"), "dxa")
        tbl_width.set(qn("w:w"), str(int(total_width_pt * 20)))
    tbl_indent = OxmlElement("w:tblInd")
    tbl_indent.set(qn("w:w"), str(round(indent_pt * 20)))
    tbl_indent.set(qn("w:type"), "dxa")
    tbl_pr.insert_element_before(
        tbl_indent,
        "w:tblBorders",
        "w:shd",
        "w:tblLayout",
        "w:tblCellMar",
        "w:tblLook",
        "w:tblCaption",
        "w:tblDescription",
        "w:tblPrChange",
    )


def _set_table_cell_margins(table) -> None:
    tbl_pr = table._tbl.tblPr
    existing = tbl_pr.find(qn("w:tblCellMar"))
    if existing is not None:
        tbl_pr.remove(existing)
    margins = OxmlElement("w:tblCellMar")
    for side, value in (
        ("top", 0),
        ("left", TABLE_LEFT_MARGIN_TWIPS),
        ("bottom", 0),
        ("right", TABLE_RIGHT_MARGIN_TWIPS),
    ):
        node = OxmlElement(f"w:{side}")
        node.set(qn("w:w"), str(value))
        node.set(qn("w:type"), "dxa")
        margins.append(node)
    tbl_pr.insert_element_before(
        margins,
        "w:tblLook",
        "w:tblCaption",
        "w:tblDescription",
        "w:tblPrChange",
    )


def _set_table_borders(table, sides: tuple[str, ...]) -> None:
    """Draw only the borders that exist in the source PDF."""
    tbl_pr = table._tbl.tblPr
    existing = tbl_pr.find(qn("w:tblBorders"))
    if existing is not None:
        tbl_pr.remove(existing)
    borders = OxmlElement("w:tblBorders")
    for side in TABLE_BORDER_SIDES:
        node = OxmlElement(f"w:{side}")
        if side in sides:
            node.set(qn("w:val"), "single")
            node.set(qn("w:sz"), "6")
            node.set(qn("w:space"), "0")
            node.set(qn("w:color"), "000000")
        else:
            node.set(qn("w:val"), "nil")
        borders.append(node)
    tbl_pr.insert_element_before(
        borders,
        "w:shd",
        "w:tblLayout",
        "w:tblCellMar",
        "w:tblLook",
        "w:tblCaption",
        "w:tblDescription",
        "w:tblPrChange",
    )


def _add_table(
    document,
    detected: DetectedTable,
    max_width_inches: float,
    indent_inches: float = 0.0,
):
    rows = detected.rows
    columns = max(len(row) for row in rows)
    widths = _table_column_widths(detected, columns, max_width_inches)
    table = document.add_table(rows=len(rows), cols=columns)
    table.style = "Table Grid"
    table.autofit = False
    table.alignment = WD_TABLE_ALIGNMENT.LEFT
    _set_table_layout(table, sum(widths), indent_inches * 72)
    _set_table_cell_margins(table)
    _set_table_borders(
        table, detected.borders if detected.borders else TABLE_BORDER_SIDES
    )
    for column_index, width in enumerate(widths):
        table.columns[column_index].width = Pt(width)
    for row_index, height in enumerate(detected.row_heights[: len(rows)]):
        table_row = table.rows[row_index]
        table_row.height = Pt(max(10.0, min(float(height), 96.0)))
        table_row.height_rule = WD_ROW_HEIGHT_RULE.AT_LEAST

    label_values = _is_label_value_table(rows)
    for row_index, row in enumerate(rows):
        for column_index in range(columns):
            value = row[column_index] if column_index < len(row) else ""
            cell = table.cell(row_index, column_index)
            cell.width = Pt(widths[column_index])
            cell.vertical_alignment = (
                WD_CELL_VERTICAL_ALIGNMENT.CENTER
                if label_values
                else WD_CELL_VERTICAL_ALIGNMENT.TOP
            )
            lines = value.splitlines() or [""]
            for line_index, line in enumerate(lines):
                paragraph = (
                    cell.paragraphs[0]
                    if line_index == 0
                    else cell.add_paragraph()
                )
                paragraph.paragraph_format.space_before = Pt(0)
                paragraph.paragraph_format.space_after = Pt(0)

                label = (
                    LABEL_COLON_RE.match(line.strip())
                    if label_values and column_index == 0
                    else None
                )
                if label:
                    label_text = label.group("label").rstrip()
                    # Keep the colon in a fixed column just before the value
                    # cell instead of letting Word place it after the label.
                    # A label too long for that stop would wrap, so leave it
                    # inline with its colon.
                    if len(label_text) * 5.0 + 8 <= widths[column_index] - 12:
                        paragraph.paragraph_format.tab_stops.clear_all()
                        paragraph.paragraph_format.tab_stops.add_tab_stop(
                            Pt(max(12.0, widths[column_index] - 6.0)),
                            WD_TAB_ALIGNMENT.RIGHT,
                        )
                        label_text += "\t:"
                    else:
                        label_text += " :"
                    run = paragraph.add_run(label_text)
                    run.bold = True
                else:
                    run = paragraph.add_run(line)
                    if label_values and column_index == 0 and line.strip():
                        run.bold = True
                    elif row_index == 0 and value and not label_values:
                        run.bold = True
                run.font.size = Pt(9.5)
    document.add_paragraph()
    return table


def _image_rotation(block: dict) -> int:
    """Rotation encoded by the image transform in multiples of 90 degrees."""
    transform = block.get("transform")
    if not transform:
        return 0
    try:
        a, b = float(transform[0]), float(transform[1])
    except (TypeError, ValueError, IndexError):
        return 0
    if abs(a) < 1e-9 and abs(b) < 1e-9:
        return 0
    angle = math.degrees(math.atan2(b, a))
    nearest = round(angle / 90) * 90
    if abs(angle - nearest) > 1.0:
        return 0
    return int(nearest % 360)


def _png_stream(image: Image.Image) -> io.BytesIO:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    buffer.seek(0)
    return buffer


def _prepare_image(
    data: bytes,
    block: dict,
    page,
    rect: fitz.Rect,
    warnings: list[str],
    page_number: int,
) -> io.BytesIO | None:
    """Transcode any PDF image stream into a format Word really embeds.

    PDFs frequently store JPEG2000, JBIG2, CMYK JPEG or stencil masks. python-docx
    only accepts PNG/JPEG/GIF/BMP/TIFF, so those images are normalised here.
    """
    angle = _image_rotation(block)
    try:
        with Image.open(io.BytesIO(data)) as opened:
            opened.load()
            image = ImageOps.exif_transpose(opened)
            if angle:
                image = image.rotate(-angle, expand=True)
            if image.mode in ("RGBA", "LA") or (
                image.mode == "P" and "transparency" in image.info
            ):
                image = image.convert("RGBA")
            elif image.mode not in ("RGB", "L"):
                image = image.convert("RGB")
            return _png_stream(image)
    except Exception:
        pass

    if not angle:
        try:
            pixmap = fitz.Pixmap(data)
            if pixmap.colorspace is None or pixmap.colorspace.n not in (1, 3):
                pixmap = fitz.Pixmap(fitz.csRGB, pixmap)
            return io.BytesIO(pixmap.tobytes("png"))
        except Exception:
            pass

    # Last resort: render the region from the page so the visual is not lost.
    try:
        pixmap = page.get_pixmap(clip=rect, matrix=fitz.Matrix(2, 2), alpha=False)
        warnings.append(
            f"Gambar pada halaman {page_number} diekstrak sebagai render area halaman "
            "karena format aslinya tidak dapat dikonversi langsung."
        )
        return io.BytesIO(pixmap.tobytes("png"))
    except Exception:
        return None


def _add_image(
    document,
    block: dict,
    max_width_inches: float,
    max_height_inches: float,
    page,
    page_number: int,
    warnings: list[str],
) -> bool:
    data = block.get("image")
    rect = fitz.Rect(block["bbox"])
    if not data:
        warnings.append(
            f"Gambar pada halaman {page_number} tidak memiliki data yang dapat dibaca."
        )
        return False

    stream = _prepare_image(data, block, page, rect, warnings, page_number)
    if stream is None:
        warnings.append(f"Gambar pada halaman {page_number} tidak dapat disalin ke Word.")
        return False

    width_inches = max(0.1, rect.width / 72)
    height_inches = max(0.1, rect.height / 72)
    scale = min(
        1.0,
        max_width_inches / width_inches if width_inches else 1.0,
        max_height_inches / height_inches if height_inches else 1.0,
    )
    try:
        document.add_picture(
            stream,
            width=Inches(width_inches * scale),
            height=Inches(height_inches * scale),
        )
        return True
    except (KeyError, ValueError, OSError):
        warnings.append(f"Gambar pada halaman {page_number} ditolak oleh penulis DOCX.")
        return False


def _page_size(page) -> tuple[float, float]:
    """Page size in points, already accounting for the page rotation."""
    rect = page.rect
    return float(rect.width), float(rect.height)


def _detect_margins(info: PageInfo) -> tuple[float, float, float, float]:
    """Approximate the source margins from the content bounding box (points)."""
    width, height = info.width, info.height
    default = 0.8 * 72
    blocks = [block for block in info.blocks if block.get("bbox")]
    if not blocks:
        return default, default, default, default
    x0 = min(block["bbox"][0] for block in blocks)
    y0 = min(block["bbox"][1] for block in blocks)
    x1 = max(block["bbox"][2] for block in blocks)
    y1 = max(block["bbox"][3] for block in blocks)
    # Full-page backgrounds or a very small text area would produce nonsense.
    if (x1 - x0) < width * 0.4 or (y1 - y0) < height * 0.4:
        return default, default, default, default
    clamp = lambda value, limit: max(18.0, min(value, limit))  # noqa: E731
    left = clamp(x0, width * 0.25)
    right = clamp(width - x1, width * 0.25)
    top = clamp(y0, height * 0.25)
    bottom = clamp(height - y1, height * 0.25)
    if left + right > width * 0.8:
        left = right = default
    if top + bottom > height * 0.8:
        top = bottom = default
    return left, right, top, bottom


def _apply_page_geometry(
    section,
    width: float,
    height: float,
    margins: tuple[float, float, float, float],
) -> None:
    left, right, top, bottom = margins
    section.orientation = WD_ORIENT.LANDSCAPE if width > height else WD_ORIENT.PORTRAIT
    section.page_width = Inches(width / 72)
    section.page_height = Inches(height / 72)
    section.left_margin = Inches(left / 72)
    section.right_margin = Inches(right / 72)
    section.top_margin = Inches(top / 72)
    section.bottom_margin = Inches(bottom / 72)


def _paragraphs_from_ocr(text: str, style: str = "Normal") -> list[tuple[str, str]]:
    paragraphs: list[tuple[str, str]] = []
    for chunk in re.split(r"\n\s*\n", text):
        lines = [line.strip() for line in chunk.splitlines() if line.strip()]
        if not lines:
            continue
        current: list[str] = []
        for line in lines:
            if BULLET_RE.match(line) or NUMBER_RE.match(line):
                if current:
                    paragraphs.append((" ".join(current), style))
                    current = []
                paragraphs.append((line, "li"))
            else:
                current.append(line)
        if current:
            paragraphs.append((" ".join(current), style))
    return paragraphs


def _add_text_block(
    document,
    block: dict,
    body_size: float,
    headers: set[str],
    warnings: list[str],
) -> None:
    text, runs = _block_text(block)
    if not text or text in headers:
        return
    max_size = max((run[3] for run in runs), default=body_size)
    bold = bool(runs) and all(run[1] for run in runs if run[0].strip())
    kind, level = _classify_text_block_for_heading(
        text, max_size, body_size, bold, len(block.get("lines", []))
    )

    if kind == "li":
        match = BULLET_RE.match(text) or NUMBER_RE.match(text)
        content = match.group(3) if match else text
        style = "List Number" if NUMBER_RE.match(text) else "List Bullet"
        paragraph = document.add_paragraph(style=style)
        # Add content as plain text - runs include list marker which we don't want
        _add_runs(paragraph, [(content, False, False, body_size, False)])
        return

    if kind == "h" and level:
        paragraph = document.add_paragraph(style=HEADING_STYLE[level])
        _add_runs(paragraph, runs, bold_all=True)
        paragraph.paragraph_format.space_before = Pt(10 if level > 1 else 14)
        paragraph.paragraph_format.space_after = Pt(4)
        return

    paragraph = document.add_paragraph()
    _add_runs(paragraph, runs)
    paragraph.paragraph_format.space_after = Pt(6)
    if text.endswith((".", "!", "?", ":", ";")) and len(text) > 60:
        paragraph.alignment = WD_ALIGN_PARAGRAPH.LEFT
