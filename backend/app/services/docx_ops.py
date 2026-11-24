"""Word tools: DOCX -> PDF/TXT/HTML/Markdown, TXT/Markdown -> DOCX, merge, cleanup."""
from __future__ import annotations

from pathlib import Path
import re
from typing import Any

from docx import Document
from docx.oxml.ns import qn
from docx.shared import Pt
from docx.table import Table
from docx.text.paragraph import Paragraph

from app.core.errors import ConversionError
from app.services.base import ConversionContext, ConversionResult
from app.services.capabilities import libreoffice_available, word_available
from app.services.html_ops import render_html_to_pdf, wrap_html
from app.services.html_to_docx import html_to_document
from app.services.office_ops import convert_parallel_with_soffice, convert_with_soffice
from app.services.word_com import convert_parallel_with_word, convert_with_word, font_warnings

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PDF_MIME = "application/pdf"


def _open_docx(path: Path) -> Document:
    try:
        return Document(str(path))
    except Exception as exc:
        raise ConversionError("The Word file could not be opened. It may be damaged or not a real DOCX.") from exc


def iter_block_items(document: Document):
    body = document.element.body
    for child in body.iterchildren():
        if child.tag == qn("w:p"):
            yield Paragraph(child, document)
        elif child.tag == qn("w:tbl"):
            yield Table(child, document)


def _run_markdown(run) -> str:
    text = run.text
    if not text:
        return ""
    if run.bold and run.italic:
        return f"***{text}***"
    if run.bold:
        return f"**{text}**"
    if run.italic:
        return f"*{text}*"
    return text


def _table_markdown(table: Table) -> str:
    rows = []
    for row in table.rows:
        cells = [" ".join(cell.text.split()).replace("|", "\\|") for cell in row.cells]
        rows.append("| " + " | ".join(cells) + " |")
    if not rows:
        return ""
    columns = max(len(row.cells) for row in table.rows)
    separator = "| " + " | ".join(["---"] * columns) + " |"
    return "\n".join([rows[0], separator, *rows[1:]])


def _inches(value, fallback: float) -> float:
    try:
        inches = float(value.inches)
    except (AttributeError, TypeError, ValueError):
        return fallback
    return inches if inches > 0 else fallback


def _section_page_css(document: Document) -> tuple[str, list[str]]:
    """Read paper size, orientation and margins from the DOCX page setup."""
    warnings: list[str] = []
    sections = list(document.sections)
    first = sections[0]
    width = _inches(first.page_width, 8.27)
    height = _inches(first.page_height, 11.69)
    top = _inches(first.top_margin, 1.0)
    bottom = _inches(first.bottom_margin, 1.0)
    left = _inches(first.left_margin, 1.0)
    right = _inches(first.right_margin, 1.0)
    css = (
        "@page { size: "
        f"{width:.2f}in {height:.2f}in; "
        f"margin: {top:.2f}in {right:.2f}in {bottom:.2f}in {left:.2f}in; }}"
    )
    if len(sections) > 1:
        sizes = {
            (
                round(_inches(section.page_width, width), 2),
                round(_inches(section.page_height, height), 2),
            )
            for section in sections
        }
        warnings.append(
            f"Dokumen memiliki {len(sections)} bagian. Ukuran halaman bagian pertama "
            f"({width:.2f} x {height:.2f} inci) dipakai oleh renderer HTML."
            + (" Ukuran bagian lain berbeda dan tidak dapat divariasikan oleh renderer ini." if len(sizes) > 1 else "")
        )
    return css, warnings


ENGINES = ("auto", "libreoffice", "python", "word")

# A unique paragraph text that survives DOCX -> HTML conversion and is replaced
# by an explicit page break. This is how the Python renderer keeps the author's
# pagination instead of letting content reflow freely.
PAGE_BREAK_SENTINEL = "HXCONVERTPAGEBREAK"


def _engine(ctx: ConversionContext) -> str:
    """Which renderer the user asked for (unknown values fall back to auto)."""
    value = str(ctx.options.get("engine", "auto") or "auto").strip().lower()
    return value if value in ENGINES else "auto"


