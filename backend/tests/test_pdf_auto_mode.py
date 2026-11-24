"""Auto-mode routing: complex PDF forms must keep their original layout."""
from __future__ import annotations

import fitz

from app.services import pdf_to_docx
from app.services.base import ConversionContext, ConversionResult
from tests.conftest import make_pdf


def _form_pdf() -> bytes:
    """Landscape form with a wide, merged-cell table like a scanned receipt."""
    document = fitz.open()
    page = document.new_page(width=842, height=595)
    x0, y0 = 42, 40
    widths = [70, 60, 60, 90, 30, 70, 50, 100, 80, 70, 50, 40, 72]
    rows = 8
    row_h = 45

    x = x0
    for width in widths:
        page.draw_line(fitz.Point(x, y0), fitz.Point(x, y0 + rows * row_h))
        x += width
    for row in range(rows + 1):
        y = y0 + row * row_h
        page.draw_line(fitz.Point(x0, y), fitz.Point(x0 + sum(widths), y))

    for row in range(rows):
        for column in range(13):
            xx = x0 + sum(widths[:column]) + 3
            yy = y0 + row * row_h + 14
            page.insert_text((xx, yy), f"R{row + 1}C{column + 1}", fontsize=7)

    data = document.tobytes()
    document.close()
    return data


def _design_pdf() -> bytes:
    """Slide/screenshot page: labels drawn on top of large coloured panels."""
    document = fitz.open()
    for page_number in range(2):
        page = document.new_page(width=1200, height=675)
        page.draw_rect(
            fitz.Rect(0, 0, 1200, 675), fill=(1, 1, 1), color=None
        )
        panels = [
            (60, 90, 500, 420, (0.12, 0.35, 0.65)),
            (560, 90, 1140, 300, (0.95, 0.45, 0.2)),
            (560, 340, 1140, 600, (0.2, 0.6, 0.35)),
        ]
        for index, (x0, y0, x1, y1, color) in enumerate(panels):
            page.draw_rect(
                fitz.Rect(x0, y0, x1, y1), fill=color, color=None
            )
            page.insert_text(
                (x0 + 24, y0 + 60),
                f"Slide {page_number + 1} panel {index + 1}",
                fontsize=28,
                color=(1, 1, 1),
            )
            page.insert_text(
                (x0 + 24, y0 + 100),
                "Deskripsi singkat di atas bidang warna.",
                fontsize=16,
                color=(1, 1, 1),
            )
    data = document.tobytes()
    document.close()
    return data


def _context(tmp_path, pdf_bytes: bytes) -> ConversionContext:
    source = tmp_path / "input.pdf"
    source.write_bytes(pdf_bytes)
    workdir = tmp_path / "work"
    workdir.mkdir()
    return ConversionContext(
        inputs=[source],
        original_names=["input.pdf"],
        options={"mode": "auto"},
        workdir=workdir,
    )


def test_complex_form_is_detected_and_classified():
    document = fitz.open(stream=_form_pdf(), filetype="pdf")
    try:
        infos = pdf_to_docx._analyze_pages(document)
    finally:
        document.close()

    assert infos[0].complex_table is True
    assert infos[0].cls == "mixed"


def test_simple_text_page_is_not_treated_as_complex_table():
    document = fitz.open(stream=make_pdf(), filetype="pdf")
    try:
        infos = pdf_to_docx._analyze_pages(document)
    finally:
        document.close()

    assert infos[0].complex_table is False
    assert infos[0].design_page is False
    assert infos[0].cls == "text-only"


def test_design_page_is_detected():
    document = fitz.open(stream=_design_pdf(), filetype="pdf")
    try:
        infos = pdf_to_docx._analyze_pages(document)
    finally:
        document.close()

    assert all(info.design_page for info in infos)
    assert all(info.complex_table is False for info in infos)


