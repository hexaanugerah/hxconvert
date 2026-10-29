"""Regression tests for the PDF/image tools and batch behaviours added in V3."""
from __future__ import annotations

import io
import zipfile

import fitz
import pytest

from tests.conftest import make_pdf, make_png, upload


def _completed(client, response):
    assert response.status_code == 202, response.text
    detail = client.get(f"/api/v1/conversions/{response.json()['id']}")
    assert detail.status_code == 200, detail.text
    data = detail.json()
    assert data["status"] == "completed", data.get("error_message")
    return data


def _download(client, job):
    response = client.get(job["download_url"])
    assert response.status_code == 200, response.text
    return response.content


def test_extract_pages(client):
    job = _completed(
        client,
        upload(client, "extract-pages", [("doc.pdf", make_pdf(pages=3))], {"pages": "1,3"}),
    )
    document = fitz.open(stream=_download(client, job), filetype="pdf")
    assert document.page_count == 2
    document.close()


def test_crop_pages_shrinks_page(client):
    job = _completed(
        client,
        upload(
            client,
            "crop-pages",
            [("doc.pdf", make_pdf())],
            {"top": 10, "bottom": 10, "left": 5, "right": 5},
        ),
    )
    document = fitz.open(stream=_download(client, job), filetype="pdf")
    assert document[0].rect.width < 595
    assert document[0].rect.height < 842
    document.close()


def test_resize_pages_to_letter(client):
    job = _completed(
        client,
        upload(
            client,
            "resize-pages",
            [("doc.pdf", make_pdf())],
            {"page_size": "letter", "orientation": "portrait", "margin_mm": 5},
        ),
    )
    document = fitz.open(stream=_download(client, job), filetype="pdf")
    assert abs(document[0].rect.width - 612) < 1
    assert abs(document[0].rect.height - 792) < 1
    assert len(document[0].get_text().strip()) > 10
    document.close()


def test_protect_and_unlock_pdf(client):
    job = _completed(
        client,
        upload(
            client,
            "protect-pdf",
            [("doc.pdf", make_pdf())],
            {"password": "rahasia", "allow_printing": True, "allow_copying": False},
        ),
    )
    # The password must never be echoed back through the API.
    assert job["options"]["password"] == "********"

    protected = _download(client, job)
    document = fitz.open(stream=protected, filetype="pdf")
    assert document.needs_pass
    assert document.authenticate("rahasia")
    document.close()

    unlocked = _completed(
        client,
        upload(
            client,
            "unlock-pdf",
            [("protected.pdf", protected)],
            {"password": "rahasia"},
        ),
    )
    document = fitz.open(stream=_download(client, unlocked), filetype="pdf")
    assert not document.needs_pass
    document.close()

    # A wrong password must fail the job, not produce a result.
    response = upload(
        client,
        "unlock-pdf",
        [("protected.pdf", protected)],
        {"password": "salah"},
    )
    detail = client.get(f"/api/v1/conversions/{response.json()['id']}").json()
    assert detail["status"] == "failed"


def test_add_signature_places_image(client):
    png = make_png("SIGNATURE")
    job = _completed(
        client,
        upload(
            client,
            "add-signature",
            [("doc.pdf", make_pdf()), ("ttd.png", png)],
            {"position": "bottom-right", "width": 30},
        ),
    )
    document = fitz.open(stream=_download(client, job), filetype="pdf")
    assert len(document[-1].get_images(full=True)) >= 1
    document.close()


def test_compare_pdf_report(client):
    original = make_pdf()
    document = fitz.open(stream=original, filetype="pdf")
    document[0].insert_text((72, 300), "CHANGED LINE", fontsize=16, color=(1, 0, 0))
    changed = document.tobytes()
    document.close()

    job = _completed(
        client,
        upload(client, "compare-pdf", [("a.pdf", original), ("b.pdf", changed)], {}),
    )
    report = fitz.open(stream=_download(client, job), filetype="pdf")
    text = report[0].get_text()
    assert "Laporan Perbandingan PDF" in text
    assert report.page_count >= 2
    report.close()


