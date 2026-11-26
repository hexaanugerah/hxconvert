"""PDF editor: added text, images, shapes, and replacement of existing text."""
from __future__ import annotations

import json

from tests.conftest import make_pdf, make_png, upload


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


def test_edit_pdf_adds_text_image_shape_and_replaces_text(client):
    import fitz

    edits = {
        "items": [
            {
                "type": "text",
                "page": 1,
                "x": 72,
                "y": 300,
                "text": "Catatan editor",
                "size": 16,
                "color": "#111111",
                "font": "helv",
                "bold": True,
            },
            {
                "type": "rect",
                "page": 1,
                "x": 72,
                "y": 340,
                "w": 160,
                "h": 50,
                "stroke": "#e11d48",
                "fill": "#fde68a",
                "stroke_width": 2,
            },
            {
                "type": "ellipse",
                "page": 1,
                "x": 260,
                "y": 340,
                "w": 80,
                "h": 50,
                "stroke": "#2563eb",
                "fill": None,
                "stroke_width": 1,
            },
            {
                "type": "image",
                "page": 1,
                "x": 380,
                "y": 300,
                "w": 110,
                "h": 40,
                "asset": 0,
            },
        ],
        "replaces": [
            {
                "page": 1,
                "search": "Quarterly Report",
                "text": "Laporan Baru",
                "size": 20,
                "color": "#111111",
                "font": "helv",
            }
        ],
    }

    job = _job(
        client,
        upload(
            client,
            "edit-pdf",
            [("report.pdf", make_pdf("Quarterly Report")), ("logo.png", make_png())],
            options={"edits": json.dumps(edits)},
        ),
    )
    content = _download(client, job)
    assert content.startswith(b"%PDF")

    pdf = fitz.open(stream=content, filetype="pdf")
    try:
        assert pdf.page_count == 1
        page = pdf[0]
        text = page.get_text()
        assert "Catatan editor" in text
        assert "Laporan Baru" in text
        assert "Quarterly Report" not in text
        assert page.get_images(), "the uploaded image must be embedded in the page"
        assert len(text) > len("Catatan editor")
    finally:
        pdf.close()


def test_edit_pdf_replaces_clicked_text_rectangle(client):
    import fitz

    edits = {
        "items": [
            {
                "type": "text-edit",
                "page": 1,
                "x": 60,
                "y": 45,
                "w": 320,
                "h": 35,
                "text": "Laporan Baru",
                "size": 18,
                "color": "#111111",
                "font": "helv",
                "bold": True,
            }
        ]
    }
    job = _job(
        client,
        upload(
            client,
            "edit-pdf",
            [("report.pdf", make_pdf("Quarterly Report"))],
            options={"edits": json.dumps(edits)},
        ),
    )

    pdf = fitz.open(stream=_download(client, job), filetype="pdf")
    try:
        page = pdf[0]
        text = page.get_text()
        assert "Laporan Baru" in text
        assert "Quarterly Report" not in text
        # Text outside the clicked rectangle must stay untouched.
        assert "Page 1 body text" in text
    finally:
        pdf.close()


def test_edit_pdf_text_edit_detects_original_style(client):
    import fitz

    document = fitz.open()
    page = document.new_page(width=595, height=842)
    page.insert_text(
        (72, 80),
        "Original Style",
        fontsize=20,
        fontname="tibo",
        color=(0.8, 0.1, 0.1),
    )
    source = document.tobytes()
    document.close()

    edits = {
        "items": [
            {
                "type": "text-edit",
                "page": 1,
                "x": 65,
                "y": 55,
                "w": 220,
                "h": 40,
                "text": "Gaya Baru",
                "size": 12,
                "color": "#111111",
                "font": "helv",
                "bold": False,
                "auto_style": True,
                "baseline": 80,
            }
        ]
    }
    job = _job(
        client,
        upload(
            client,
            "edit-pdf",
            [("styled.pdf", source)],
            options={"edits": json.dumps(edits)},
        ),
    )

    pdf = fitz.open(stream=_download(client, job), filetype="pdf")
    try:
        spans = [
            span
            for block in pdf[0].get_text("dict")["blocks"]
            if block.get("type") == 0
            for line in block.get("lines", [])
            for span in line.get("spans", [])
        ]
        edited = next(span for span in spans if "Gaya Baru" in span.get("text", ""))
        color = int(edited["color"])
        red, green, blue = (color >> 16) & 255, (color >> 8) & 255, color & 255
        assert abs(red - 204) <= 6
        assert abs(green - 26) <= 6
        assert abs(blue - 26) <= 6
        assert "Times" in edited["font"] or (int(edited["flags"]) & 16)
    finally:
        pdf.close()