def test_auto_mode_routes_design_pages_to_full_image(tmp_path, monkeypatch):
    calls: list[str] = []

    def fake_images(ctx, doc, pages_info, total_pages, *, dpi=300, jpeg_quality=None):
        calls.append("images")
        return ConversionResult(
            tmp_path / "images.docx", "images.docx", pdf_to_docx.DOCX_MIME, []
        )

    def fake_layout(ctx, scanned_pages, page_count, source_image_count):
        calls.append("layout")
        return ConversionResult(
            tmp_path / "layout.docx", "layout.docx", pdf_to_docx.DOCX_MIME, []
        )

    def fake_editable(ctx, doc, page_analysis, *, force_ocr):
        calls.append("editable")
        return ConversionResult(
            tmp_path / "editable.docx", "editable.docx", pdf_to_docx.DOCX_MIME, []
        )

    monkeypatch.setattr(pdf_to_docx, "_convert_image_only", fake_images)
    monkeypatch.setattr(pdf_to_docx, "_convert_layout_mode", fake_layout)
    monkeypatch.setattr(pdf_to_docx, "_convert_editable", fake_editable)

    result = pdf_to_docx.convert(_context(tmp_path, _design_pdf()))

    assert calls == ["images"]
    assert any("halaman desain/visual" in warning for warning in result.warnings)


def test_pdf_to_word_ui_is_automatic_only():
    from app.services.job_creation import validate_options
    from app.services.registry import get_tool, tools_public

    tool = get_tool("pdf-to-word")
    # Advanced options stay available to API callers ...
    assert {option.name for option in tool.options} >= {"mode", "ocr"}
    # ... but the web UI receives none, so there is no mode to choose.
    public = next(tool for tool in tools_public() if tool["id"] == "pdf-to-word")
    assert public["options"] == []

    assert validate_options(tool, {})["mode"] == "auto"
    assert validate_options(tool, {"mode": "layout"})["mode"] == "layout"


def test_auto_mode_routes_complex_form_to_layout(tmp_path, monkeypatch):
    calls: list[str] = []

    def fake_layout(ctx, scanned_pages, page_count, source_image_count):
        calls.append("layout")
        return ConversionResult(
            tmp_path / "layout.docx", "layout.docx", pdf_to_docx.DOCX_MIME, []
        )

    def fake_editable(ctx, doc, page_analysis, *, force_ocr):
        calls.append("editable")
        return ConversionResult(
            tmp_path / "editable.docx", "editable.docx", pdf_to_docx.DOCX_MIME, []
        )

    monkeypatch.setattr(pdf_to_docx, "_convert_layout_mode", fake_layout)
    monkeypatch.setattr(pdf_to_docx, "_convert_editable", fake_editable)

    result = pdf_to_docx.convert(_context(tmp_path, _form_pdf()))

    assert calls == ["layout"]
    assert any("form kompleks" in warning for warning in result.warnings)


def test_auto_mode_keeps_simple_text_editable(tmp_path, monkeypatch):
    calls: list[str] = []

    def fake_layout(ctx, scanned_pages, page_count, source_image_count):
        calls.append("layout")
        return ConversionResult(
            tmp_path / "layout.docx", "layout.docx", pdf_to_docx.DOCX_MIME, []
        )

    def fake_editable(ctx, doc, page_analysis, *, force_ocr):
        calls.append("editable")
        return ConversionResult(
            tmp_path / "editable.docx", "editable.docx", pdf_to_docx.DOCX_MIME, []
        )

    monkeypatch.setattr(pdf_to_docx, "_convert_layout_mode", fake_layout)
    monkeypatch.setattr(pdf_to_docx, "_convert_editable", fake_editable)

    result = pdf_to_docx.convert(_context(tmp_path, make_pdf()))

    assert calls == ["editable"]
    assert not result.warnings


