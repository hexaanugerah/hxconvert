"""Tests for daily quotas, job cancel/retry and the metrics endpoint."""
from __future__ import annotations

import json
import shutil

from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.database import SessionLocal
from app.models.conversion import ConversionJob
from app.core.storage import job_input_dir
from tests.conftest import make_pdf, unique_email, upload


def _register(client: TestClient) -> None:
    response = client.post(
        "/api/v1/auth/register",
        json={"name": "Quota User", "email": unique_email(), "password": "Password123"},
    )
    assert response.status_code == 201, response.text


def _set_status(job_id: str, **fields) -> None:
    with SessionLocal() as db:
        job = db.get(ConversionJob, job_id)
        for key, value in fields.items():
            setattr(job, key, value)
        db.commit()


def test_guest_daily_job_quota(client, monkeypatch):
    # Guest identity is shared by all anonymous tests, so allow one more job
    # than the usage already recorded in this run.
    baseline = client.get("/api/v1/conversions/stats").json()["used_jobs_24h"]
    monkeypatch.setattr(settings, "guest_daily_job_quota", baseline + 1)

    first = upload(client, "extract-text", [("quota-1.pdf", make_pdf())])
    assert first.status_code == 202, first.text

    blocked = upload(client, "extract-text", [("quota-2.pdf", make_pdf())])
    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "rate_limited"
    assert "Kuota harian" in blocked.json()["error"]["message"]
    assert "Masuk ke akun" in blocked.json()["error"]["message"]

    stats = client.get("/api/v1/conversions/stats").json()
    assert stats["usage"]["guest_daily_job_quota"] == baseline + 1
    assert stats["used_jobs_24h"] == baseline + 1


def test_registered_user_daily_job_quota(client, monkeypatch):
    _register(client)
    monkeypatch.setattr(settings, "daily_job_quota", 2)

    stats = client.get("/api/v1/conversions/stats").json()
    assert stats["used_jobs_24h"] == 0

    for index in range(2):
        response = upload(client, "extract-text", [(f"user-{index}.pdf", make_pdf())])
        assert response.status_code == 202, response.text

    blocked = upload(client, "extract-text", [("user-blocked.pdf", make_pdf())])
    assert blocked.status_code == 429
    assert "Masuk ke akun" not in blocked.json()["error"]["message"]


def test_daily_megabyte_quota(client, monkeypatch):
    monkeypatch.setattr(settings, "daily_megabyte_quota", 1)
    monkeypatch.setattr(settings, "guest_daily_job_quota", 100)

    blocked = upload(client, "txt-to-pdf", [("big.txt", b"a" * (1536 * 1024))])
    assert blocked.status_code == 429
    assert "Kuota ukuran harian" in blocked.json()["error"]["message"]


def test_cancel_queued_job_keeps_inputs_for_retry(client):
    # A failed job keeps its uploads; queue it again with valid options.
    created = upload(
        client,
        "rotate-pdf",
        [("cancel-me.pdf", make_pdf())],
        options={"pages": "99", "angle": "90"},
    ).json()
    _set_status(
        created["id"],
        status="queued",
        stage="queued",
        options_json=json.dumps({"pages": "1", "angle": "90"}),
    )

    cancelled = client.post(f"/api/v1/conversions/{created['id']}/cancel")
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"

    detail = client.get(f"/api/v1/conversions/{created['id']}").json()
    assert detail["status"] == "cancelled"
    assert detail["download_url"] is None

    # Repeated cancellation of a finished job is a validation error.
    again = client.post(f"/api/v1/conversions/{created['id']}/cancel")
    assert again.status_code == 422
    assert again.json()["error"]["code"] == "validation_error"

    # Inputs survived, so the job can be retried.
    retry = client.post(f"/api/v1/conversions/{created['id']}/retry")
    assert retry.status_code == 202, retry.text
    assert retry.json()["status"] == "queued"
    assert client.get(f"/api/v1/conversions/{created['id']}").json()["status"] == "completed"


def test_cancel_completed_job_is_rejected(client):
    created = upload(client, "extract-text", [("done.pdf", make_pdf())]).json()
    assert client.get(f"/api/v1/conversions/{created['id']}").json()["status"] == "completed"

    response = client.post(f"/api/v1/conversions/{created['id']}/cancel")
    assert response.status_code == 422


def test_cancel_processing_job_is_cooperative(client):
    created = upload(client, "extract-text", [("running.pdf", make_pdf())]).json()
    _set_status(
        created["id"],
        status="processing",
        stage="converting",
        completed_at=None,
        output_path=None,
        output_size=None,
    )

    response = client.post(f"/api/v1/conversions/{created['id']}/cancel")
    assert response.status_code == 200
    # Still processing: the worker discards the result once the engine finishes.
    assert response.json()["status"] == "processing"

    with SessionLocal() as db:
        job = db.get(ConversionJob, created["id"])
        assert job.cancel_requested is True


def test_retry_reruns_failed_job(client):
    created = upload(
        client,
        "rotate-pdf",
        [("retry-me.pdf", make_pdf())],
        options={"pages": "99", "angle": "90"},
    ).json()
    assert client.get(f"/api/v1/conversions/{created['id']}").json()["status"] == "failed"
    assert job_input_dir(created["id"]).is_dir()

    # Make the stored options valid and retry without uploading again.
    _set_status(created["id"], options_json=json.dumps({"pages": "1", "angle": "90"}))
    retry = client.post(f"/api/v1/conversions/{created['id']}/retry")
    assert retry.status_code == 202, retry.text

    detail = client.get(f"/api/v1/conversions/{created['id']}").json()
    assert detail["status"] == "completed", detail
    assert detail["error_message"] is None
    assert detail["download_url"]
    assert client.get(f"/api/v1/conversions/{created['id']}/download").status_code == 200


def test_retry_requires_retained_inputs(client):
    created = upload(client, "extract-text", [("gone.pdf", make_pdf())]).json()
    _set_status(created["id"], status="failed", error_message="boom")

    # Uploads are kept until expiry; simulate an expired/cleaned job.
    shutil.rmtree(job_input_dir(created["id"]), ignore_errors=True)
    response = client.post(f"/api/v1/conversions/{created['id']}/retry")
    assert response.status_code == 422
    assert "Unggah ulang" in response.json()["error"]["message"]


def test_retry_rejects_completed_job(client):
    created = upload(client, "extract-text", [("fine.pdf", make_pdf())]).json()
    response = client.post(f"/api/v1/conversions/{created['id']}/retry")
    assert response.status_code == 422


def test_metrics_endpoint_and_token(client, monkeypatch):
    upload(client, "extract-text", [("metrics.pdf", make_pdf())])

    response = client.get("/api/v1/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    body = response.text
    assert "hx_conversion_jobs{status=" in body
    assert "hx_conversion_jobs_total{tool=\"extract-text\"}" in body
    assert "hx_job_outcomes_total{outcome=\"completed\"}" in body
    assert "hx_stage_duration_seconds_total{stage=" in body
    assert "hx_database_up 1" in body

    monkeypatch.setattr(settings, "metrics_token", "scrape-secret")
    assert client.get("/api/v1/metrics").status_code == 403
    assert (
        client.get("/api/v1/metrics", headers={"X-Metrics-Token": "wrong"}).status_code
        == 403
    )
    allowed = client.get("/api/v1/metrics", headers={"X-Metrics-Token": "scrape-secret"})
    assert allowed.status_code == 200
    assert "hx_database_up 1" in allowed.text