def _prepare_html_source(source: Path, destination: Path) -> tuple[Path, int]:
    """Copy the DOCX with a marker paragraph before every explicit page break.

    Mammoth has no notion of page breaks, so the breaks are marked in a copy of
    the document and turned into CSS page breaks afterwards. The original file
    is left untouched, and documents without explicit breaks are used as-is.
    """
    from docx.oxml import OxmlElement

    document = _open_docx(source)
    markings: list[tuple[Any, bool]] = []  # (paragraph, marker goes before?)
    for paragraph in document.paragraphs:
        try:
            if paragraph.paragraph_format.page_break_before:
                markings.append((paragraph, True))
                continue
        except Exception:  # pragma: no cover - malformed style
            pass
        try:
            has_break = bool(paragraph._p.xpath('.//w:br[@w:type="page"]'))
        except Exception:  # pragma: no cover - malformed XML
            continue
        # A page-break run is almost always the last thing in its paragraph (or
        # its own paragraph), so the break belongs *after* it. Mid-paragraph
        # breaks are approximated by breaking after the whole paragraph.
        if has_break:
            markings.append((paragraph, False))
    if not markings:
        return source, 0

    for paragraph, before in markings:
        try:
            if before:
                paragraph.insert_paragraph_before(PAGE_BREAK_SENTINEL)
            else:
                marker = OxmlElement("w:p")
                run = OxmlElement("w:r")
                text = OxmlElement("w:t")
                text.text = PAGE_BREAK_SENTINEL
                run.append(text)
                marker.append(run)
                paragraph._p.addnext(marker)
        except Exception:  # pragma: no cover - malformed document body
            continue
    destination.parent.mkdir(parents=True, exist_ok=True)
    document.save(destination)
    return destination, len(markings)


def _python_renderer_css(document: Document) -> str:
    """Consistent typography taken from the document's own Normal style."""
    family = "Helvetica, Arial, sans-serif"
    size = 10.5
    try:
        normal = document.styles["Normal"]
        name = normal.font.name
        if name:
            family = f"'{name}', Helvetica, Arial, sans-serif"
        if normal.font.size:
            size = round(float(normal.font.size.pt), 1)
    except Exception:  # pragma: no cover - unusual template
        pass
    return (
        f"body {{ font-family: {family}; font-size: {size}pt; }}"
        "table { border: 0.6pt solid #94a3b8; }"
        "th, td { border: 0.6pt solid #94a3b8; }"
    )


def _convert_with_python(
    ctx: ConversionContext,
    document: Document,
    stem: str,
    warnings: list[str],
) -> ConversionResult:
    """Deterministic DOCX -> PDF that needs neither Word nor LibreOffice.

    The same input produces the same PDF on every server: no COM automation, no
    installed Word version, no font cache of the host deciding the pagination.
    """
    import mammoth

    source, break_count = _prepare_html_source(
        ctx.input, ctx.workdir / "_docx_html_source.docx"
    )
    try:
        with source.open("rb") as handle:
            result = mammoth.convert_to_html(handle)
    except Exception as exc:
        raise ConversionError("The Word document could not be converted.") from exc

    body = result.value
    if break_count:
        body = body.replace(
            f"<p>{PAGE_BREAK_SENTINEL}</p>",
            '<div style="page-break-before: always;"></div>',
        )

    page_css, section_warnings = _section_page_css(document)
    warnings.extend(section_warnings)
    warnings.append(
        "Renderer Python dipakai: hasil konsisten di semua server tanpa otomasi Word "
        "atau LibreOffice. Ukuran halaman, margin, paragraf, tabel, gambar, dan pemisah "
        "halaman dipertahankan; header/footer, kolom majalah, dan objek mengambang kompleks "
        "dapat berbeda dari tampilan di Word."
    )
    html = wrap_html(
        body,
        stem,
        page_css=f"{page_css} {_python_renderer_css(document)}",
    )
    output = render_html_to_pdf(html, ctx.workdir / "document.pdf")
    if result.messages:
        warnings.append("Beberapa fitur Word disederhanakan oleh renderer Python.")
    return ConversionResult(output, f"{stem}.pdf", PDF_MIME, warnings)


ENGINE_CANDIDATES = {
    # Quality order: Word is the reference renderer, LibreOffice is the closest
    # portable one, and the Python renderer always works. Every path (single
    # and batch) walks this same order so a file does not change appearance
    # just because it was uploaded together with other files.
    "auto": ("word", "libreoffice", "python"),
    "word": ("word",),
    "libreoffice": ("libreoffice",),
    "python": ("python",),
}

ENGINE_LABEL = {"word": "Microsoft Word", "libreoffice": "LibreOffice"}


def _engine_candidates(ctx: ConversionContext) -> tuple[str, ...]:
    return ENGINE_CANDIDATES[_engine(ctx)]


def _missing_engine_error(engine: str) -> ConversionError:
    return ConversionError(
        f"Mesin {ENGINE_LABEL[engine]} dipilih, tetapi {ENGINE_LABEL[engine]} "
        "tidak tersedia di server ini."
    )


def _engine_failure(engine: str, exc: ConversionError) -> ConversionError:
    return ConversionError(
        f"{ENGINE_LABEL[engine]} gagal mengonversi dokumen ini. " + (exc.message or "")
    )