def test_layout_mode_aligns_label_colons_and_maps_carlito():
    """Rows split by pdf2docx must share the tab stop of the other label rows."""
    from docx import Document
    from docx.shared import Emu

    document = Document()
    table = document.add_table(rows=1, cols=1)
    cell = table.cell(0, 0)
    paragraphs = [
        cell.paragraphs[0],
        cell.add_paragraph(),
        cell.add_paragraph(),
        cell.add_paragraph(),
    ]
    texts = [
        "No. Bukti \t: HO/B.27",
        "Tgl. Posting B:29/04/2026",
        "No. Doc. SAP : 103514135",
        "Referensi \t: DPAK/SPPb/120/IV/2026",
    ]
    for paragraph, text in zip(paragraphs, texts):
        run = paragraph.add_run(text)
        run.font.name = "Carlito"
    for paragraph in (paragraphs[0], paragraphs[3]):
        paragraph.paragraph_format.tab_stops.add_tab_stop(Emu(829310))

    assert pdf_to_docx._apply_layout_font_aliases(document) is True
    assert pdf_to_docx._align_colon_columns(document) is True

    expected = 829310 + int(pdf_to_docx.COLON_TAB_HEADROOM)
    for paragraph in paragraphs:
        assert "\t:" in paragraph.text
        stops = [int(stop.position) for stop in paragraph.paragraph_format.tab_stops]
        assert stops == [expected]
        assert all(run.font.name in (None, "Calibri") for run in paragraph.runs)
    assert paragraphs[1].text == "Tgl. Posting B\t: 29/04/2026"
    assert paragraphs[2].text == "No. Doc. SAP\t: 103514135"


def test_layout_colon_alignment_leaves_single_prose_colon_alone():
    from docx import Document

    document = Document()
    paragraph = document.add_paragraph("Catatan: teks ini bukan kolom label.")
    assert pdf_to_docx._align_colon_columns(document) is False
    assert "\t" not in paragraph.text


def test_layout_colon_alignment_uses_tab_stop_without_tab_and_empty_values():
    """Form variants store all six rows in one text line and may leave values blank."""
    from docx import Document
    from docx.shared import Emu

    document = Document()
    table = document.add_table(rows=1, cols=1)
    cell = table.cell(0, 0)
    texts = [
        "No. Bukti : HO/B.25",
        "Tgl. Posting B:",
        "No. Doc. SAP :",
        "Referensi : DPAK/SPPb/143/IV/2026",
    ]
    paragraphs = [cell.paragraphs[0]]
    paragraphs.extend(cell.add_paragraph() for _ in range(len(texts) - 1))
    for paragraph, text in zip(paragraphs, texts):
        paragraph.add_run(text)
    for paragraph in (paragraphs[0], paragraphs[3]):
        paragraph.paragraph_format.tab_stops.add_tab_stop(Emu(834390))

    assert pdf_to_docx._align_colon_columns(document) is True

    expected = 834390 + int(pdf_to_docx.COLON_TAB_HEADROOM)
    for paragraph in paragraphs:
        assert "\t:" in paragraph.text
        stops = [int(stop.position) for stop in paragraph.paragraph_format.tab_stops]
        assert stops == [expected]
    assert paragraphs[1].text == "Tgl. Posting B\t:"
    assert paragraphs[2].text == "No. Doc. SAP\t:"


def _label_value_form_pdf() -> bytes:
    """Two-column SAP-style form: labels on the left, values on the right."""
    document = fitz.open()
    page = document.new_page(width=595, height=842)
    rows = [
        ("No. Bukti", "HO/B."),
        ("Tgl. Posting B", ""),
        ("No. Doc. SAP", ""),
        ("Referensi", "AMCO/DOSG/SPPb/1113/XI/2026"),
        ("GL Account", "11201071"),
        ("Deskripsi", "BRI 063801002280300"),
    ]
    x0, x1, colon_x = 40, 555, 160
    top, row_h = 80, 38
    for index in range(len(rows) + 1):
        y = top + index * row_h
        page.draw_line(fitz.Point(x0, y), fitz.Point(x1, y), width=0.8)
    for x in (x0, colon_x, x1):
        page.draw_line(
            fitz.Point(x, top),
            fitz.Point(x, top + len(rows) * row_h),
            width=0.8,
        )
    for index, (label, value) in enumerate(rows):
        y = top + index * row_h + 24
        page.insert_text((x0 + 6, y), label + " :", fontsize=10, fontname="hebo")
        if value:
            page.insert_text((colon_x + 4, y), value, fontsize=10)
    data = document.tobytes()
    document.close()
    return data


