"""PDF -> DOCX with content-aware conversion.

Conversion rules:
- Scanned/image-only pages -> full-page images in Word (no OCR)
- Digital text pages -> editable Word text
- Mixed pages -> layout preservation (pdf2docx)
- "Full Image" mode -> all pages as full-page images

Automatic OCR is DISABLED by default. OCR only runs when explicitly requested.

Performance notes:
- The document is analysed in ONE pass; per-page text blocks, image rects and
  vector-drawing statistics are cached and reused by every later stage.
- Full-image mode renders each page exactly once and embeds it immediately,
  avoiding intermediate PIL decode/re-encode/PNG-optimize roundtrips.
- pdfplumber (table detection) opens the file once per document, not once per page.
"""
from __future__ import annotations

import io
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import fitz
from docx import Document
from docx.enum.section import WD_ORIENT, WD_SECTION
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
from docx.shared import Inches, Pt
from PIL import Image, ImageOps

from app.core.errors import ConversionError
from app.services.base import ConversionContext, ConversionResult
from app.services.capabilities import ocr_available
from app.services.ocr import ocr_pdf_page
from app.services.pdf_ops import _open_pdf, MAX_PAGES

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

BULLET_RE = re.compile(r"^(\s*)([•·▪‣⁃\-\*]\s+)(.*)$")
NUMBER_RE = re.compile(r"^(\s*)(\d+[\.\)]\s+)(.*)$")

HEADING_STYLE = {1: "Heading 1", 2: "Heading 2", 3: "Heading 3"}


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
    cls: str = field(default="")


