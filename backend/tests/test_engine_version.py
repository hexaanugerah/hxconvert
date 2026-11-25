"""Guard: conversion output may only change together with ``ENGINE_VERSION``.

The result cache keys on ``ENGINE_VERSION``; if the engine changes but the
constant does not, users keep getting stale cached conversions after a deploy.
This test fingerprints the actual DOCX content produced by the fixture PDF in
both modes, so a behaviour change without a version bump fails loudly.
"""
from __future__ import annotations

import hashlib
import io
import zipfile

import pytest

from tests.conftest import upload

# Update both constants in the same commit that bumps ENGINE_VERSION in
# app/services/result_cache.py.
RECORDED_ENGINE_VERSION = "2026-11-25.2"
RECORDED_FINGERPRINTS = {
    "editable": "d5b88de568ff32b0ee8d4a98b267e0d82f46d75dfc110a48bef11677d7c5a92e",
    "layout": "84ddcb7c267ce7357b6a5f3a065282cd625d14abcd0189447965b1c6bdd40e96",
}


def _document_xml(client, pdf: bytes, mode: str) -> bytes:
    response = upload(
        client,
        "pdf-to-word",
        [("fingerprint.pdf", pdf)],
        options={"mode": mode, "ocr": "never"},
    )
    assert response.status_code == 202, response.text
    job = client.get(f"/api/v1/conversions/{response.json()['id']}").json()
    assert job["status"] == "completed", job.get("error_message")
    download = client.get(job["download_url"])
    assert download.status_code == 200, download.text
    with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
        return archive.read("word/document.xml")


@pytest.mark.parametrize("mode", sorted(RECORDED_FINGERPRINTS))
def test_conversion_output_matches_recorded_engine_version(client, pdf_bytes, mode):
    from app.services.result_cache import ENGINE_VERSION

    digest = hashlib.sha256(_document_xml(client, pdf_bytes, mode)).hexdigest()
    assert (ENGINE_VERSION, digest) == (
        RECORDED_ENGINE_VERSION,
        RECORDED_FINGERPRINTS[mode],
    ), (
        f"Output konversi '{mode}' berbeda dari yang tercatat. Jika perilaku "
        "konversi memang berubah, naikkan ENGINE_VERSION di "
        "app/services/result_cache.py lalu perbarui RECORDED_ENGINE_VERSION dan "
        "RECORDED_FINGERPRINTS di test ini agar hasil cache lama tidak dipakai."
    )
