"""Multi-page handling: page detection, previews, ordering and page geometry."""
from __future__ import annotations

import hashlib
import io
import os
import zipfile

import pytest

from tests.conftest import make_docx, make_pdf, unique_email, upload


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


def _make_sized_pdf(sizes: list[tuple[float, float]]) -> bytes:
    import fitz

    document = fitz.open()
    for index, (width, height) in enumerate(sizes):
        page = document.new_page(width=width, height=height)
        page.insert_text((40, 120), f"Page {index + 1} heading", fontsize=20, fontname="helv")
        page.insert_text(
            (40, 150),
            f"Body text on page {index + 1} used to verify page geometry.",
            fontsize=11,
        )
    data = document.tobytes()
    document.close()
    return data


def _make_image(size: tuple[int, int], color: tuple[int, int, int]) -> bytes:
    from PIL import Image

    image = Image.new("RGB", size, color)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _pdf_pages(client, job: dict):
    import fitz

    document = fitz.open(stream=_download(client, job), filetype="pdf")
    return document


def _center_color(document, page_index: int) -> tuple[int, int, int]:
    import fitz

    page = document[page_index]
    pixmap = page.get_pixmap(matrix=fitz.Matrix(0.2, 0.2), alpha=False)
    pixel = pixmap.pixel(pixmap.width // 2, pixmap.height // 2)
    return int(pixel[0]), int(pixel[1]), int(pixel[2])


def _closest_color(pixel: tuple[int, int, int], palette: list[tuple[int, int, int]]) -> int:
    return min(
        range(len(palette)),
        key=lambda index: sum((pixel[channel] - palette[index][channel]) ** 2 for channel in range(3)),
    )


def test_single_page_pdf_reports_one_real_preview(client):
    job = _job(
        client,
        upload(
            client,
            "pdf-to-word",
            [("one.pdf", make_pdf(pages=1))],
            options={"mode": "editable"},
        ),
    )

    assert job["page_count"] == 1
    assert [page["page_number"] for page in job["pages"]] == [1]
    page = job["pages"][0]
    assert page["status"] == "completed"
    assert page["preview_url"]
    assert abs(page["width_pt"] - 595) < 1
    assert abs(page["height_pt"] - 842) < 1
    assert page["orientation"] == "portrait"

    preview = client.get(page["preview_url"])
    assert preview.status_code == 200, preview.text
    assert preview.headers["content-type"] == "image/png"
    assert preview.content.startswith(b"\x89PNG\r\n\x1a\n")


def test_eight_page_pdf_renders_all_pages_in_order(client):
    job = _job(
        client,
        upload(
            client,
            "pdf-to-word",
            [("eight.pdf", make_pdf(pages=8))],
            options={"mode": "editable"},
        ),
    )

    assert job["page_count"] == 8
    assert [page["page_number"] for page in job["pages"]] == list(range(1, 9))
    assert all(page["status"] == "completed" for page in job["pages"])
    assert len({page["preview_url"] for page in job["pages"]}) == 8

    digests = set()
    for page in job["pages"]:
        response = client.get(page["preview_url"])
        assert response.status_code == 200, response.text
        assert response.content.startswith(b"\x89PNG\r\n\x1a\n")
        digests.add(hashlib.sha256(response.content).hexdigest())
    # Every page has a distinct number printed on it, so the renders differ.
    assert len(digests) == 8

    # The converted Word document must contain the content of every page too,
    # not just the previews.
    from docx import Document

    document = Document(io.BytesIO(_download(client, job)))
    text = "\n".join(paragraph.text for paragraph in document.paragraphs)
    for number in range(1, 9):
        assert f"Page {number} body text" in text


def test_more_than_eight_page_pdf_reports_every_page(client):
    job = _job(client, upload(client, "extract-text", [("twelve.pdf", make_pdf(pages=12))]))

    assert job["page_count"] == 12
    assert [page["page_number"] for page in job["pages"]] == list(range(1, 13))
    assert all(page["preview_url"] for page in job["pages"])
    for number in (1, 8, 12):
        response = client.get(job["pages"][number - 1]["preview_url"])
        assert response.status_code == 200, response.text


def test_eight_images_keep_order_names_and_aspect_ratio(client):
    palette = [
        (220, 30, 30),
        (30, 60, 220),
        (30, 170, 60),
        (225, 210, 30),
        (210, 40, 190),
        (40, 190, 200),
        (120, 20, 20),
        (20, 20, 20),
    ]
    dimensions = [(600, 900), (1200, 600), (600, 900), (1200, 600), (600, 900), (1200, 600), (600, 900), (1200, 600)]
    files = [
        ("scan.png", _make_image(dimensions[index], palette[index]))
        for index in range(8)
    ]

    job = _job(
        client,
        upload(client, "image-to-pdf", files, options={"page_size": "auto", "fit": "fit"}),
    )

    assert job["page_count"] == 8
    assert [page["page_number"] for page in job["pages"]] == list(range(1, 9))
    assert all(page["preview_url"] for page in job["pages"])
    assert len({page["preview_url"] for page in job["pages"]}) == 8

    document = _pdf_pages(client, job)
    try:
        assert document.page_count == 8
        for index, (width, height) in enumerate(dimensions):
            page = document[index]
            assert abs(page.rect.width / page.rect.height - width / height) < 0.02
            actual = _center_color(document, index)
            assert _closest_color(actual, palette) == index, (index, actual)
    finally:
        document.close()


@pytest.mark.parametrize(
    "page_size, expected",
    [
        ("a4", (842.0, 595.0)),
        ("letter", (792.0, 612.0)),
        ("legal", (1008.0, 612.0)),
        ("a3", (1190.6, 841.9)),
        ("f4", (936.0, 595.3)),
    ],
)
def test_image_standard_page_sizes_preserve_aspect(client, page_size, expected):
    image = _make_image((1600, 800), (10, 120, 200))
    job = _job(
        client,
        upload(
            client,
            "image-to-pdf",
            [("wide.png", image)],
            options={"page_size": page_size, "fit": "fit", "orientation": "auto"},
        ),
    )
    document = _pdf_pages(client, job)
    try:
        assert document.page_count == 1
        page = document[0]
        # The landscape source image selects a landscape page automatically.
        assert abs(page.rect.width - expected[0]) < 2
        assert abs(page.rect.height - expected[1]) < 2

        # The placed image keeps its 2:1 ratio (it is not stretched to the page).
        from PIL import Image

        pixmap = page.get_pixmap(matrix=__import__("fitz").Matrix(0.25, 0.25), alpha=False)
        rendered = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
        pixels = rendered.load()
        xs: list[int] = []
        ys: list[int] = []
        for y in range(rendered.height):
            for x in range(rendered.width):
                if pixels[x, y] != (255, 255, 255):
                    xs.append(x)
                    ys.append(y)
        assert xs and ys
        content_ratio = (max(xs) - min(xs) + 1) / (max(ys) - min(ys) + 1)
        assert abs(content_ratio - 2.0) < 0.06
    finally:
        document.close()


def test_mixed_page_sizes_pdf_to_word_keeps_sections(client):
    sizes = [(595.0, 842.0), (792.0, 612.0), (500.0, 700.0)]
    job = _job(
        client,
        upload(
            client,
            "pdf-to-word",
            [("mixed.pdf", _make_sized_pdf(sizes))],
            options={"mode": "editable"},
        ),
    )

    from docx import Document

    document = Document(io.BytesIO(_download(client, job)))
    assert len(document.sections) == 3, [
        (round(section.page_width.inches, 2), round(section.page_height.inches, 2))
        for section in document.sections
    ]
    for section, (width, height) in zip(document.sections, sizes):
        assert abs(section.page_width.inches - width / 72) < 0.06
        assert abs(section.page_height.inches - height / 72) < 0.06

    text = "\n".join(paragraph.text for paragraph in document.paragraphs)
    for number in (1, 2, 3):
        assert f"Page {number} heading" in text


def test_scanned_pdf_is_handled_with_ocr_or_a_clear_message(client):
    import fitz
    from PIL import Image, ImageDraw
    image = Image.new("RGB", (1000, 500), "white")
    draw = ImageDraw.Draw(image)
    draw.text((60, 200), "SCANNED PAGE 12345", fill="black")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")

    document = fitz.open()
    for _ in range(2):
        page = document.new_page(width=595, height=842)
        page.insert_image(fitz.Rect(40, 40, 555, 500), stream=buffer.getvalue())
    scanned = document.tobytes()
    document.close()

    from app.services.capabilities import ocr_available

    # Scanned pages without an explicit OCR request must not explode: they are
    # preserved visually (as images) and the result must report which pages
    # lack a text layer so the user knows to enable OCR for editable text.
    job = _job(
        client,
        upload(client, "pdf-to-word", [("scanned.pdf", scanned)], options={"ocr": "auto"}),
    )

    assert job["page_count"] == 2
    if not ocr_available():
        assert job["error_message"] is None

    from docx import Document

    content = _download(client, job)
    output = Document(io.BytesIO(content))
    # The scan visuals survive: either the scanned raster is embedded in the
    # DOCX package, or it is described in the warnings - it must not be lost.
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        has_media = any(name.startswith("word/media/") for name in archive.namelist())
    assert has_media
    # A clear message about the scanned pages and OCR guidance is provided.
    joined = " ".join(job["warnings"]).lower()
    assert any(key in joined for key in ("lapisan teks", "pindai", "ocr"))


def test_corrupt_pdf_fails_cleanly(client):
    job = _job(
        client,
        upload(client, "pdf-to-word", [("broken.pdf", b"%PDF-1.4\nthis is not a real document")]),
        expected="failed",
    )
    assert job["error_message"]
    assert job["download_url"] is None
    assert job["page_count"] in (None, 0)
    assert job["pages"] == []


def test_large_valid_file_completes(client):
    from PIL import Image

    noise = Image.frombytes("RGB", (1200, 1200), os.urandom(1200 * 1200 * 3))
    buffer = io.BytesIO()
    noise.save(buffer, format="PNG")
    image_bytes = buffer.getvalue()

    import fitz

    document = fitz.open()
    for index in range(3):
        page = document.new_page(width=595, height=842)
        page.insert_text((40, 60), f"Large document page {index + 1}", fontsize=14)
        page.insert_image(fitz.Rect(40, 90, 555, 600), stream=image_bytes)
    large_pdf = document.tobytes()
    document.close()
    assert len(large_pdf) > 3 * 1024 * 1024

    job = _job(client, upload(client, "extract-text", [("large.pdf", large_pdf)]))
    assert job["file_size"] == len(large_pdf)
    assert job["page_count"] == 3
    assert "Large document page 1" in _download(client, job).decode("utf-8")


def test_one_failed_page_render_is_reported_without_failing_job(client, monkeypatch):
    import app.services.document_inspection as inspection

    original = inspection._write_pdf_page_preview

    def flaky(page, preview_path):
        if page.number == 2:  # zero-based: the third page
            raise RuntimeError("simulated renderer failure")
        return original(page, preview_path)

    monkeypatch.setattr(inspection, "_write_pdf_page_preview", flaky)

    job = _job(client, upload(client, "extract-text", [("three.pdf", make_pdf(pages=3))]))
    assert job["page_count"] == 3
    statuses = [page["status"] for page in job["pages"]]
    assert statuses == ["completed", "completed", "failed"]
    assert job["pages"][2]["preview_url"] is None
    assert "3" in job["pages"][2]["error"]
    assert any("3" in warning for warning in job["warnings"])
    assert client.get(job["pages"][0]["preview_url"]).status_code == 200


def test_corrupt_image_among_valid_images_is_skipped(client):
    broken = b"\x89PNG\r\n\x1a\n" + b"not a real png body" * 20
    palette = [(200, 20, 20), (20, 20, 200)]
    response = upload(
        client,
        "image-to-pdf",
        [
            ("first.png", _make_image((400, 600), palette[0])),
            ("broken.png", broken),
            ("last.png", _make_image((400, 600), palette[1])),
        ],
        options={"page_size": "auto"},
    )
    job = _job(client, response)
    assert job["page_count"] == 2
    assert any("broken.png" in warning for warning in job["warnings"])

    document = _pdf_pages(client, job)
    try:
        assert document.page_count == 2
        assert _closest_color(_center_color(document, 0), palette) == 0
        assert _closest_color(_center_color(document, 1), palette) == 1
    finally:
        document.close()


def test_download_page_count_matches_source(client):
    job = _job(
        client,
        upload(client, "pdf-to-images", [("eight.pdf", make_pdf(pages=8))], options={"format": "png"}),
    )
    with zipfile.ZipFile(io.BytesIO(_download(client, job))) as archive:
        names = [name for name in archive.namelist() if name.endswith(".png")]
    assert names == [f"page-{number:03d}.png" for number in range(1, 9)]


def test_word_to_pdf_keeps_page_size_and_orientation(client):
    from docx import Document
    from docx.enum.section import WD_ORIENT
    from docx.shared import Inches

    document = Document()
    section = document.sections[0]
    section.orientation = WD_ORIENT.LANDSCAPE
    section.page_width = Inches(11.69)
    section.page_height = Inches(8.27)
    section.left_margin = section.right_margin = Inches(0.5)
    section.top_margin = section.bottom_margin = Inches(0.5)
    document.add_paragraph("Landscape Word document with a custom paper size.")
    buffer = io.BytesIO()
    document.save(buffer)

    job = _job(client, upload(client, "docx-to-pdf", [("landscape.docx", buffer.getvalue())]))
    pdf = _pdf_pages(client, job)
    try:
        assert pdf.page_count == 1, "no extra blank page should be added"
        page = pdf[0]
        assert abs(page.rect.width - 11.69 * 72) < 4
        assert abs(page.rect.height - 8.27 * 72) < 4
        assert "Landscape Word document" in page.get_text()
    finally:
        pdf.close()


def test_excel_to_pdf_keeps_landscape_and_all_columns(client):
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Wide"
    sheet.page_setup.orientation = "landscape"
    sheet.page_setup.paperSize = 9  # A4
    sheet.append([f"Column {number}" for number in range(1, 15)])
    sheet.append([f"value {number}" for number in range(1, 15)])
    buffer = io.BytesIO()
    workbook.save(buffer)

    job = _job(client, upload(client, "xlsx-to-pdf", [("wide.xlsx", buffer.getvalue())]))
    pdf = _pdf_pages(client, job)
    try:
        assert pdf.page_count >= 1
        assert pdf[0].rect.width > pdf[0].rect.height
        text = "".join(page.get_text() for page in pdf)
        assert "Column 1" in text
        assert "Column 14" in text, "columns must not be cut off on the right"
    finally:
        pdf.close()


def test_pdf_to_pptx_scales_each_mixed_page_without_overflow(client):
    from pptx import Presentation

    sizes = [(595.0, 842.0), (792.0, 612.0)]
    job = _job(client, upload(client, "pdf-to-pptx", [("mixed.pdf", _make_sized_pdf(sizes))]))
    presentation = Presentation(io.BytesIO(_download(client, job)))
    assert len(presentation.slides) == 2
    for slide in presentation.slides:
        for shape in slide.shapes:
            assert shape.left >= 0
            assert shape.top >= 0
            assert shape.left + shape.width <= presentation.slide_width + 10
            assert shape.top + shape.height <= presentation.slide_height + 10


def test_non_paginated_output_does_not_fabricate_pages(client):
    job = _job(client, upload(client, "docx-to-txt", [("sample.docx", make_docx())]))
    assert job["page_count"] is None
    assert job["pages"] == []


def test_preview_page_limit_marks_pages_pending_without_fake_urls(client, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "preview_max_pages", 2)
    job = _job(client, upload(client, "extract-text", [("three.pdf", make_pdf(pages=3))]))

    assert job["page_count"] == 3
    assert [page["status"] for page in job["pages"]] == [
        "completed",
        "completed",
        "pending",
    ]
    assert job["pages"][2]["preview_url"] is None
    assert client.get(f"/api/v1/conversions/{job['id']}/pages/3/preview").status_code == 404
    assert client.get(f"/api/v1/conversions/{job['id']}/pages/99/preview").status_code == 404


def test_preview_is_private_and_expires(client):
    from datetime import datetime, timedelta, timezone

    from fastapi.testclient import TestClient

    job = _job(client, upload(client, "pdf-to-word", [("private.pdf", make_pdf(pages=2))]))
    preview_url = job["pages"][0]["preview_url"]

    from app.main import app

    with TestClient(app) as other_client:
        assert other_client.get(preview_url).status_code == 404

    from app.core.database import SessionLocal
    from app.models.conversion import ConversionJob

    with SessionLocal() as db:
        record = db.get(ConversionJob, job["id"])
        record.expires_at = datetime.now(timezone.utc) - timedelta(minutes=5)
        db.commit()

    assert client.get(preview_url).status_code == 410
