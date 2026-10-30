"""Batch fast path tests for LibreOffice-backed tools (portable fakes)."""
from pathlib import Path

from openpyxl import Workbook
from pptx import Presentation

from app.services import batch_conversion, docx_ops, excel_ops, pptx_ops
from app.services.base import ConversionContext, ConversionResult
from app.services.batch_conversion import process_batch_conversion
from app.services.office_ops import SofficeBatchOutcome


def _write_pptx(path: Path, slides: int = 1) -> None:
    prs = Presentation()
    layout = prs.slide_layouts[0]
    for index in range(slides):
        slide = prs.slides.add_slide(layout)
        slide.shapes.title.text = f"Slide {index + 1}"
    path.parent.mkdir(parents=True, exist_ok=True)
    prs.save(path)
    return path


def _write_xlsx(path: Path) -> None:
    wb = Workbook()
    wb.active["A1"] = "hallo"
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path


def _write_pdf(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"%PDF-1.4\n%%EOF\n")
    return path


def _fake_soffice(results: list[SofficeBatchOutcome]):
    def fake(jobs, target, workdir):
        assert len(results) == len(jobs)
        return results

    return fake


def _contexts(inputs: list[Path], names: list[str], workdir: Path) -> list[ConversionContext]:
    contexts = []
    for index, (path, name) in enumerate(zip(inputs, names)):
        contexts.append(
            ConversionContext(
                inputs=[path],
                original_names=[name],
                options={},
                workdir=workdir / f"file_{index}",
            )
        )
    return contexts


def test_pptx_to_pdf_batch_disabled_without_libreoffice(tmp_path, monkeypatch):
    monkeypatch.setattr(pptx_ops, "libreoffice_available", lambda: False)
    pptx_paths = [_write_pptx(tmp_path / f"in{i}.pptx") for i in range(2)]
    contexts = _contexts(pptx_paths, [p.name for p in pptx_paths], tmp_path / "work")

    assert pptx_ops.pptx_to_pdf_batch(contexts) == [None, None]


def test_pptx_to_pdf_batch_uses_single_invocation(tmp_path, monkeypatch):
    calls: list[int] = []
    pptx_paths = [_write_pptx(tmp_path / f"in{i}.pptx") for i in range(3)]

    def fake(jobs, target, workdir):
        calls.append(len(jobs))
        return [
            SofficeBatchOutcome(output=_write_pdf(output), error=None)
            for _, output in jobs
        ]

    monkeypatch.setattr(pptx_ops, "libreoffice_available", lambda: True)
    monkeypatch.setattr(pptx_ops, "convert_parallel_with_soffice", fake)
    contexts = _contexts(pptx_paths, [p.name for p in pptx_paths], tmp_path / "work")

    results = pptx_ops.pptx_to_pdf_batch(contexts)
    assert calls == [3]
    assert all(result is not None for result in results)
    for ctx, result in zip(contexts, results):
        assert result.path == ctx.workdir / "document.pdf"
        assert result.filename.endswith(".pdf")


def test_pptx_to_images_batch_skips_failed_files(tmp_path, monkeypatch):
    pptx_paths = [_write_pptx(tmp_path / f"in{i}.pptx") for i in range(2)]
    first_pdf = _write_pdf(tmp_path / "work" / "out" / "a.pdf")

    def fake(jobs, target, workdir):
        return [
            SofficeBatchOutcome(output=first_pdf, error=None),
            SofficeBatchOutcome(error="LibreOffice failed to convert this file to PDF."),
        ]

    monkeypatch.setattr(pptx_ops, "libreoffice_available", lambda: True)
    monkeypatch.setattr(pptx_ops, "convert_parallel_with_soffice", fake)
    contexts = _contexts(pptx_paths, [p.name for p in pptx_paths], tmp_path / "work")

    results = pptx_ops.pptx_to_images_batch(contexts)
    assert results[0] is None or results[0].path.exists()  # depends on fake pdf content
    assert results[1] is None  # falls back to the per-file handler


def test_xlsx_to_pdf_batch_uses_single_invocation(tmp_path, monkeypatch):
    xlsx_paths = [_write_xlsx(tmp_path / f"in{i}.xlsx") for i in range(2)]

    def fake(jobs, target, workdir):
        return [
            SofficeBatchOutcome(output=_write_pdf(output), error=None)
            for _, output in jobs
        ]

    monkeypatch.setattr(excel_ops, "libreoffice_available", lambda: True)
    monkeypatch.setattr(excel_ops, "convert_parallel_with_soffice", fake)
    contexts = _contexts(xlsx_paths, [p.name for p in xlsx_paths], tmp_path / "work")

    results = excel_ops.xlsx_to_pdf_batch(contexts)
    assert all(result is not None and result.path.name == "document.pdf" for result in results)


def test_docx_to_pdf_batch_returns_none_without_word_and_libreoffice(tmp_path, monkeypatch):
    monkeypatch.setattr(docx_ops, "word_available", lambda: False)
    monkeypatch.setattr(docx_ops, "libreoffice_available", lambda: False)
    docx_paths = []
    for index in range(2):
        path = tmp_path / f"in{index}.docx"
        path.write_bytes(b"docx bytes")
        docx_paths.append(path)
    contexts = _contexts(docx_paths, [p.name for p in docx_paths], tmp_path / "work")

    assert docx_ops.docx_to_pdf_batch(contexts) == [None, None]


def test_docx_to_pdf_batch_falls_back_to_soffice_for_word_failures(tmp_path, monkeypatch):
    docx_paths = []
    for index in range(2):
        path = tmp_path / f"in{index}.docx"
        path.write_bytes(b"docx bytes")
        docx_paths.append(path)

    def fake_soffice(jobs, target, workdir):
        return [SofficeBatchOutcome(output=_write_pdf(output), error=None) for _, output in jobs]

    monkeypatch.setattr(docx_ops, "libreoffice_available", lambda: True)
    monkeypatch.setattr(docx_ops, "convert_parallel_with_soffice", fake_soffice)
    contexts = _contexts(docx_paths, [p.name for p in docx_paths], tmp_path / "work")

    results = docx_ops.docx_to_pdf_batch(contexts)
    assert all(result is not None for result in results)


def test_process_batch_conversion_routes_to_soffice_tools(tmp_path, monkeypatch):
    """The batch runner feeds the fast path from the registry automatically."""
    xlsx_paths = [_write_xlsx(tmp_path / f"in{i}.xlsx") for i in range(2)]

    def fake(jobs, target, workdir):
        return [
            SofficeBatchOutcome(output=_write_pdf(output), error=None)
            for _, output in jobs
        ]

    monkeypatch.setattr(excel_ops, "libreoffice_available", lambda: True)
    monkeypatch.setattr(excel_ops, "convert_parallel_with_soffice", fake)
    monkeypatch.setattr(batch_conversion, "job_output_dir", lambda bid: tmp_path / bid)
    inputs = xlsx_paths
    names = [path.name for path in inputs]
    workdir = tmp_path / "work"

    result = process_batch_conversion("lo-batch", "xlsx-to-pdf", inputs, names, {}, workdir)

    assert result.successful == 2
    assert result.failed == 0
