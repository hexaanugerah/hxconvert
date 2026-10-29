"""Layout fidelity: PDF images into DOCX and DOCX layout into PDF."""
from __future__ import annotations

import io
import zipfile

import pytest

from tests.conftest import upload


def _job(client, response, expected: str = "completed") -> dict:
    assert response.status_code == 202, response.text
    job_id = response.json()["id"]
    detail = client.get(f"/api/v1/conversions/{job_id}")
    assert detail.status_code == 200, detail.text
    data = detail.json()
    assert data["status"] == expected, data.get("error_message") or data
    return data


def _download(client, job: dict) -> bytes:
    assert job["download_url"]
    response = client.get(job["download_url"])
    assert response.status_code == 200, response.text
    return response.content


def _image_bytes(
    size: tuple[int, int],
    color: tuple[int, int, int],
    *,
    fmt: str = "PNG",
    text: str | None = None,
    alpha: bool = False,
) -> bytes:
    from PIL import Image, ImageDraw

    mode = "RGBA" if alpha else "RGB"
    fill = color + ((255,) if alpha else ())
    image = Image.new(mode, size, fill)
    draw = ImageDraw.Draw(image)
    if text:
        draw.rectangle([4, 4, size[0] - 4, size[1] - 4], outline=(255, 255, 255), width=4)
        draw.text((12, 12), text, fill=(255, 255, 255))
    buffer = io.BytesIO()
    image.save(buffer, format=fmt)
    return buffer.getvalue()


def _rich_pdf() -> bytes:
    """PDF with PNG, alpha PNG, JPEG2000, repeated images, table image and vector art."""
    import fitz

    logo = _image_bytes((300, 120), (20, 80, 200), text="LOGO")
    photo = _image_bytes((500, 320), (200, 60, 20), text="PHOTO")
    transparent = _image_bytes((240, 240), (30, 180, 80), alpha=True)
    jp2 = _image_bytes((400, 300), (120, 20, 160), fmt="JPEG2000", text="JPX")

    document = fitz.open()
    page = document.new_page(width=595, height=842)
    page.insert_text((50, 55), "Layout fidelity document", fontsize=18)
    page.insert_image(fitz.Rect(50, 70, 200, 130), stream=logo)
    page.insert_image(fitz.Rect(50, 140, 300, 300), stream=photo)
    page.insert_image(fitz.Rect(320, 140, 440, 260), stream=transparent)
    page.insert_image(fitz.Rect(320, 275, 520, 425), stream=jp2)
    page.insert_image(fitz.Rect(50, 320, 200, 380), stream=logo)  # repeated
    # vector chart (no raster image)
    page.draw_rect(fitz.Rect(60, 440, 520, 620), color=(0.2, 0.2, 0.2), width=2)
    for index, height in enumerate((60, 120, 90, 150)):
        page.draw_rect(
            fitz.Rect(90 + index * 100, 600 - height, 150 + index * 100, 600),
            color=(0.1, 0.2, 0.6),
            fill=(0.45, 0.65, 0.95),
        )
    page.insert_text((60, 650), "Vector chart above must stay visible.", fontsize=11)

    second = document.new_page(width=595, height=842)
    second.insert_text((50, 55), "Page with a table image", fontsize=15)
    x0, y0, x1 = 50, 90, 540
    rows = [y0, y0 + 40, y0 + 160]
    for y in rows:
        second.draw_line(fitz.Point(x0, y), fitz.Point(x1, y), color=(0, 0, 0))
    for x in (x0, x0 + 120, x1):
        second.draw_line(fitz.Point(x, rows[0]), fitz.Point(x, rows[-1]), color=(0, 0, 0))
    second.insert_text((x0 + 6, y0 + 25), "Item", fontsize=11)
    second.insert_text((x0 + 126, y0 + 25), "Preview", fontsize=11)
    second.insert_text((x0 + 6, y0 + 65), "Logo in cell", fontsize=11)
    second.insert_image(fitz.Rect(x0 + 130, y0 + 45, x0 + 300, y0 + 140), stream=photo)

    data = document.tobytes()
    document.close()
    return data


