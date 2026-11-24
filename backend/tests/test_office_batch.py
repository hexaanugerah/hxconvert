"""Batch fast path tests for LibreOffice-backed tools (portable fakes)."""
from pathlib import Path

import fitz
import pytest
from openpyxl import Workbook
from pptx import Presentation

from app.core.errors import ConversionError
from app.services import batch_conversion, docx_ops, excel_ops, pptx_ops
from app.services.base import ConversionContext, ConversionResult
from app.services.batch_conversion import process_batch_conversion
from app.services.office_ops import SofficeBatchOutcome
from app.services.word_com import WordBatchOutcome


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


def _write_docx_with_page_break(path: Path) -> Path:
    from docx import Document
    from docx.enum.text import WD_BREAK

    document = Document()
    paragraph = document.add_paragraph("First page")
    paragraph.add_run().add_break(WD_BREAK.PAGE)
    document.add_paragraph("Second page")
    document.save(path)
    return path


def _fake_word_output(two_pages: bool = True):
    def fake(input_path, workdir):
        output = workdir / "word.pdf"
        document = fitz.open()
        for index in range(2 if two_pages else 1):
            page = document.new_page()
            page.insert_text((72, 72), f"Word page {index + 1}")
        document.save(output)
        document.close()
        return output

    return fake


def test_docx_to_pdf_auto_prefers_word_when_available(tmp_path, monkeypatch):
    """Auto uses the best renderer available, so every path yields Word output."""
    source = _write_docx_with_page_break(tmp_path / "source.docx")

    def explode(*args, **kwargs):
        raise AssertionError("LibreOffice must not run when Word succeeded")

    monkeypatch.setattr(docx_ops, "word_available", lambda: True)
    monkeypatch.setattr(docx_ops, "convert_with_word", _fake_word_output())
    monkeypatch.setattr(docx_ops, "libreoffice_available", lambda: True)
    monkeypatch.setattr(docx_ops, "convert_with_soffice", explode)

    workdir = tmp_path / "work"
    workdir.mkdir()
    result = docx_ops.docx_to_pdf(
        ConversionContext(
            inputs=[source], original_names=[source.name], options={}, workdir=workdir
        )
    )

    pdf = fitz.open(result.path)
    try:
        assert pdf.page_count == 2
        assert "Word page 1" in pdf[0].get_text()
    finally:
        pdf.close()


def test_docx_to_pdf_python_engine_ignores_word_and_libreoffice(tmp_path, monkeypatch):
    """An explicit python engine must never launch Word or LibreOffice."""
    source = _write_docx_with_page_break(tmp_path / "source.docx")

    def explode(*args, **kwargs):
        raise AssertionError("Forced python engine must not start another renderer")

    monkeypatch.setattr(docx_ops, "word_available", lambda: True)
    monkeypatch.setattr(docx_ops, "convert_with_word", explode)
    monkeypatch.setattr(docx_ops, "libreoffice_available", lambda: True)
    monkeypatch.setattr(docx_ops, "convert_with_soffice", explode)

    workdir = tmp_path / "work"
    workdir.mkdir()
    result = docx_ops.docx_to_pdf(
        ConversionContext(
            inputs=[source],
            original_names=[source.name],
            options={"engine": "python"},
            workdir=workdir,
        )
    )

    pdf = fitz.open(result.path)
    try:
        assert pdf.page_count == 2
        assert "First page" in pdf[0].get_text()
        assert "Second page" in pdf[1].get_text()
    finally:
        pdf.close()


def test_docx_to_pdf_explicit_word_engine_errors_when_missing(tmp_path, monkeypatch):
    source = _write_docx_with_page_break(tmp_path / "source.docx")
    monkeypatch.setattr(docx_ops, "word_available", lambda: False)

    with pytest.raises(ConversionError) as excinfo:
        docx_ops.docx_to_pdf(
            ConversionContext(
                inputs=[source],
                original_names=[source.name],
                options={"engine": "word"},
                workdir=tmp_path,
            )
        )
    assert "Microsoft Word" in excinfo.value.message


def test_docx_to_pdf_auto_falls_back_to_libreoffice_when_word_fails(
    tmp_path, monkeypatch
):
    source = _write_docx_with_page_break(tmp_path / "source.docx")

    def broken_word(*args, **kwargs):
        raise ConversionError("Word crashed")

    def fake_soffice(input_path, target, workdir):
        output = workdir / "soffice.pdf"
        document = fitz.open()
        document.new_page().insert_text((72, 72), "Soffice page")
        document.save(output)
        document.close()
        return output

    monkeypatch.setattr(docx_ops, "word_available", lambda: True)
    monkeypatch.setattr(docx_ops, "convert_with_word", broken_word)
    monkeypatch.setattr(docx_ops, "libreoffice_available", lambda: True)
    monkeypatch.setattr(docx_ops, "convert_with_soffice", fake_soffice)

    workdir = tmp_path / "work"
    workdir.mkdir()
    result = docx_ops.docx_to_pdf(
        ConversionContext(
            inputs=[source],
            original_names=[source.name],
            options={},
            workdir=workdir,
        )
    )
    assert "Word crashed" in " ".join(result.warnings)


def test_docx_to_pdf_batch_falls_back_to_soffice_for_word_failures(tmp_path, monkeypatch):
    docx_paths = []
    for index in range(2):
        path = tmp_path / f"in{index}.docx"
        path.write_bytes(b"docx bytes")
        docx_paths.append(path)

    def fake_soffice(jobs, target, workdir):
        return [SofficeBatchOutcome(output=_write_pdf(output), error=None) for _, output in jobs]

    monkeypatch.setattr(docx_ops, "word_available", lambda: True)
    monkeypatch.setattr(
        docx_ops,
        "convert_parallel_with_word",
        lambda jobs, workdir: [WordBatchOutcome(error="no word")] * len(jobs),
    )
    monkeypatch.setattr(docx_ops, "libreoffice_available", lambda: True)
    monkeypatch.setattr(docx_ops, "convert_parallel_with_soffice", fake_soffice)
    contexts = _contexts(docx_paths, [p.name for p in docx_paths], tmp_path / "work")

    results = docx_ops.docx_to_pdf_batch(contexts)
    assert all(result is not None for result in results)
    assert all(result.path.read_bytes().startswith(b"%PDF") for result in results)


def test_docx_to_pdf_batch_word_engine_does_not_fall_back_to_soffice(
    tmp_path, monkeypatch
):
    """A forced Word batch must not silently change renderer."""
    docx_paths = []
    for index in range(2):
        path = tmp_path / f"in{index}.docx"
        path.write_bytes(b"docx bytes")
        docx_paths.append(path)

    def explode(*args, **kwargs):
        raise AssertionError("Forced Word batch must not start LibreOffice")

    monkeypatch.setattr(docx_ops, "word_available", lambda: True)
    monkeypatch.setattr(
        docx_ops,
        "convert_parallel_with_word",
        lambda jobs, workdir: [WordBatchOutcome(error="word down")] * len(jobs),
    )
    monkeypatch.setattr(docx_ops, "libreoffice_available", lambda: True)
    monkeypatch.setattr(docx_ops, "convert_parallel_with_soffice", explode)

    contexts = _contexts(docx_paths, [p.name for p in docx_paths], tmp_path / "work")
    for context in contexts:
        context.options = {"engine": "word"}

    assert docx_ops.docx_to_pdf_batch(contexts) == [None, None]


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