def docx_to_pdf(ctx: ConversionContext) -> ConversionResult:
    document = _open_docx(ctx.input)  # validates the file
    stem = Path(ctx.original_name).stem or "document"
    engine = _engine(ctx)
    warnings: list[str] = []

    for candidate in _engine_candidates(ctx):
        if candidate == "python":
            return _convert_with_python(ctx, document, stem, warnings)

        available = word_available() if candidate == "word" else libreoffice_available()
        if not available:
            if engine != "auto":
                raise _missing_engine_error(candidate)
            continue

        try:
            if candidate == "word":
                output = convert_with_word(ctx.input, ctx.workdir)
                warnings.extend(font_warnings(ctx.input))
            else:
                output = convert_with_soffice(ctx.input, "pdf", ctx.workdir)
                # LibreOffice can only use installed or embedded fonts; say so
                # when the document relies on a face this host does not have.
                warnings.extend(font_warnings(ctx.input, "LibreOffice"))
            return ConversionResult(output, f"{stem}.pdf", PDF_MIME, warnings)
        except ConversionError as exc:
            if engine != "auto":
                raise _engine_failure(candidate, exc) from exc
            warnings.append(
                f"{ENGINE_LABEL[candidate]} gagal mengonversi dokumen ini "
                f"({exc.message}). Renderer berikutnya dipakai; hasil dapat berbeda."
            )

    return _convert_with_python(ctx, document, stem, warnings)


def docx_to_pdf_batch(
    contexts: list[ConversionContext],
) -> list[ConversionResult | None]:
    """Batch fast path: one shared renderer session for many DOCX files.

    Uses the same engine order as the single-file path, so a document keeps the
    same appearance whether it is converted alone or in a batch. Files the fast
    path cannot render return ``None``; the per-file handler then applies the
    same rules (including a clear error for a forced engine that is missing).
    """
    if len(contexts) < 2:
        return [None] * len(contexts)

    candidates = _engine_candidates(contexts[0])
    if candidates == ("python",):
        return [None] * len(contexts)

    for ctx in contexts:
        ctx.workdir.mkdir(parents=True, exist_ok=True)
    jobs = [(ctx.input, ctx.workdir / "document.pdf") for ctx in contexts]
    outputs: list[Path | None] = [None] * len(contexts)
    used: list[str | None] = [None] * len(contexts)

    if "word" in candidates and word_available():
        try:
            outcomes = convert_parallel_with_word(jobs, contexts[0].workdir.parent)
        except ConversionError:
            outcomes = []
        for index, outcome in enumerate(outcomes):
            if outcome.output is not None:
                outputs[index] = outcome.output
                used[index] = "word"

    pending = [index for index, produced in enumerate(outputs) if produced is None]
    if "libreoffice" in candidates and pending and libreoffice_available():
        try:
            lo_outcomes = convert_parallel_with_soffice(
                [jobs[index] for index in pending], "pdf", contexts[0].workdir.parent
            )
        except ConversionError:
            lo_outcomes = []
        for pointer, outcome in zip(pending, lo_outcomes):
            if outcome.output is not None:
                outputs[pointer] = outcome.output
                used[pointer] = "libreoffice"

    results: list[ConversionResult | None] = []
    for index, ctx in enumerate(contexts):
        produced = outputs[index]
        if produced is None:
            results.append(None)
            continue
        stem = Path(ctx.original_name).stem or "document"
        warnings = (
            font_warnings(ctx.input, "LibreOffice")
            if used[index] == "libreoffice"
            else font_warnings(ctx.input)
        )
        results.append(
            ConversionResult(produced, f"{stem}.pdf", PDF_MIME, warnings)
        )
    return results


def docx_to_txt(ctx: ConversionContext) -> ConversionResult:
    document = _open_docx(ctx.input)
    parts: list[str] = []
    for block in iter_block_items(document):
        if isinstance(block, Paragraph):
            parts.append(block.text)
        else:
            for row in block.rows:
                parts.append("\t".join(cell.text.replace("\n", " ") for cell in row.cells))
    text = "\n".join(parts).strip()
    if not text:
        raise ConversionError("The Word document contains no extractable text.")
    output = ctx.workdir / "document.txt"
    output.write_text(text, encoding="utf-8")
    return ConversionResult(output, f"{Path(ctx.original_name).stem or 'document'}.txt", "text/plain; charset=utf-8")


def docx_to_html(ctx: ConversionContext) -> ConversionResult:
    import mammoth

    try:
        with ctx.input.open("rb") as handle:
            result = mammoth.convert_to_html(handle)
    except Exception as exc:
        raise ConversionError("The Word document could not be converted to HTML.") from exc
    html = wrap_html(result.value, Path(ctx.original_name).stem)
    output = ctx.workdir / "document.html"
    output.write_text(html, encoding="utf-8")
    warnings = ["Some Word-specific formatting may be simplified in HTML."] if result.messages else []
    return ConversionResult(output, f"{Path(ctx.original_name).stem or 'document'}.html", "text/html; charset=utf-8", warnings)


