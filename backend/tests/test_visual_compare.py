"""Visual comparison report between a source PDF and its round trip."""
from __future__ import annotations

import fitz

import roundtrip_report
from app.services import visual_compare


def _pdf(path, pages=1, extra_box: bool = False, moved: bool = False, shift: float = 0.0):
    document = fitz.open()
    for index in range(pages):
        page = document.new_page(width=595, height=842)
        y = 260 if moved and index == 0 else 120
        for line in range(8):
            page.insert_text(
                (72 + shift, y + line * 16), f"Halaman {index + 1} baris {line + 1}"
            )
        if extra_box and index == 0:
            page.draw_rect(fitz.Rect(300, 400, 520, 520), color=(0, 0, 0), fill=(0, 0, 0))
    document.save(str(path))
    document.close()
    return path


def test_identical_documents_have_no_difference(tmp_path):
    source = _pdf(tmp_path / "a.pdf")

    report = visual_compare.compare_pdfs(source, source)

    assert report.identical is True
    assert report.changed_pages == []
    assert report.average_ratio == 0
    assert report.pages[0].text_similarity == 1.0
    assert report.pages[0].identical is True


def test_moved_text_is_flagged_visually(tmp_path):
    first = _pdf(tmp_path / "a.pdf")
    second = _pdf(tmp_path / "b.pdf", moved=True)

    report = visual_compare.compare_pdfs(first, second)

    assert report.identical is False
    assert report.changed_pages == [1]
    assert report.pages[0].ratio > visual_compare.IDENTICAL_RATIO
    # The text is unchanged, only its position is different.
    assert report.pages[0].text_similarity == 1.0


def test_small_render_offset_is_aligned_before_comparing(tmp_path):
    """A renderer that places the page a couple of pixels off must not be
    reported as a content difference."""
    first = _pdf(tmp_path / "a.pdf")
    second = _pdf(tmp_path / "b.pdf", shift=2.0)

    report = visual_compare.compare_pdfs(first, second)

    assert report.pages[0].offset != (0, 0)
    assert report.pages[0].identical is True


def test_page_count_mismatch_is_reported(tmp_path):
    first = _pdf(tmp_path / "a.pdf", pages=2)
    second = _pdf(tmp_path / "b.pdf", pages=1)

    report = visual_compare.compare_pdfs(first, second)

    assert report.only_first == [2]
    assert report.only_second == []
    assert len(report.pages) == 1
    assert report.identical is False


def test_html_report_embeds_images_and_names(tmp_path):
    first = _pdf(tmp_path / "asli & uji.pdf")
    second = _pdf(tmp_path / "hasil.pdf", extra_box=True)

    report = visual_compare.compare_pdfs(first, second, first_name="asli & uji.pdf")
    html = visual_compare.build_html_report(report, warnings=["Catatan uji"])

    assert "Laporan Perbandingan Visual" in html
    assert "data:image/png;base64," in html
    assert html.count("<section class=\"page\">") == len(report.pages)
    assert "asli &amp; uji.pdf" in html
    assert "Catatan uji" in html
    assert "Halaman 1" in html


def test_cli_writes_report_for_an_existing_roundtrip_pdf(tmp_path):
    source = _pdf(tmp_path / "source.pdf")
    result = _pdf(tmp_path / "result.pdf", extra_box=True)
    output = tmp_path / "laporan"

    code = roundtrip_report.main(
        [str(source), "--roundtrip-pdf", str(result), "-o", str(output)]
    )

    assert code == 0
    report = (output / "report.html").read_text(encoding="utf-8")
    assert "Laporan Perbandingan Visual" in report
    assert "Halaman 1" in report
    assert not (output / "converted.docx").exists()


def test_cli_exit_code_reflects_differences(tmp_path):
    source = _pdf(tmp_path / "source.pdf")
    result = _pdf(tmp_path / "result.pdf", extra_box=True)

    code = roundtrip_report.main(
        [
            str(source),
            "--roundtrip-pdf",
            str(result),
            "-o",
            str(tmp_path / "laporan"),
            "--fail-on-difference",
        ]
    )

    assert code == 1