@pytest.fixture()
def rich_pdf_bytes() -> bytes:
    return _rich_pdf()


def test_layout_mode_keeps_raster_jpx_and_vector_visuals(client, rich_pdf_bytes):
    job = _job(
        client,
        upload(
            client,
            "pdf-to-word",
            [("rich.pdf", rich_pdf_bytes)],
            options={"mode": "layout", "ocr": "never"},
        ),
    )
    content = _download(client, job)

    from docx import Document

    document = Document(io.BytesIO(content))
    text = "\n".join(paragraph.text for paragraph in document.paragraphs)
    assert "Layout fidelity document" in text
    assert "Page with a table image" in text

    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        media = [name for name in archive.namelist() if name.startswith("word/media/")]
    assert len(document.inline_shapes) >= 6
    assert len(media) >= 4
    assert abs(document.sections[0].page_width.inches - 595 / 72) < 0.06
    assert abs(document.sections[0].page_height.inches - 842 / 72) < 0.06

    # Round-trip through the reference Word renderer: images must actually be
    # visible in the produced PDF, not merely present in the DOCX package.
    from app.services.capabilities import libreoffice_available, word_available

    if not (word_available() or libreoffice_available()):
        pytest.skip("No reference DOCX renderer is installed for the visual round trip.")

    pdf_job = _job(client, upload(client, "docx-to-pdf", [("rich.docx", content)]))
    pdf_bytes = _download(client, pdf_job)
    import fitz

    pdf = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        assert pdf.page_count >= 2
        images = [info for page in pdf for info in page.get_image_info()]
        assert len(images) >= 4
        pdf_text = "".join(page.get_text() for page in pdf)
        assert "Layout fidelity document" in pdf_text
        assert "Vector chart" in pdf_text
    finally:
        pdf.close()


def test_editable_mode_transcodes_jpx_image(client):
    import fitz

    document = fitz.open()
    page = document.new_page(width=595, height=842)
    page.insert_text((50, 55), "JPEG2000 image document", fontsize=16)
    page.insert_image(
        fitz.Rect(50, 80, 350, 305),
        stream=_image_bytes((400, 300), (100, 30, 170), fmt="JPEG2000", text="JPX"),
    )
    pdf_bytes = document.tobytes()
    document.close()

    job = _job(
        client,
        upload(
            client,
            "pdf-to-word",
            [("jpx.pdf", pdf_bytes)],
            options={"mode": "editable", "ocr": "never"},
        ),
    )
    content = _download(client, job)

    from docx import Document

    output = Document(io.BytesIO(content))
    assert len(output.inline_shapes) == 1
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        media = [name for name in archive.namelist() if name.startswith("word/media/")]
    assert len(media) == 1
    assert media[0].endswith(".png")
    assert not any("tidak dapat disalin" in warning for warning in job["warnings"])


def test_layout_mode_preserves_mixed_page_sizes(client):
    import fitz

    sizes = [(595.0, 842.0), (792.0, 612.0), (500.0, 700.0)]
    document = fitz.open()
    for index, (width, height) in enumerate(sizes):
        page = document.new_page(width=width, height=height)
        page.insert_text((40, 100), f"Mixed page {index + 1}", fontsize=16)
    pdf_bytes = document.tobytes()
    document.close()

    job = _job(
        client,
        upload(
            client,
            "pdf-to-word",
            [("mixed.pdf", pdf_bytes)],
            options={"mode": "layout", "ocr": "never"},
        ),
    )
    from docx import Document

    output = Document(io.BytesIO(_download(client, job)))
    assert len(output.sections) == 3
    for section, (width, height) in zip(output.sections, sizes):
        assert abs(section.page_width.inches - width / 72) < 0.06
        assert abs(section.page_height.inches - height / 72) < 0.06
        expected = "LANDSCAPE" if width > height else "PORTRAIT"
        assert expected in str(section.orientation)