def docx_to_markdown(ctx: ConversionContext) -> ConversionResult:
    document = _open_docx(ctx.input)
    lines: list[str] = []

    for block in iter_block_items(document):
        if isinstance(block, Table):
            table = _table_markdown(block)
            if table:
                lines.extend(["", table, ""])
            continue

        text = "".join(_run_markdown(run) for run in block.runs).strip() or block.text.strip()
        if not text:
            continue
        style = (block.style.name or "").lower()
        heading = re.match(r"heading (\d)", style)
        if heading:
            level = min(6, int(heading.group(1)))
            lines.extend([f"{'#' * level} {block.text.strip()}", ""])
        elif style.startswith("list bullet"):
            lines.append(f"- {block.text.strip()}")
        elif style.startswith("list number"):
            lines.append(f"1. {block.text.strip()}")
        elif style.startswith("quote") or style.startswith("intense quote"):
            lines.extend([f"> {block.text.strip()}", ""])
        else:
            lines.extend([text, ""])

    markdown = "\n".join(lines).strip()
    if not markdown:
        raise ConversionError("The Word document contains no convertible content.")
    output = ctx.workdir / "document.md"
    output.write_text(markdown + "\n", encoding="utf-8")
    return ConversionResult(output, f"{Path(ctx.original_name).stem or 'document'}.md", "text/markdown; charset=utf-8")


def txt_to_docx(ctx: ConversionContext) -> ConversionResult:
    text = ctx.input.read_text(encoding="utf-8", errors="replace")
    if not text.strip():
        raise ConversionError("The text file is empty.")
    document = Document()
    document.styles["Normal"].font.name = "Calibri"
    document.styles["Normal"].font.size = Pt(11)
    for chunk in re.split(r"\n\s*\n", text):
        lines = chunk.splitlines()
        if not lines:
            continue
        paragraph = document.add_paragraph()
        for index, line in enumerate(lines):
            if index:
                paragraph.add_run().add_break()
            paragraph.add_run(line.strip())
    output = ctx.workdir / "document.docx"
    document.save(output)
    return ConversionResult(output, f"{Path(ctx.original_name).stem or 'document'}.docx", DOCX_MIME)


def markdown_to_docx(ctx: ConversionContext) -> ConversionResult:
    import markdown

    source = ctx.input.read_text(encoding="utf-8", errors="replace")
    if not source.strip():
        raise ConversionError("The Markdown file is empty.")
    html = markdown.markdown(source, extensions=["tables", "fenced_code", "sane_lists"])
    document = Document()
    document, warnings = html_to_document(html)
    output = ctx.workdir / "document.docx"
    document.save(output)
    return ConversionResult(output, f"{Path(ctx.original_name).stem or 'document'}.docx", DOCX_MIME, warnings)


def merge_docx(ctx: ConversionContext) -> ConversionResult:
    if len(ctx.inputs) < 2:
        raise ConversionError("Select at least two DOCX files to merge.")
    try:
        from docxcompose.composer import Composer

        master = _open_docx(ctx.inputs[0])
        composer = Composer(master)
        for path in ctx.inputs[1:]:
            composer.append(_open_docx(path))
        output = ctx.workdir / "merged.docx"
        composer.save(output)
        return ConversionResult(output, "merged.docx", DOCX_MIME)
    except ImportError:
        raise ConversionError("The docxcompose package is required to merge Word documents on this server.")


def clean_docx(ctx: ConversionContext) -> ConversionResult:
    document = _open_docx(ctx.input)
    font_name = str(ctx.options.get("font", "Calibri") or "Calibri")[:60]
    font_size = max(8, min(24, int(ctx.options.get("font_size", 11) or 11)))

    normal = document.styles["Normal"]
    normal.font.name = font_name
    normal.font.size = Pt(font_size)
    normal.paragraph_format.space_after = Pt(6)
    normal.paragraph_format.line_spacing = 1.15

    empty_streak = 0
    previous = None
    for paragraph in list(document.paragraphs):
        style_name = (paragraph.style.name or "").lower()
        is_heading = style_name.startswith("heading")
        if not paragraph.text.strip() and not paragraph._p.xpath(".//w:drawing"):
            empty_streak += 1
            if empty_streak >= 2:
                paragraph._element.getparent().remove(paragraph._element)
            continue
        empty_streak = 0
        if previous is not None:
            paragraph.paragraph_format.space_before = Pt(8 if is_heading else 0)
        if not is_heading and not style_name.startswith("list"):
            paragraph.paragraph_format.space_after = Pt(6)
        previous = paragraph

    output = ctx.workdir / "cleaned.docx"
    document.save(output)
    return ConversionResult(
        output,
        f"{Path(ctx.original_name).stem or 'document'}-clean.docx",
        DOCX_MIME,
        ["Formatting normalization adjusts paragraph spacing and body font; custom styles may change."],
    )