def _divider_form_pdf() -> bytes:
    """SAP-style form box with one vertical divider and no row rules."""
    document = fitz.open()
    page = document.new_page(width=595, height=842)
    rows = [
        ("No. Bukti", ": HO/B."),
        ("Tgl. Posting B", ":"),
        ("No. Doc. SAP", ":"),
        ("Referensi", ": AMCO/DOSG/SPPb/1113/XI/2026"),
        ("GL Account", ": 11201071"),
        ("Deskripsi", ": BRI 063801002280300"),
    ]
    page.draw_rect(fitz.Rect(40, 80, 555, 330), width=0.8)
    page.draw_line(fitz.Point(160, 80), fitz.Point(160, 330), width=0.8)
    y = 110
    for label, value in rows:
        page.insert_text((45, y), label, fontsize=10)
        page.insert_text((162, y), value, fontsize=10)
        y += 38
    data = document.tobytes()
    document.close()
    return data


def test_merged_form_row_is_split_into_logical_rows(tmp_path):
    """pdfplumber sees this form as one tall row; positions must rebuild it."""
    source = tmp_path / "form.pdf"
    source.write_bytes(_divider_form_pdf())
    tables_source = pdf_to_docx._TableSource(source)
    try:
        detected = tables_source.collect(0)
    finally:
        tables_source.close()

    assert len(detected) == 1
    table = detected[0]
    assert len(table.rows) == 6
    assert table.rows[0] == ["No. Bukti", ": HO/B."]
    assert table.rows[1] == ["Tgl. Posting B", ":"]
    assert table.rows[3][1] == ": AMCO/DOSG/SPPb/1113/XI/2026"
    assert abs(table.column_widths[0] - 120) <= 1
    assert abs(table.column_widths[1] - 395) <= 1
    assert abs(table.row_heights[0] - 38) <= 1
    assert table.borders == ("top", "left", "bottom", "right", "insideV")


def test_narrow_label_value_form_is_not_a_complex_table():
    document = fitz.open(stream=_label_value_form_pdf(), filetype="pdf")
    try:
        infos = pdf_to_docx._analyze_pages(document)
    finally:
        document.close()

    assert infos[0].complex_table is False
    assert infos[0].cls == "text-only"


def test_auto_mode_rebuilds_label_value_form_with_source_widths(tmp_path):
    """A label/value form must keep its tight label column instead of one
    half-page column, so values stay next to the colon."""
    from docx import Document

    result = pdf_to_docx.convert(_context(tmp_path, _label_value_form_pdf()))
    output = Document(str(result.path))
    assert len(output.tables) == 1
    table = output.tables[0]
    assert abs(table.columns[0].width.pt - 120) <= 6
    assert abs(table.columns[1].width.pt - 395) <= 6

    label_paragraphs = [
        cell.paragraphs[0] for cell in table.columns[0].cells
    ]
    assert all("\t:" in paragraph.text for paragraph in label_paragraphs)
    assert len(
        {
            int(paragraph.paragraph_format.tab_stops[0].position)
            for paragraph in label_paragraphs
        }
    ) == 1


