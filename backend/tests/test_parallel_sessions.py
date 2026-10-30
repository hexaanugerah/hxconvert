"""Sharding/merge behaviour of the parallel converter sessions."""
import time
from pathlib import Path

from app.services import office_ops, word_com
from app.services.office_ops import SofficeBatchOutcome
from app.services.word_com import WordBatchOutcome


def _mark_warm():
    word_com._last_word_activity = time.monotonic()
    office_ops._last_soffice_activity = time.monotonic()


def test_session_count_capped_by_files_and_cpus(monkeypatch):
    monkeypatch.setattr(office_ops.os, "cpu_count", lambda: 3)
    assert office_ops._session_count(1) == 1
    assert office_ops._session_count(2, max_sessions=1) == 1
    assert office_ops._session_count(15, max_sessions=2) == 2
    assert office_ops._session_count(15, max_sessions=8) == 3
    assert office_ops._session_count(4, max_sessions=6) == 3


def test_shard_indices_round_robin():
    assert office_ops._shard_indices(5, 2) == [[0, 2, 4], [1, 3]]
    assert office_ops._shard_indices(3, 4) == [[0], [1], [2]]


def test_convert_parallel_with_word_merges_shard_outcomes(tmp_path, monkeypatch):
    outputs: dict[int, Path] = {}
    _mark_warm()
    monkeypatch.setenv("DOCUCONVERT_WORD_POOL", "0")

    def fake_session(shard_jobs, workdir, position):
        assert len(shard_jobs) == len(
            office_ops._shard_indices(4, 2)[position]
        )
        return [
            WordBatchOutcome(output=Path(output_path), error=None)
            for _, output_path in shard_jobs
        ]

    monkeypatch.setattr(word_com, "word_available", lambda: True)
    monkeypatch.setattr(word_com, "_word_batch_session", fake_session)

    inputs = []
    for index in range(4):
        path = tmp_path / f"in{index}.docx"
        path.write_bytes(b"docx")
        inputs.append(path)
    outputs_dir = tmp_path / "out"
    outputs_dir.mkdir()
    jobs = [(path, outputs_dir / f"{index}.pdf") for index, path in enumerate(inputs)]

    merged = word_com.convert_parallel_with_word(jobs, tmp_path / "work", max_sessions=2)

    assert len(merged) == 4
    assert [outcome.output.name for outcome in merged] == ["0.pdf", "1.pdf", "2.pdf", "3.pdf"]


def test_convert_parallel_with_soffice_merges_shard_outcomes(tmp_path, monkeypatch):
    _mark_warm()
    calls: list[int] = []

    def fake_convert(shard_jobs, target, workdir, lock=None):
        calls.append(len(shard_jobs))
        return [SofficeBatchOutcome(output=Path(output_path)) for _, output_path in shard_jobs]

    monkeypatch.setattr(office_ops, "convert_batch_with_soffice", fake_convert)
    monkeypatch.setattr(
        office_ops, "_get_profile_locks", lambda: [__import__("threading").Lock() for _ in range(3)]
    )

    paths = []
    for index in range(5):
        path = tmp_path / f"in{index}.pptx"
        path.write_bytes(b"pptx")
        paths.append(path)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    jobs = [(path, out_dir / f"{index}.pdf") for index, path in enumerate(paths)]

    merged = office_ops.convert_parallel_with_soffice(
        jobs, "pdf", tmp_path / "work", max_sessions=3
    )

    assert calls == [2, 2, 1]
    assert [outcome.output.name for outcome in merged] == ["0.pdf", "1.pdf", "2.pdf", "3.pdf", "4.pdf"]
