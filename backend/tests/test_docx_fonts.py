"""Embedding the source PDF's fonts into the converted DOCX."""
from __future__ import annotations

import io
import zipfile
from pathlib import Path

import fitz
import pytest
from docx import Document
from docx.oxml.ns import qn
from fontTools.ttLib import TTFont
from lxml import etree

from app.services import docx_fonts


def _vera_font() -> Path | None:
    """A TrueType face that is not installed on Windows, for substitution tests."""
    try:
        import reportlab
    except ImportError:  # pragma: no cover - reportlab ships with xhtml2pdf
        return None
    path = Path(reportlab.__file__).parent / "fonts" / "Vera.ttf"
    return path if path.is_file() else None


def _pdf_with_font(font: Path) -> bytes:
    document = fitz.open()
    page = document.new_page()
    page.insert_font(fontname="F0", fontfile=str(font))
    page.insert_text((72, 96), "Hello embedded font", fontname="F0", fontsize=14)
    data = document.tobytes()
    document.close()
    return data


def _docx_using_font(name: str) -> bytes:
    document = Document()
    run = document.add_paragraph().add_run("Hello embedded font")
    run.font.name = name
    rpr = run._r.get_or_add_rPr()
    fonts = rpr.find(qn("w:rFonts"))
    for attribute in ("w:ascii", "w:hAnsi", "w:eastAsia", "w:cs"):
        fonts.set(qn(attribute), name)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def test_obfuscation_is_reversible_and_only_touches_the_header():
    payload = bytes(range(64))
    guid = "{0F1E2D3C-4B5A-6978-8796-A5B4C3D2E1F0}"

    obfuscated = docx_fonts._obfuscate(payload, guid)

    assert obfuscated[:32] != payload[:32]
    assert obfuscated[32:] == payload[32:]
    assert docx_fonts._obfuscate(obfuscated, guid) == payload


def test_embed_pdf_fonts_packages_the_original_face(tmp_path):
    vera = _vera_font()
    if vera is None:
        pytest.skip("No TrueType test font available.")

    pdf = tmp_path / "source.pdf"
    pdf.write_bytes(_pdf_with_font(vera))
    docx = tmp_path / "converted.docx"
    docx.write_bytes(_docx_using_font("Bitstream Vera Sans"))

    embedded = docx_fonts.embed_pdf_fonts(docx, pdf)

    assert embedded == ["Bitstream Vera Sans"]
    with zipfile.ZipFile(docx) as archive:
        names = archive.namelist()
        parts = [name for name in names if name.startswith("word/fonts/")]
        assert len(parts) == 1
        assert "word/_rels/fontTable.xml.rels" in names
        settings = archive.read("word/settings.xml").decode()
        assert "embedTrueTypeFonts" in settings
        content_types = archive.read("[Content_Types].xml").decode()
        assert "odttf" in content_types
        font_table = etree.fromstring(archive.read("word/fontTable.xml"))
        entry = next(
            node
            for node in font_table.findall(f"{{{docx_fonts.W_NS}}}font")
            if node.get(f"{{{docx_fonts.W_NS}}}name") == "Bitstream Vera Sans"
        )
        embed = entry.find(f"{{{docx_fonts.W_NS}}}embedRegular")
        assert embed is not None
        guid = embed.get(f"{{{docx_fonts.W_NS}}}fontKey")
        payload = docx_fonts._obfuscate(archive.read(parts[0]), guid)

    # The packaged stream must still be a readable face of the same family.
    font = TTFont(io.BytesIO(payload))
    assert font["name"].getDebugName(1) == "Bitstream Vera Sans"
    font.close()
    Document(str(docx))  # the rewritten package opens cleanly


def test_embed_pdf_fonts_skips_a_subset_that_cannot_render_the_text(tmp_path):
    """A PDF subset only holds the glyphs the source page used. Embedding it
    for text with new characters would show empty boxes."""
    vera = _vera_font()
    if vera is None:
        pytest.skip("No TrueType test font available.")

    pdf = tmp_path / "source.pdf"
    pdf.write_bytes(_pdf_with_font(vera))
    docx = tmp_path / "converted.docx"
    docx.write_bytes(_docx_using_font("Bitstream Vera Sans"))
    # Add a character the source PDF never rendered, in the same run font.
    document = Document(str(docx))
    document.paragraphs[0].runs[0].text += " \U0001f600"
    document.save(str(docx))
    original = docx.read_bytes()

    assert docx_fonts.embed_pdf_fonts(docx, pdf) == []
    assert docx.read_bytes() == original


def test_embed_pdf_fonts_leaves_an_unrelated_document_alone(tmp_path):
    vera = _vera_font()
    if vera is None:
        pytest.skip("No TrueType test font available.")

    pdf = tmp_path / "source.pdf"
    pdf.write_bytes(_pdf_with_font(vera))
    docx = tmp_path / "converted.docx"
    docx.write_bytes(_docx_using_font("Calibri"))
    original = docx.read_bytes()

    assert docx_fonts.embed_pdf_fonts(docx, pdf) == []
    assert docx.read_bytes() == original
