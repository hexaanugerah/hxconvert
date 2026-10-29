"""HTML / text / Markdown rendering to PDF using xhtml2pdf."""
from __future__ import annotations

from io import BytesIO
from pathlib import Path
import re

from app.core.errors import ConversionError, ValidationAppError
from app.services.base import ConversionContext, ConversionResult

PDF_MIME = "application/pdf"

BASE_CSS = """
@page { size: A4; margin: 18mm 16mm; }
body { font-family: Helvetica, Arial, sans-serif; font-size: 10.5pt; line-height: 1.45; color: #111827; }
h1, h2, h3, h4 { color: #0f172a; margin: 14pt 0 6pt; }
h1 { font-size: 20pt; } h2 { font-size: 16pt; } h3 { font-size: 13pt; } h4 { font-size: 11.5pt; }
p { margin: 0 0 7pt; }
ul, ol { margin: 0 0 8pt 16pt; }
li { margin-bottom: 3pt; }
table { border-collapse: collapse; width: 100%; margin: 8pt 0; }
th, td { border: 0.6pt solid #94a3b8; padding: 3pt 5pt; vertical-align: top; font-size: 9pt; text-align: left; }
th { background-color: #e2e8f0; }
img { max-width: 100%; }
pre, code { font-family: Courier, monospace; font-size: 9pt; }
pre { background-color: #f1f5f9; padding: 6pt; }
blockquote { border-left: 2pt solid #cbd5e1; margin-left: 0; padding-left: 8pt; color: #475569; }
.sheet-title { font-size: 13pt; font-weight: bold; margin: 0 0 6pt; color: #0f172a; }
"""


def wrap_html(body: str, title: str = "Document", page_css: str | None = None) -> str:
    """Wrap body HTML in a printable document.

    ``page_css`` lets callers carry the source document's paper size, orientation
    and margins into the PDF instead of always printing A4 portrait.
    """
    extra = f"<style>{page_css}</style>" if page_css else ""
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'/>"
        f"<title>{_escape(title)}</title><style>{BASE_CSS}</style>{extra}</head><body>{body}</body></html>"
    )


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def render_html_to_pdf(html: str, destination: Path, base_dir: Path | None = None) -> Path:
    try:
        from xhtml2pdf import pisa
    except ImportError as exc:  # pragma: no cover
        raise ConversionError("The PDF renderer (xhtml2pdf) is not installed on the server.") from exc

    result = BytesIO()
    try:
        status = pisa.CreatePDF(
            src=html,
            dest=result,
            encoding="utf-8",
            path=str(base_dir) if base_dir else None,
        )
    except Exception as exc:
        raise ConversionError(f"The HTML could not be rendered to PDF: {exc}") from exc
    if status.err:
        raise ConversionError("The HTML could not be rendered to PDF because it contains invalid markup.")
    destination.write_bytes(result.getvalue())
    if destination.stat().st_size < 100:
        raise ConversionError("The rendered PDF is empty.")
    return destination


def html_file_to_pdf(ctx: ConversionContext) -> ConversionResult:
    try:
        html = ctx.input.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ConversionError("The HTML file could not be read.") from exc
    if not html.strip():
        raise ValidationAppError("The HTML file is empty.")
    output = render_html_to_pdf(html, ctx.workdir / "document.pdf", ctx.input.parent)
    return ConversionResult(output, f"{Path(ctx.original_name).stem or 'document'}.pdf", PDF_MIME)


def text_to_pdf(ctx: ConversionContext) -> ConversionResult:
    text = ctx.input.read_text(encoding="utf-8", errors="replace")
    if not text.strip():
        raise ValidationAppError("The text file is empty.")
    paragraphs = []
    for chunk in re.split(r"\n\s*\n", text):
        lines = chunk.splitlines()
        if not lines:
            continue
        escaped = "<br/>".join(_escape(line) for line in lines)
        paragraphs.append(f"<p>{escaped}</p>")
    output = render_html_to_pdf(
        wrap_html("".join(paragraphs), Path(ctx.original_name).stem), ctx.workdir / "document.pdf"
    )
    return ConversionResult(output, f"{Path(ctx.original_name).stem or 'document'}.pdf", PDF_MIME)


def markdown_to_pdf(ctx: ConversionContext) -> ConversionResult:
    import markdown

    source = ctx.input.read_text(encoding="utf-8", errors="replace")
    if not source.strip():
        raise ValidationAppError("The Markdown file is empty.")
    try:
        body = markdown.markdown(source, extensions=["tables", "fenced_code", "sane_lists"])
    except Exception as exc:
        raise ConversionError("The Markdown could not be parsed.") from exc
    output = render_html_to_pdf(
        wrap_html(body, Path(ctx.original_name).stem), ctx.workdir / "document.pdf"
    )
    return ConversionResult(output, f"{Path(ctx.original_name).stem or 'document'}.pdf", PDF_MIME)