def _classify(
    page_area: float,
    meaningful_text: int,
    largest_image_area: float,
    images: list[tuple[int, Any]],
    significant_vector: bool,
    dominant_image: bool,
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
        images = page.get_images(full=True)
        for img in images:
            for image_rect in page.get_image_rects(img[0]):
                area = image_rect.get_area()
                if area > largest_image_area:
                    largest_image_area = area
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
        del drawings  # only the computed flag is needed later

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
                cls=_classify(
                    page_area,
                    len(text),
                    largest_image_area,
                    images,
                    significant_vector,
                    dominant_image,
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
    """
    mode = str(ctx.options.get("mode", "auto") or "auto").lower()
    ocr_mode = str(ctx.options.get("ocr", "never") or "never").lower()

    doc = _open_pdf(ctx.input)
    try:
        if mode == "full-image":
            pages_info = [
                (index + 1, float(doc[index].rect.width), float(doc[index].rect.height))
                for index in range(doc.page_count)
            ]
            total = doc.page_count
            result = _convert_image_only(ctx, doc, pages_info, total)
            return result

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

    - image-only pages -> full-page images
    - text-only pages -> editable text extraction
    - mixed/vector pages -> layout preservation via pdf2docx
    """
    # Mixed document with scanned pages: layout mode preserves everything visually.
    if "image-only" in page_classes and (
        "text-only" in page_classes or "mixed" in page_classes or "vector" in page_classes
    ):
        scanned_pages = [
            index + 1 for index, cls in enumerate(page_classes) if cls == "image-only"
        ]
        return _convert_layout_mode(ctx, scanned_pages, page_count, source_image_count)

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
    return _convert_layout_mode(ctx, scanned_pages, page_count, source_image_count)


def _convert_image_only(
    ctx: ConversionContext,
    doc: fitz.Document,
    pages_info: list[tuple[int, float, float]],
    total_pages: int,
) -> ConversionResult:
    """Full-image mode: render every PDF page as a full-page image in one Word document.

    This path is intentionally OCR-free and text-extraction-free. The PDF was
    opened once by the caller; pages are rendered one at a time, embedded into
    the document, and released from memory before the next page is rendered.
    """
    import zipfile

    output = ctx.workdir / "converted.docx"
    warnings: list[str] = []

    pages_to_render = min(total_pages, MAX_PAGES)
    if total_pages > MAX_PAGES:
        warnings.append(
            f"Hanya {MAX_PAGES} halaman pertama yang dikonversi (batas keamanan)."
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
            pixmap = page.get_pixmap(
                matrix=fitz.Matrix(300 / 72, 300 / 72), alpha=False
            )
            # Word accepts the PNG produced by PyMuPDF directly: skip the
            # previous PIL decode -> re-encode -> optimize roundtrip, which
            # tripled the CPU cost of every page.
            png_bytes = pixmap.tobytes("png")
            del pixmap
        except Exception as exc:
            warnings.append(f"Halaman {_number} gagal dirender: {exc}")
            continue
        try:
            # The section has zero margins, so the page box is exactly the
            # original page size. The image keeps the PDF aspect ratio.
            run.add_picture(
                io.BytesIO(png_bytes),
                width=Inches(w / 72),
                height=Inches(h / 72),
            )
            embedded += 1
        except Exception as exc:
            warnings.append(
                f"Gambar halaman {_number} tidak dapat disisipkan ke DOCX: {exc}"
            )
        finally:
            del png_bytes  # release the rendered page before the next one

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
            "secara visual; gunakan mode edit dengan OCR agar teksnya dapat diedit."
        )
    if source_image_count and not media:
        warnings.append(
            "PDF sumber berisi gambar, tetapi tidak ada gambar yang tersimpan di DOCX hasil "
            "mesin tata letak. Gunakan mode edit sebagai alternatif."
        )
    if len(scanned_pages) == page_count:
        warnings.append(
            "Seluruh dokumen tampak hasil pindai. Untuk Word yang dapat diedit sepenuhnya, "
            "pilih mode edit dan aktifkan OCR."
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
        "Mode edit: teks diekstrak menjadi paragraf Word Anda dapat mengubah langsung; "
        "posisi dan pemenggalan kolom asli PDF bisa berbeda dari tampilan aslinya."
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
        (0.8 * 72, 0.8 * 72, 0.8 * 72, 0.8 * 72),
    )
    current_size = (first.width, first.height)
    mixed_page_sizes = False
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
                    mixed_page_sizes = True
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

            tables: list[
                tuple[tuple[float, float, float, float], list[list[str]]]
            ] = []
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

            table_rects = [fitz.Rect(bbox) for bbox, _ in tables]
            emitted: set[int] = set()

            if not two_column:
                interleaved: list[tuple[float, str, object]] = []
                for block in ordered:
                    interleaved.append(
                        (block["bbox"][1], block.get("type", 0), block)
                    )
                for index, (bbox, rows) in enumerate(tables):
                    interleaved.append((bbox[1], 2, (index, rows)))
                interleaved.sort(key=lambda item: item[0])
                ordered = [item[2] for item in interleaved]

            for item in ordered:
                if isinstance(item, tuple):
                    index, rows = item
                    if index in emitted:
                        continue
                    emitted.add(index)
                    _add_table(document, rows)
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


class _TableSource:
    """pdfplumber opened once per document.

    The previous implementation opened the whole PDF for every page to look
    for tables, which multiplied I/O the longer the document was.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._pdf = None

    def collect(self, page_index: int):
        import pdfplumber

        if self._pdf is None:
            self._pdf = pdfplumber.open(str(self.path))
        if page_index >= len(self._pdf.pages):
            return []
        page = self._pdf.pages[page_index]
        tables: list[
            tuple[tuple[float, float, float, float], list[list[str]]]
        ] = []
        for table in page.find_tables():
            rows = table.extract()
            cleaned_rows = []
            for row in rows:
                values = ["" if cell is None else str(cell).strip() for cell in row]
                if any(values):
                    cleaned_rows.append(values)
            if len(cleaned_rows) >= 2 and max(len(row) for row in cleaned_rows) >= 2:
                tables.append((tuple(table.bbox), cleaned_rows))
        return tables

    def close(self) -> None:
        if self._pdf is not None:
            try:
                self._pdf.close()
            except Exception:  # pragma: no cover - defensive
                pass
            self._pdf = None


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


def _add_table(document, rows: list[list[str]]):
    columns = max(len(row) for row in rows)
    table = document.add_table(rows=len(rows), cols=columns)
    table.style = "Table Grid"
    table.autofit = True
    for row_index, row in enumerate(rows):
        for column_index in range(columns):
            value = row[column_index] if column_index < len(row) else ""
            cell = table.cell(row_index, column_index)
            cell.text = ""
            paragraph = cell.paragraphs[0]
            run = paragraph.add_run(value)
            run.font.size = Pt(9.5)
            if row_index == 0 and value:
                run.bold = True
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
