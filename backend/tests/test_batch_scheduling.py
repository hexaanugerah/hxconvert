from pathlib import Path

from app.services import batch_conversion
from app.services.base import ConversionContext, ConversionResult
from app.services.batch_conversion import BatchFileResult, process_batch_conversion
from app.services.registry import ToolSpec


def _fake_tool(batch_handler):
    def handler(ctx: ConversionContext) -> ConversionResult:  # pragma: no cover
        raise AssertionError("per-file handler must not run for batch-handled files")

    return ToolSpec(
        id="fake",
        label="Fake",
        description="",
        category="text",
        inputs=(".docx",),
        output="pdf",
        handler=handler,
        batch_handler=batch_handler,
    )


def _write_pdf(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"%PDF-1.4\n%%EOF\n")
    return path


def _inputs(tmp_path: Path, count: int) -> tuple[list[Path], list[str]]:
    paths = []
    for index in range(count):
        path = tmp_path / "inputs" / f"in{index}.docx"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"docx")
        paths.append(path)
    return paths, [path.name for path in paths]


def _size_tool(payload: bytes) -> ToolSpec:
    """Tool that writes a fixed-size PDF so ZIP sizing can be exercised."""

    def handler(ctx: ConversionContext) -> ConversionResult:
        output = ctx.workdir / "out.pdf"
        output.write_bytes(payload)
        return ConversionResult(output, "out.pdf", "application/pdf")

    return ToolSpec(
        id="fake-size",
        label="Fake Size",
        description="",
        category="pdf",
        inputs=(".docx",),
        output="pdf",
        handler=handler,
    )


def test_batch_handler_runs_once_for_all_files(tmp_path, monkeypatch):
    calls: list[int] = []

    def batch_handler(contexts):
        calls.append(len(contexts))
        return [
            ConversionResult(
                _write_pdf(ctx.workdir / "out.pdf"),
                "out.pdf",
                "application/pdf",
            )
            for ctx in contexts
        ]

    monkeypatch.setattr(batch_conversion, "get_tool", lambda _: _fake_tool(batch_handler))
    monkeypatch.setattr(batch_conversion, "_worker_count", lambda: 2)
    monkeypatch.setattr(batch_conversion, "job_output_dir", lambda bid: tmp_path / bid)
    inputs, names = _inputs(tmp_path, 3)

    result = process_batch_conversion(
        "batch-one", "fake", inputs, names, {}, tmp_path / "work"
    )

    assert calls == [3]
    assert result.successful == 3
    assert result.failed == 0


def test_batch_handler_none_falls_back_per_file(tmp_path, monkeypatch):
    def batch_handler(contexts):
        return [
            ConversionResult(
                _write_pdf(contexts[0].workdir / "out.pdf"),
                "out.pdf",
                "application/pdf",
            ),
            None,
        ]

    converted: list[int] = []

    def fake_convert_one(index, input_path, original_name, tool, options, workdir):
        converted.append(index)
        output = _write_pdf(workdir / f"file_{index}" / "out.pdf")
        return BatchFileResult(
            index=index,
            original_name=original_name,
            success=True,
            output_path=output,
            output_filename="out.pdf",
            output_mime="application/pdf",
        )

    monkeypatch.setattr(batch_conversion, "get_tool", lambda _: _fake_tool(batch_handler))
    monkeypatch.setattr(batch_conversion, "_worker_count", lambda: 2)
    monkeypatch.setattr(batch_conversion, "job_output_dir", lambda bid: tmp_path / bid)
    monkeypatch.setattr(batch_conversion, "_convert_one", fake_convert_one)
    inputs, names = _inputs(tmp_path, 2)

    result = process_batch_conversion(
        "batch-two", "fake", inputs, names, {}, tmp_path / "work"
    )

    assert converted == [1]
    assert result.successful == 2
    assert result.failed == 0


def _run_size_batch(tmp_path, monkeypatch, payload: bytes) -> batch_conversion.BatchConversionResult:
    monkeypatch.setattr(batch_conversion, "get_tool", lambda _: _size_tool(payload))
    monkeypatch.setattr(batch_conversion, "_worker_count", lambda: 1)
    monkeypatch.setattr(batch_conversion, "job_output_dir", lambda bid: tmp_path / bid)
    inputs, names = _inputs(tmp_path, 2)
    return process_batch_conversion(
        "batch-size", "fake-size", inputs, names, {}, tmp_path / "work"
    )


def test_batch_zip_warns_when_archive_payload_is_large(tmp_path, monkeypatch):
    """Large image exports still get a ZIP, but the user is told it is heavy."""
    monkeypatch.setattr(batch_conversion, "ZIP_WARN_MB", 1)
    monkeypatch.setattr(batch_conversion, "ZIP_SKIP_MB", 100)
    payload = b"%PDF-1.4\n" + b"x" * (2 * 1024 * 1024)

    result = _run_size_batch(tmp_path, monkeypatch, payload)

    assert result.zip_path is not None and result.zip_path.exists()
    assert result.successful == 2
    assert any("berukuran besar" in warning for warning in result.warnings)
    assert not any("tidak dibuat" in warning for warning in result.warnings)


def test_batch_zip_is_skipped_above_hard_limit(tmp_path, monkeypatch):
    """An absurd archive is not built; per-file downloads must survive."""
    monkeypatch.setattr(batch_conversion, "ZIP_WARN_MB", 1)
    monkeypatch.setattr(batch_conversion, "ZIP_SKIP_MB", 2)
    payload = b"%PDF-1.4\n" + b"x" * (2 * 1024 * 1024)

    result = _run_size_batch(tmp_path, monkeypatch, payload)

    assert result.zip_path is None
    assert any("ZIP tidak dibuat" in warning for warning in result.warnings)
    assert all(item.success for item in result.file_results)
    assert all(
        item.output_path is not None and item.output_path.exists()
        for item in result.file_results
    )


def test_duplicate_files_convert_once(tmp_path, monkeypatch):
    """Byte-identical files are converted a single time and their results reused."""
    calls: list[Path] = []

    def handler(ctx: ConversionContext) -> ConversionResult:
        calls.append(ctx.input)
        return ConversionResult(
            _write_pdf(ctx.workdir / "out.pdf"),
            "out.pdf",
            "application/pdf",
        )

    tool = ToolSpec(
        id="fake-pdf",
        label="Fake PDF",
        description="",
        category="pdf",
        inputs=(".pdf",),
        output="pdf",
        handler=handler,
    )
    monkeypatch.setattr(batch_conversion, "get_tool", lambda _: tool)
    monkeypatch.setattr(batch_conversion, "_worker_count", lambda: 1)  # keep the run inline
    monkeypatch.setattr(batch_conversion, "job_output_dir", lambda bid: tmp_path / bid)

    inputs_dir = tmp_path / "inputs"
    inputs_dir.mkdir(parents=True, exist_ok=True)
    same_a = inputs_dir / "a.pdf"
    same_b = inputs_dir / "b.pdf"
    other = inputs_dir / "c.pdf"
    same_a.write_bytes(b"identical-pdf-bytes")
    same_b.write_bytes(b"identical-pdf-bytes")
    other.write_bytes(b"different-pdf-bytes")
    inputs = [same_a, same_b, other]
    names = [path.name for path in inputs]

    result = process_batch_conversion(
        "batch-dups", "fake-pdf", inputs, names, {}, tmp_path / "work"
    )

    assert len(calls) == 2  # a & b share one conversion
    assert result.successful == 3
    assert result.failed == 0
    assert all(item.success for item in result.file_results)
