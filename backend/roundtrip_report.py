"""Automated visual report for a PDF -> Word -> PDF round trip.

    python roundtrip_report.py dokumen.pdf
    python roundtrip_report.py dokumen.pdf --engine libreoffice
    python roundtrip_report.py dokumen.pdf --roundtrip-pdf hasil.pdf --open

The script converts the PDF to DOCX with the same service the API uses,
converts that DOCX back to PDF, then renders both PDFs and writes a
self-contained HTML report with side-by-side pages, a red difference overlay and
per-page pixel/text metrics.
"""
from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import webbrowser
from pathlib import Path

from app.core.errors import AppError
from app.services import docx_ops, pdf_to_docx
from app.services.base import ConversionContext
from app.services.visual_compare import DEFAULT_DPI, build_html_report, compare_pdfs

ENGINES = ("auto", "libreoffice", "word", "python")
MODES = ("auto", "layout", "editable", "full-image", "fast")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Jalankan PDF -> Word -> PDF lalu buat laporan perbandingan visual "
            "antara PDF asli dan hasilnya."
        )
    )
    parser.add_argument("pdf", type=Path, help="PDF asli yang akan diuji.")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Folder laporan (default: <nama-pdf>-visual-report di samping PDF).",
    )
    parser.add_argument(
        "--mode",
        choices=MODES,
        default="auto",
        help="Mode konversi PDF ke Word (default: auto, sama seperti aplikasi).",
    )
    parser.add_argument(
        "--engine",
        choices=ENGINES,
        default="auto",
        help=(
            "Mesin Word ke PDF: auto (LibreOffice bila ada), libreoffice, word "
            "(otomasi Microsoft Word), atau python. Default: auto."
        ),
    )
    parser.add_argument(
        "--roundtrip-pdf",
        type=Path,
        help="Pakai PDF hasil yang sudah ada; lewati kedua langkah konversi.",
    )
    parser.add_argument(
        "--dpi",
        type=int,
        default=DEFAULT_DPI,
        help=f"Resolusi render untuk pembandingan (default: {DEFAULT_DPI}).",
    )
    parser.add_argument(
        "--open",
        action="store_true",
        help="Buka laporan HTML di peramban setelah selesai.",
    )
    parser.add_argument(
        "--fail-on-difference",
        action="store_true",
        help="Keluar dengan kode 1 bila ada perbedaan visual (untuk CI).",
    )
    return parser.parse_args(argv)


def _convert_roundtrip(
    source: Path, *, mode: str, engine: str, workdir: Path
) -> tuple[Path, Path, list[str]]:
    """PDF -> DOCX -> PDF, returning both artefacts and conversion warnings."""
    warnings: list[str] = []
    first_dir = workdir / "pdf-to-word"
    first_dir.mkdir(parents=True, exist_ok=True)
    to_word = pdf_to_docx.convert(
        ConversionContext(
            inputs=[source],
            original_names=[source.name],
            workdir=first_dir,
            options={"mode": mode},
        )
    )
    warnings.extend(to_word.warnings)

    back_dir = workdir / "word-to-pdf"
    back_dir.mkdir(parents=True, exist_ok=True)
    to_pdf = docx_ops.docx_to_pdf(
        ConversionContext(
            inputs=[to_word.path],
            original_names=[to_word.filename],
            workdir=back_dir,
            options={"engine": engine},
        )
    )
    warnings.extend(to_pdf.warnings)
    return to_word.path, to_pdf.path, warnings


def _print_summary(report, report_path: Path, artifacts: list[Path]) -> None:
    print(f"Laporan : {report_path}")
    for artifact in artifacts:
        print(f"Artefak : {artifact}")
    print(
        f"Halaman : {report.first_page_count} asli, "
        f"{report.second_page_count} hasil, {len(report.pages)} dibandingkan"
    )
    print(f"Rata-rata piksel berbeda: {report.average_ratio * 100:.3f}%")
    if report.only_first:
        print("Hanya di PDF asli   : " + ", ".join(map(str, report.only_first)))
    if report.only_second:
        print("Hanya di PDF hasil  : " + ", ".join(map(str, report.only_second)))
    print()
    for page in report.pages:
        similarity = (
            f"{page.text_similarity * 100:6.2f}%"
            if page.text_similarity is not None
            else "   n/a"
        )
        print(
            f"  Halaman {page.page:>3}: {page.status.upper():7} "
            f"{page.ratio * 100:8.3f}% piksel, "
            f"selisih {page.mean_difference:5.2f}/255, "
            f"geser {page.offset[0]:+d},{page.offset[1]:+d} px, teks {similarity}"
        )
    if report.identical:
        print("\nKedua PDF identik secara visual pada semua halaman.")
    elif report.nearly_identical:
        print(
            "\nKedua PDF hampir identik: selisih tiap halaman di bawah 1% piksel "
            "(hanya rasterisasi font)."
        )
    else:
        print(f"\n{len(report.changed_pages)} halaman memiliki perbedaan visual.")


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    source = args.pdf.expanduser()
    if not source.is_file():
        print(f"Berkas tidak ditemukan: {source}", file=sys.stderr)
        return 2

    output_dir = (args.output or source.with_name(f"{source.stem}-visual-report")).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        with tempfile.TemporaryDirectory(prefix="roundtrip-report-") as temporary:
            workdir = Path(temporary)
            if args.roundtrip_pdf:
                roundtrip_pdf = args.roundtrip_pdf.expanduser()
                if not roundtrip_pdf.is_file():
                    print(f"PDF hasil tidak ditemukan: {roundtrip_pdf}", file=sys.stderr)
                    return 2
                warnings: list[str] = []
                artifacts: list[Path] = []
            else:
                docx_path, roundtrip_pdf, warnings = _convert_roundtrip(
                    source, mode=args.mode, engine=args.engine, workdir=workdir
                )
                docx_copy = output_dir / "converted.docx"
                shutil.copy(docx_path, docx_copy)
                artifacts = [docx_copy]

            report = compare_pdfs(
                source,
                roundtrip_pdf,
                dpi=max(72, min(300, args.dpi)),
                first_name=source.name,
                second_name=(
                    args.roundtrip_pdf.name
                    if args.roundtrip_pdf
                    else f"{source.stem}.pdf (hasil PDF ke Word ke PDF)"
                ),
            )
            pdf_copy = output_dir / "roundtrip.pdf"
            if not args.roundtrip_pdf:
                shutil.copy(roundtrip_pdf, pdf_copy)
                artifacts.append(pdf_copy)

            report_path = output_dir / "report.html"
            report_path.write_text(
                build_html_report(report, warnings=warnings), encoding="utf-8"
            )
    except AppError as exc:
        message = getattr(exc, "message", str(exc))
        print(f"Konversi gagal: {message}", file=sys.stderr)
        return 1
    except Exception as exc:  # pragma: no cover - unexpected renderer failure
        print(f"Gagal membuat laporan: {exc}", file=sys.stderr)
        return 1

    _print_summary(report, report_path, artifacts)
    if args.open:
        webbrowser.open(report_path.resolve().as_uri())
    if args.fail_on_difference and not report.identical:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