def test_auto_mode_keeps_form_box_and_divider_without_extra_row_lines(tmp_path):
    from docx import Document
    from docx.oxml.ns import qn

    result = pdf_to_docx.convert(_context(tmp_path, _divider_form_pdf()))
    output = Document(str(result.path))
    assert len(output.tables) == 1
    table = output.tables[0]
    assert len(table.rows) == 6
    assert abs(table.columns[0].width.pt - 120) <= 6
    assert abs(table.columns[1].width.pt - 395) <= 6
    rows = [[cell.text for cell in row.cells] for row in table.rows]
    assert rows[0] == ["No. Bukti", ": HO/B."]
    assert rows[1] == ["Tgl. Posting B", ":"]
    assert rows[3][1] == ": AMCO/DOSG/SPPb/1113/XI/2026"
    assert abs(table.rows[0].height.pt - 38) <= 1

    # Labels are bold, values are not.
    assert table.cell(0, 0).paragraphs[0].runs[0].bold is True
    assert not table.cell(0, 1).paragraphs[0].runs[0].bold

    # The source has an outer box and one divider, but no horizontal row
    # rules, so Word must not invent them.
    borders = table._tbl.tblPr.find(qn("w:tblBorders"))
    assert borders.find(qn("w:insideV")).get(qn("w:val")) == "single"
    assert borders.find(qn("w:insideH")).get(qn("w:val")) == "nil"


def test_layout_value_cells_are_moved_off_the_border():
    from docx import Document

    document = Document()
    table = document.add_table(rows=1, cols=2)
    value_cell = table.cell(0, 1)
    value_cell.text = ""
    first = value_cell.paragraphs[0]
    first.add_run(": HO/B.")
    value_cell.add_paragraph().add_run(": 11201071")

    assert pdf_to_docx._inset_border_colon_text(document) is True
    for paragraph in value_cell.paragraphs:
        assert int(paragraph.paragraph_format.left_indent) >= int(
            pdf_to_docx.BORDER_TEXT_INSET
        )
    # A normal label cell is untouched.
    assert table.cell(0, 0).paragraphs[0].paragraph_format.left_indent is None


def test_layout_amount_cells_shrink_instead_of_wrapping(tmp_path):
    """A long amount that only just fit the PDF column must stay on one line."""
    from docx import Document
    from docx.shared import Pt

    from app.services.docx_fonts import FontMeasurer
    from tests.test_docx_fonts import _pdf_with_font, _vera_font

    font = _vera_font()
    if font is None:
        pytest.skip("No TrueType test font available.")
    pdf = tmp_path / "source.pdf"
    pdf.write_bytes(_pdf_with_font(font))

    document = Document()
    table = document.add_table(rows=1, cols=2)
    table.autofit = False
    for index, width in enumerate((60, 68)):
        table.columns[index].width = Pt(width)
        table.cell(0, index).width = Pt(width)
    cell = table.cell(0, 1)
    run = cell.paragraphs[0].add_run("11,364,000,000")
    run.font.name = "Bitstream Vera Sans"
    run.font.size = Pt(10)

    measured = FontMeasurer(pdf).width("Bitstream Vera Sans", 10, "11,364,000,000")
    assert measured is not None and measured > 67

    assert pdf_to_docx._fit_table_numbers(document, pdf) is True
    size = cell.paragraphs[0].runs[0].font.size.pt
    assert size < 10
    fitted = FontMeasurer(pdf).width("Bitstream Vera Sans", size, "11,364,000,000")
    assert fitted is not None and fitted <= 68


def test_layout_large_amount_shortfall_widens_column(tmp_path):
    """A shortfall too big for a small font tweak borrows width from the neighbour."""
    from docx import Document
    from docx.shared import Pt

    from app.services.docx_fonts import FontMeasurer
    from tests.test_docx_fonts import _pdf_with_font, _vera_font

    font = _vera_font()
    if font is None:
        pytest.skip("No TrueType test font available.")
    pdf = tmp_path / "source.pdf"
    pdf.write_bytes(_pdf_with_font(font))

    document = Document()
    table = document.add_table(rows=1, cols=2)
    table.autofit = False
    for index, width in enumerate((120, 55)):
        table.columns[index].width = Pt(width)
        table.cell(0, index).width = Pt(width)
    cell = table.cell(0, 1)
    run = cell.paragraphs[0].add_run("11,364,000,000")
    run.font.name = "Bitstream Vera Sans"
    run.font.size = Pt(10)

    assert pdf_to_docx._fit_table_numbers(document, pdf) is True
    donor, amount = table.columns[0].width.pt, table.columns[1].width.pt
    assert amount > 55 and donor < 120
    assert round(donor + amount) == 175
    assert cell.width.pt == amount
    size = run.font.size.pt
    fitted = FontMeasurer(pdf).width("Bitstream Vera Sans", size, "11,364,000,000")
    assert fitted is not None and fitted <= amount - 0.4