def test_edit_pdf_reuses_original_embedded_font(client):
    from pathlib import Path

    import fitz

    font_path = Path(__file__).resolve().parents[1] / "assets/fonts/SourceSansPro-Regular.ttf"
    document = fitz.open()
    page = document.new_page(width=595, height=842)
    page.insert_font(fontname="F0", fontfile=str(font_path))
    page.insert_text((72, 80), "Original Style", fontname="F0", fontsize=20)
    source = document.tobytes()
    document.close()

    edits = {
        "items": [
            {
                "type": "text-edit",
                "page": 1,
                "x": 65,
                "y": 55,
                "w": 220,
                "h": 40,
                "text": "Embedded New",
                "size": 20,
                "color": "#111111",
                "font": "helv",
                "auto_style": True,
                "baseline": 80,
            }
        ]
    }
    job = _job(
        client,
        upload(
            client,
            "edit-pdf",
            [("embedded.pdf", source)],
            options={"edits": json.dumps(edits)},
        ),
    )

    pdf = fitz.open(stream=_download(client, job), filetype="pdf")
    try:
        spans = [
            span
            for block in pdf[0].get_text("dict")["blocks"]
            if block.get("type") == 0
            for line in block.get("lines", [])
            for span in line.get("spans", [])
        ]
        edited = next(span for span in spans if "Embedded" in span.get("text", ""))
        assert "sourcesans" in edited["font"].lower().replace(" ", "")
        assert any(
            "Source Sans Pro" in str(entry[3])
            for entry in pdf[0].get_fonts(full=True)
        )
    finally:
        pdf.close()


def test_ocr_layout_returns_clickable_lines(client, monkeypatch):
    import app.api.v1.pdf as pdf_api

    monkeypatch.setattr(pdf_api, "ocr_available", lambda: True)
    monkeypatch.setattr(
        pdf_api,
        "ocr_pdf_page_lines",
        lambda page, language="eng", dpi=220: [
            {"text": "Baris pindai", "x0": 72.0, "y0": 300.0, "x1": 240.0, "y1": 320.0}
        ],
    )

    response = client.post(
        "/api/v1/pdf/ocr-layout",
        data={"page_number": "1", "language": "eng"},
        files=[("files", ("scan.pdf", make_pdf("scan"), "application/pdf"))],
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["page"] == 1
    assert len(body["runs"]) == 1
    run = body["runs"][0]
    assert run["text"] == "Baris pindai"
    assert run["w"] == 168.0
    assert run["h"] == 20.0


def test_edit_pdf_requires_an_element(client):
    job = _job(
        client,
        upload(
            client,
            "edit-pdf",
            [("report.pdf", make_pdf())],
            options={"edits": json.dumps({"items": [], "replaces": []})},
        ),
        expected="failed",
    )
    assert "minimal satu elemen" in (job["error_message"] or "")


def _scanned_pdf() -> bytes:
    """A page that is only an image, like a scan or photo."""
    import fitz

    document = fitz.open()
    page = document.new_page(width=595, height=842)
    page.insert_image(fitz.Rect(0, 0, 595, 842), stream=make_png())
    data = document.tobytes()
    document.close()
    return data


def test_edit_pdf_auto_ocr_enables_replacement(client, monkeypatch):
    import fitz

    import app.services.pdf_editor as editor

    monkeypatch.setattr(editor, "ocr_available", lambda: True)
    monkeypatch.setattr(
        editor,
        "ocr_pdf_page_lines",
        lambda page, language="eng", dpi=220: [
            {"text": "Quarterly Report", "x0": 72, "y0": 300, "x1": 260, "y1": 322}
        ],
    )

    edits = {
        "replaces": [
            {
                "page": 1,
                "search": "Quarterly Report",
                "text": "Laporan Baru",
                "size": 18,
                "color": "#111111",
                "font": "helv",
            }
        ]
    }
    job = _job(
        client,
        upload(
            client,
            "edit-pdf",
            [("scan.pdf", _scanned_pdf())],
            options={"edits": json.dumps(edits), "ocr_language": "eng+ind"},
        ),
    )
    assert any("OCR otomatis" in warning for warning in job["warnings"])

    content = _download(client, job)
    pdf = fitz.open(stream=content, filetype="pdf")
    try:
        text = pdf[0].get_text()
        assert "Laporan Baru" in text
        assert "Quarterly Report" not in text
    finally:
        pdf.close()


def test_edit_pdf_scanned_page_without_ocr_warns(client, monkeypatch):
    import app.services.pdf_editor as editor

    monkeypatch.setattr(editor, "ocr_available", lambda: False)

    edits = {
        "replaces": [
            {"page": 1, "search": "Quarterly Report", "text": "Laporan Baru"}
        ]
    }
    job = _job(
        client,
        upload(
            client,
            "edit-pdf",
            [("scan.pdf", _scanned_pdf())],
            options={"edits": json.dumps(edits)},
        ),
    )
    assert any("OCR otomatis tidak tersedia" in warning for warning in job["warnings"])
    assert job["download_url"]


def test_edit_pdf_reports_missing_search_text(client):
    edits = {
        "items": [
            {"type": "text", "page": 1, "x": 72, "y": 400, "text": "Tambahan", "size": 12}
        ],
        "replaces": [{"page": 1, "search": "Tidak Ada Di Sini", "text": "X"}],
    }
    job = _job(
        client,
        upload(
            client,
            "edit-pdf",
            [("report.pdf", make_pdf())],
            options={"edits": json.dumps(edits)},
        ),
    )
    assert any("tidak ditemukan" in warning for warning in job["warnings"])
    assert job["download_url"]
