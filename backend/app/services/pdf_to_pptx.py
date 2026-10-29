"""PDF -> PPTX. Each PDF page becomes a slide with editable text boxes."""
from __future__ import annotations

from pathlib import Path

import fitz
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.util import Emu, Pt

from app.core.errors import ConversionError
from app.services.base import ConversionContext, ConversionResult
from app.services.pdf_ops import _open_pdf

PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
MAX_PAGES = 100
SLIDE_WIDTH_EMU = int(13.333 * 914400)
EMU_PER_POINT = 12700


def convert(ctx: ConversionContext) -> ConversionResult:
    doc = _open_pdf(ctx.input)
    include_images = bool(ctx.options.get("include_images", False))
    warnings: list[str] = []

    if doc.page_count > MAX_PAGES:
        warnings.append(f"Only the first {MAX_PAGES} pages were converted.")

    first_page = doc[0]
    page_width = first_page.rect.width or 595
    page_height = first_page.rect.height or 842

    prs = Presentation()
    prs.slide_width = Emu(SLIDE_WIDTH_EMU)
    prs.slide_height = Emu(int(SLIDE_WIDTH_EMU * page_height / page_width))

    page_sizes = {
        (round(float(doc[index].rect.width), 1), round(float(doc[index].rect.height), 1))
        for index in range(min(doc.page_count, MAX_PAGES))
    }
    if len(page_sizes) > 1:
        warnings.append(
            "Halaman PDF memiliki ukuran berbeda. Setiap halaman diskalakan secara proporsional "
            "ke ukuran slide tanpa memotong konten."
        )

    try:
        for page_index in range(min(doc.page_count, MAX_PAGES)):
            page = doc[page_index]
            slide = prs.slides.add_slide(prs.slide_layouts[6])
            # Fit this page into the slide while keeping its aspect ratio.
            page_scale = min(
                prs.slide_width / max(float(page.rect.width), 1.0),
                prs.slide_height / max(float(page.rect.height), 1.0),
            )
            offset_x = (prs.slide_width - page.rect.width * page_scale) / 2
            offset_y = (prs.slide_height - page.rect.height * page_scale) / 2

            if include_images:
                pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
                image_path = ctx.workdir / f"_slide-{page_index + 1}.png"
                pixmap.save(str(image_path))
                image_scale = min(
                    prs.slide_width / max(pixmap.width, 1),
                    prs.slide_height / max(pixmap.height, 1),
                )
                image_width = int(pixmap.width * image_scale)
                image_height = int(pixmap.height * image_scale)
                slide.shapes.add_picture(
                    str(image_path),
                    int((prs.slide_width - image_width) / 2),
                    int((prs.slide_height - image_height) / 2),
                    width=image_width,
                    height=image_height,
                )

            blocks = [
                block
                for block in page.get_text("dict").get("blocks", [])
                if block.get("type") == 0
            ]
            if not blocks:
                warnings.append(f"Page {page_index + 1} has no extractable text layer.")
                continue

            for block in blocks:
                rect = fitz.Rect(block["bbox"])
                if rect.width < 2 or rect.height < 2:
                    continue
                left = Emu(int(offset_x + rect.x0 * page_scale))
                top = Emu(int(offset_y + rect.y0 * page_scale))
                width = Emu(int(max(rect.width, 20) * page_scale))
                height = Emu(int(max(rect.height, 14) * page_scale))
                box = slide.shapes.add_textbox(left, top, width, height)
                frame = box.text_frame
                frame.word_wrap = True
                frame.margin_left = 0
                frame.margin_right = 0
                frame.margin_top = 0
                frame.margin_bottom = 0

                paragraph = None
                for line in block.get("lines", []):
                    text = "".join(span.get("text", "") for span in line.get("spans", [])).strip()
                    if not text:
                        continue
                    paragraph = frame.add_paragraph() if paragraph is not None else frame.paragraphs[0]
                    run = paragraph.add_run()
                    run.text = text
                    size = max(span.get("size", 12) for span in line.get("spans", [{}]))
                    run.font.size = Pt(
                        max(5, min(96, round(float(size) * page_scale / EMU_PER_POINT, 1)))
                    )
                    bold = any(bool(span.get("flags", 0) & 16) for span in line.get("spans", []))
                    run.font.bold = bold
                    run.font.color.rgb = RGBColor(0x11, 0x18, 0x27)
    finally:
        doc.close()

    output = ctx.workdir / "presentation.pptx"
    prs.save(output)
    if include_images:
        warnings.append("Rendered page images are placed behind the text boxes for layout fidelity.")
    else:
        warnings.append("Text is editable; complex shapes, charts and background graphics are not reproduced.")
    return ConversionResult(output, "presentation.pptx", PPTX_MIME, warnings)