def _rich_docx() -> bytes:
    from docx import Document
    from docx.enum.text import WD_BREAK
    from docx.shared import Inches, Pt

    document = Document()
    section = document.sections[0]
    section.page_width = Inches(8.27)
    section.page_height = Inches(11.69)
    section.left_margin = section.right_margin = Inches(0.8)
    section.top_margin = section.bottom_margin = Inches(0.8)

    header = section.header.paragraphs[0]
    header.text = "DOCUCONVERT CONFIDENTIAL HEADER"
    footer = section.footer.paragraphs[0]
    footer.text = "DocuConvert footer - internal"

    title = document.add_heading("Layout fidelity source", level=1)
    title.runs[0].font.name = "Arial"
    paragraph = document.add_paragraph("Body paragraph in Arial. ")
    paragraph.add_run("Bold run").bold = True
    paragraph.add_run(" and normal text.")

    document.add_picture(
        io.BytesIO(_image_bytes((400, 200), (180, 40, 40), text="PICTURE")),
        width=Inches(2),
    )
    table = document.add_table(rows=3, cols=3)
    table.style = "Table Grid"
    for row_index in range(3):
        for column_index in range(3):
            table.cell(row_index, column_index).text = f"R{row_index + 1}C{column_index + 1}"
    document.add_paragraph("End of first page.")

    document.add_paragraph().add_run().add_break(WD_BREAK.PAGE)
    second = document.add_paragraph("Second page text stays on page two.")
    second.add_run().font.size = Pt(11)

    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def test_word_to_pdf_matches_word_layout(client):
    from app.services.capabilities import word_available

    if not word_available():
        pytest.skip("Microsoft Word is required for the reference-layout test.")

    source = _rich_docx()
    job = _job(client, upload(client, "docx-to-pdf", [("source.docx", source)]))
    pdf_bytes = _download(client, job)

    import fitz

    pdf = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        assert pdf.page_count == 2, [page.get_text()[:80] for page in pdf]
        first = pdf[0]
        assert abs(first.rect.width - 595.4) < 2
        assert abs(first.rect.height - 841.9) < 2

        first_text = first.get_text()
        assert "DOCUCONVERT CONFIDENTIAL HEADER" in first_text
        assert "DocuConvert footer - internal" in first_text
        assert "Layout fidelity source" in first_text
        assert "R2C2" in first_text
        assert "End of first page." in first_text
        assert "Second page text" not in first_text

        second_text = pdf[1].get_text()
        assert "Second page text" in second_text
        assert "DOCUCONVERT CONFIDENTIAL HEADER" in second_text
        assert "DocuConvert footer - internal" in second_text

        # The inline picture is 2 x 1 inch and starts at the left margin.
        image_rects = first.get_image_rects(first.get_images(full=True)[0][0])
        assert image_rects
        rect = image_rects[0]
        assert abs(rect.width - 144) < 8
        assert abs(rect.height - 72) < 8
        assert abs(rect.x0 - 57.6) < 8

        assert first.get_fonts(), "fonts must be embedded by the Word export"
    finally:
        pdf.close()
    assert not any("tidak dapat mengekspor" in warning for warning in job["warnings"])


def test_word_to_pdf_warns_about_missing_font(client):
    from app.services.capabilities import word_available

    if not word_available():
        pytest.skip("Microsoft Word is required for font substitution detection.")

    from docx import Document

    document = Document()
    document.add_paragraph("Text with a font that does not exist.")
    document.paragraphs[0].runs[0].font.name = "DocuConvert Missing Font 123"
    buffer = io.BytesIO()
    document.save(buffer)

    job = _job(client, upload(client, "docx-to-pdf", [("missing-font.docx", buffer.getvalue())]))
    assert _download(client, job).startswith(b"%PDF")
    assert any("DocuConvert Missing Font 123" in warning for warning in job["warnings"])
