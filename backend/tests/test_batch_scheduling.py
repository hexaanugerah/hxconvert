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