def test_image_tools(client):
    png = make_png("COMPRESS ME")
    job = _completed(
        client,
        upload(client, "rotate-image", [("img.png", png)], {"angle": "90"}),
    )
    rotated = _download(client, job)
    original = fitz.Pixmap(io.BytesIO(png))
    result = fitz.Pixmap(io.BytesIO(rotated))
    assert result.width == original.height
    assert result.height == original.width

    job = _completed(
        client,
        upload(
            client,
            "resize-image",
            [("img.png", png)],
            {"mode": "percent", "percent": 50},
        ),
    )
    resized = fitz.Pixmap(io.BytesIO(_download(client, job)))
    assert abs(resized.width - original.width / 2) <= 1


def test_batch_duplicate_names_are_unique(client):
    response = client.post(
        "/api/v1/conversions",
        data={"tool": "txt-to-pdf", "options": "{}"},
        files=[
            ("files", ("same.txt", b"first document", "text/plain")),
            ("files", ("same.txt", b"second document", "text/plain")),
        ],
    )
    job = _completed(client, response)
    assert job["has_zip"]
    archive = zipfile.ZipFile(io.BytesIO(_download(client, job)))
    names = archive.namelist()
    assert len(set(names)) == len(names) == 2


def test_batch_partial_failure_keeps_zip_and_report(client):
    job = _completed(
        client,
        upload(
            client,
            "pdf-to-word",
            [
                ("good.pdf", make_pdf()),
                ("broken.pdf", b"%PDF-1.4 definitely broken"),
                ("good2.pdf", make_pdf(text="Second document")),
            ],
            {"mode": "editable", "ocr": "never"},
        ),
    )
    results = job["batch_results"]
    assert len(results) == 3
    assert sum(1 for item in results if not item["success"]) == 1
    assert all(item["download_url"] for item in results if item["success"])

    archive = zipfile.ZipFile(io.BytesIO(_download(client, job)))
    names = archive.namelist()
    assert "laporan-konversi.txt" in names
    assert len([name for name in names if name.endswith(".docx")]) == 2

    # Individual downloads really serve the stored file.
    first = next(item for item in results if item["success"])
    payload = client.get(first["download_url"])
    assert payload.status_code == 200
    assert payload.content.startswith(b"PK")


def test_batch_single_success_has_no_zip(client):
    job = _completed(
        client,
        upload(
            client,
            "pdf-to-word",
            [("good.pdf", make_pdf()), ("broken.pdf", b"%PDF-1.4 broken")],
            {"mode": "editable", "ocr": "never"},
        ),
    )
    assert not job["has_zip"]
    assert job["output_filename"].endswith(".docx")


def test_pdf_to_images_batch_uses_subfolders(client):
    job = _completed(
        client,
        upload(
            client,
            "pdf-to-images",
            [("one.pdf", make_pdf(pages=2)), ("two.pdf", make_pdf(text="Second", pages=2))],
            {"format": "png", "dpi": 72},
        ),
    )
    archive = zipfile.ZipFile(io.BytesIO(_download(client, job)))
    images = [name for name in archive.namelist() if name.endswith(".png")]
    assert len(images) >= 4
    assert all("/" in name for name in images)


def test_invalid_options_fail_the_job(client):
    response = upload(
        client,
        "extract-pages",
        [("doc.pdf", make_pdf())],
        {"pages": "abc"},
    )
    assert response.status_code == 202
    detail = client.get(f"/api/v1/conversions/{response.json()['id']}").json()
    assert detail["status"] == "failed"

    response = upload(
        client,
        "protect-pdf",
        [("doc.pdf", make_pdf())],
        {"password": ""},
    )
    assert response.status_code == 202
    detail = client.get(f"/api/v1/conversions/{response.json()['id']}").json()
    assert detail["status"] == "failed"
