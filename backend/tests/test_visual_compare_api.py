"""Tests for the original-vs-result visual comparison endpoints."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.core.database import SessionLocal
from app.models.conversion import ConversionJob
from tests.conftest import make_pdf, upload


def _set_status(job_id: str, **fields) -> None:
    with SessionLocal() as db:
        job = db.get(ConversionJob, job_id)
        for key, value in fields.items():
            setattr(job, key, value)
        db.commit()


def test_compare_pdf_to_word_job(client):
    created = upload(client, "pdf-to-word", [("compare.pdf", make_pdf())]).json()
    detail = client.get(f"/api/v1/conversions/{created['id']}").json()
    assert detail["status"] == "completed"
    assert detail["compare_url"] == f"/api/v1/conversions/{created['id']}/compare"

    response = client.get(detail["compare_url"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["available"] is True, body
    assert body["page_count"] >= 1
    assert len(body["pages"]) == body["page_count"]
    assert body["original_name"] == "compare.pdf"
    assert 0.0 <= body["average_ratio"] <= 1.0

    page = body["pages"][0]
    assert page["page"] == 1
    assert page["status"] in {"Sama", "Mirip", "Berbeda"}
    assert 0.0 <= page["ratio"] <= 1.0

    for kind in ("original", "result", "difference"):
        image = client.get(page[f"{kind}_url"])
        assert image.status_code == 200, image.text
        assert image.headers["content-type"].startswith("image/png")
        assert image.content.startswith(b"\x89PNG")

    # Cached: a second request returns the same summary without recomputing.
    again = client.get(detail["compare_url"])
    assert again.status_code == 200
    assert again.json()["pages"] == body["pages"]


def test_compare_unsupported_output_reports_reason(client):
    created = upload(client, "extract-text", [("notes.pdf", make_pdf())]).json()
    detail = client.get(f"/api/v1/conversions/{created['id']}").json()
    assert detail["compare_url"] is None

    response = client.get(f"/api/v1/conversions/{created['id']}/compare")
    assert response.status_code == 200
    body = response.json()
    assert body["available"] is False
    assert body["reason"]
    assert body["pages"] == []


def test_compare_unfinished_job(client):
    created = upload(client, "pdf-to-word", [("pending.pdf", make_pdf())]).json()
    _set_status(created["id"], status="processing", output_path=None)

    body = client.get(f"/api/v1/conversions/{created['id']}/compare").json()
    assert body["available"] is False
    assert "belum siap" in body["reason"]


def test_compare_expired_job(client):
    created = upload(client, "pdf-to-word", [("stale.pdf", make_pdf())]).json()
    _set_status(
        created["id"],
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )

    response = client.get(f"/api/v1/conversions/{created['id']}/compare")
    assert response.status_code == 410
    assert response.json()["error"]["code"] == "expired"


def test_compare_unknown_page_image(client):
    created = upload(client, "pdf-to-word", [("missing.pdf", make_pdf())]).json()
    client.get(f"/api/v1/conversions/{created['id']}/compare")
    assert (
        client.get(f"/api/v1/conversions/{created['id']}/compare/pages/99/original").status_code
        == 404
    )
    assert (
        client.get(f"/api/v1/conversions/{created['id']}/compare/pages/1/bogus").status_code
        == 404
    )
