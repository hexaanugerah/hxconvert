"""Persistent Word pool seams that do not need a real Word installation."""
from pathlib import Path

from app.services import word_com
from app.services.word_com import WordBatchOutcome


def test_convert_with_word_pool_disabled_returns_none(monkeypatch, tmp_path):
    monkeypatch.setenv("DOCUCONVERT_WORD_POOL", "0")
    assert word_com.convert_with_word_pool(
        [(Path("a.docx"), Path("a.pdf"))], tmp_path
    ) is None


def test_convert_with_word_pool_no_word_returns_none(monkeypatch, tmp_path):
    monkeypatch.setenv("DOCUCONVERT_WORD_POOL", "1")
    monkeypatch.setattr(word_com, "word_available", lambda: False)
    assert word_com.convert_with_word_pool(
        [(Path("a.docx"), Path("a.pdf"))], tmp_path
    ) is None


def test_convert_with_word_pool_empty_jobs(monkeypatch, tmp_path):
    monkeypatch.setenv("DOCUCONVERT_WORD_POOL", "1")
    assert word_com.convert_with_word_pool([], tmp_path) == []


def test_pool_entry_outcome_variants(tmp_path):
    ok_pdf = tmp_path / "ok.pdf"
    ok_pdf.write_bytes(b"%PDF-1.7\n" + b"x" * 200)
    bad_pdf = tmp_path / "bad.pdf"
    bad_pdf.write_bytes(b"not a pdf" * 50)

    good = word_com._pool_entry_outcome({"ok": True}, ok_pdf)
    assert good == WordBatchOutcome(output=ok_pdf, error=None)

    invalid = word_com._pool_entry_outcome({"ok": True}, bad_pdf)
    assert invalid.output is None
    assert "valid PDF" in (invalid.error or "")

    failed = word_com._pool_entry_outcome({"ok": False, "error": "boom"}, ok_pdf)
    assert failed.output is None
    assert failed.error == "boom"

    missing = word_com._pool_entry_outcome(None, ok_pdf)
    assert missing.output is None
    assert "did not report" in (missing.error or "")