def test_layout_merged_tables_are_left_alone(tmp_path):
    """Tables with merged cells keep their grid even when a value overflows."""
    from docx import Document
    from docx.shared import Pt

    from tests.test_docx_fonts import _pdf_with_font, _vera_font

    font = _vera_font()
    if font is None:
        pytest.skip("No TrueType test font available.")
    pdf = tmp_path / "source.pdf"
    pdf.write_bytes(_pdf_with_font(font))

    document = Document()
    table = document.add_table(rows=2, cols=2)
    table.autofit = False
    table.cell(0, 0).merge(table.cell(0, 1))
    for index, width in enumerate((70, 40)):
        table.columns[index].width = Pt(width)
    cell = table.cell(1, 1)
    cell.width = Pt(40)
    run = cell.paragraphs[0].add_run("11,364,000,000")
    run.font.name = "Bitstream Vera Sans"
    run.font.size = Pt(10)

    assert pdf_to_docx._table_grid_widths_pt(table) is None
    pdf_to_docx._fit_table_numbers(document, pdf)
    assert table.columns[0].width.pt == 70


def test_layout_numbers_that_fit_are_left_alone(tmp_path):
    from docx import Document
    from docx.shared import Pt

    from tests.test_docx_fonts import _pdf_with_font, _vera_font

    font = _vera_font()
    if font is None:
        pytest.skip("No TrueType test font available.")
    pdf = tmp_path / "source.pdf"
    pdf.write_bytes(_pdf_with_font(font))

    document = Document()
    table = document.add_table(rows=1, cols=1)
    table.autofit = False
    table.columns[0].width = Pt(200)
    table.cell(0, 0).width = Pt(200)
    run = table.cell(0, 0).paragraphs[0].add_run("11,364,000,000")
    run.font.name = "Bitstream Vera Sans"
    run.font.size = Pt(10)

    assert pdf_to_docx._fit_table_numbers(document, pdf) is False
    assert run.font.size.pt == 10


def test_layout_mode_relaxes_exact_row_heights():
    """Substituted fonts need rows that can grow instead of clipping text."""
    from docx import Document
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    document = Document()
    table = document.add_table(rows=2, cols=1)
    heights = []
    for row, rule in zip(table.rows, ("exact", "atLeast")):
        height = OxmlElement("w:trHeight")
        height.set(qn("w:val"), "400")
        height.set(qn("w:hRule"), rule)
        row._tr.get_or_add_trPr().append(height)
        heights.append(height)

    assert pdf_to_docx._relax_layout_row_heights(document) is True
    assert heights[0].get(qn("w:hRule")) == "atLeast"
    assert heights[1].get(qn("w:hRule")) == "atLeast"
    assert pdf_to_docx._relax_layout_row_heights(document) is False


def test_layout_colon_alignment_skips_tab_stop_outside_cell():
    """A tab stop at the cell's right edge would draw the colon on the border."""
    from docx import Document
    from docx.shared import Pt

    document = Document()
    table = document.add_table(rows=1, cols=1)
    table.columns[0].width = Pt(60)
    cell = table.cell(0, 0)
    cell.width = Pt(60)
    texts = ["A: 1", "B: 2"]
    paragraphs = [cell.paragraphs[0]]
    paragraphs.append(cell.add_paragraph())
    for paragraph, text in zip(paragraphs, texts):
        paragraph.add_run(text)
    paragraphs[0].paragraph_format.tab_stops.add_tab_stop(Pt(80))

    assert pdf_to_docx._align_colon_columns(document) is False
    assert all("\t" not in paragraph.text for paragraph in paragraphs)


