"""Performance benchmark for DocuConvert conversion pipeline.

Generates reproducible sample documents, then measures each processing stage
in milliseconds using the same code paths the worker uses.

Usage:
    python benchmark.py --label before
    python benchmark.py --label after

Results are stored in benchmark_results_<label>.json next to this file.
"""
from __future__ import annotations

import argparse
import gc
import json
import shutil
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from io import BytesIO

import psutil

BACKEND_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

PROCESS = psutil.Process()


def rss_mb() -> float:
    return PROCESS.memory_info().rss / (1024 * 1024)


def peak_rss_mb() -> float:
    try:
        return PROCESS.memory_full_info().peak_wset / (1024 * 1024)
    except Exception:
        return rss_mb()


def timed_millis(fn):
    gc.collect()
    start = time.perf_counter()
    try:
        value = fn()
    except Exception as exc:
        return {"ms": round((time.perf_counter() - start) * 1000, 1), "error": str(exc)[:300]}
    return value, {"ms": round((time.perf_counter() - start) * 1000, 1)}


# ---------------------------------------------------------------- sample files


def _scanned_page_png() -> bytes:
    """Render one raster 'scan' page (gray bands + noise) as PNG bytes."""
    from PIL import Image, ImageDraw
    import random

    random.seed(42)
    image = Image.new("RGB", (1240, 1754), "white")
    draw = ImageDraw.Draw(image)
    for band in range(0, 1754, 120):
        draw.rectangle((90, band, 1150, band + 44), fill=(245, 245, 245))
    for _ in range(4000):
        x = random.randint(0, 1239)
        y = random.randint(0, 1753)
        image.putpixel((x, y), (random.randint(0, 60),) * 3)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def make_text_pdf(path: Path, pages: int = 10, stamp: int = 0) -> Path:
    import fitz

    doc = fitz.open()
    for index in range(pages):
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 70), f"Quarterly Report {stamp} - Page {index + 1}", fontsize=22)
        for line in range(12):
            page.insert_text(
                (72, 110 + line * 26),
                f"Laporan triwulan menyebutkan pertumbuhan pendapatan sebesar {10 + line} persen.",
                fontsize=11,
            )
    path.write_bytes(doc.tobytes(deflate=True))
    doc.close()
    return path


def make_image_pdf(path: Path, pages: int) -> Path:
    """A PDF whose every page is one full-page raster image (a 'scan')."""
    import fitz

    doc = fitz.open()
    page_bytes = _scanned_page_png()
    for _ in range(pages):
        page = doc.new_page(width=595, height=842)
        page.insert_image(page.rect, stream=page_bytes)
    path.write_bytes(doc.tobytes(deflate=False))
    doc.close()
    return path


def make_mixed_pdf(path: Path, pages: int = 8) -> Path:
    import fitz

    doc = fitz.open()
    page_bytes = _scanned_page_png()
    for index in range(pages):
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 70), f"Mixed document - Page {index + 1}", fontsize=18)
        for line in range(6):
            page.insert_text(
                (72, 110 + line * 24),
                f"Paragraf campuran ke-{line + 1} berisi teks digital yang panjang untuk diukur.",
                fontsize=11,
            )
        page.insert_image(fitz.Rect(340, 300, 560, 520), stream=page_bytes)
    path.write_bytes(doc.tobytes(deflate=True))
    doc.close()
    return path


def make_docx_many_images(path: Path, images: int = 8) -> Path:
    from docx import Document
    from docx.shared import Inches

    doc = Document()
    doc.add_heading("Laporan dengan banyak gambar", 1)
    page_bytes = _scanned_page_png()
    for index in range(images):
        doc.add_heading(f"Bagian {index + 1}", 2)
        doc.add_paragraph("Teks diikuti gambar hasil pindai berukuran penuh.")
        doc.add_picture(BytesIO(page_bytes), width=Inches(5.9))
    doc.save(str(path))
    return path


def fill_to_mb(path: Path, target_mb: int) -> Path:
    """Grow a full-image PDF until it reaches approximately target MB."""
    import fitz

    page_bytes = _scanned_page_png()
    per_page = len(page_bytes) * 1.02 + 512  # PNG streams are stored raw
    pages = max(1, int((target_mb * 1024 * 1024 * 0.95) / per_page))
    doc = fitz.open()
    for _ in range(pages):
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 70), "Large file filler page", fontsize=16)
        page.insert_image(page.rect, stream=page_bytes)
    path.write_bytes(doc.tobytes(deflate=False))
    doc.close()
    return path


# ------------------------------------------------------------------- scenarios


def convert_pdf_to_word(sample: Path, options: dict, run_dir: Path) -> dict:
    from app.services.base import ConversionContext
    from app.services import pdf_to_docx
    import fitz

    workdir = run_dir / (sample.stem + "-work")
    workdir.mkdir(parents=True, exist_ok=True)
    ctx = ConversionContext(
        inputs=[sample],
        original_names=[sample.name],
        options=options,
        workdir=workdir,
    )
    stages: dict = {}

    def measure(name: str, fn):
        gc.collect()
        start = time.perf_counter()
        try:
            fn()
        except Exception as exc:
            stages[name] = {"ms": round((time.perf_counter() - start) * 1000, 1), "error": str(exc)[:300]}
        else:
            stages[name] = {"ms": round((time.perf_counter() - start) * 1000, 1)}

    measure("open_and_validate", lambda: fitz.open(sample).close())
    measure("convert", lambda: pdf_to_docx.convert(ctx))
    result_path = workdir / "converted.docx"
    if result_path.exists():
        stages["output_mb"] = round(result_path.stat().st_size / (1024 * 1024), 2)
    shutil.rmtree(workdir, ignore_errors=True)
    return stages


def batch_convert(samples: list[Path], run_dir: Path, workers: int, options: dict) -> dict:
    """Run a batch conversion with a bounded worker pool."""
    from app.services.base import ConversionContext
    from app.services import pdf_to_docx

    run_dir.mkdir(parents=True, exist_ok=True)
    timings: dict = {"total_ms": None, "slowest_file_ms": None}
    start = time.perf_counter()
    slowest = 0.0

    def one(sample: Path):
        nonlocal slowest
        local_start = time.perf_counter()
        workdir = run_dir / f"{sample.stem}-w{workers}"
        workdir.mkdir(parents=True, exist_ok=True)
        ctx = ConversionContext(
            inputs=[sample],
            original_names=[sample.name],
            options=options,
            workdir=workdir,
        )
        pdf_to_docx.convert(ctx)
        elapsed = (time.perf_counter() - local_start) * 1000
        shutil.rmtree(workdir, ignore_errors=True)
        if elapsed > slowest:
            slowest = elapsed
        return elapsed

    with ThreadPoolExecutor(max_workers=workers) as pool:
        pool.map(one, samples)
    timings["total_ms"] = round((time.perf_counter() - start) * 1000, 1)
    timings["slowest_file_ms"] = round(slowest, 1)
    return timings


def warm_process_pool() -> None:
    """Prime the app's persistent process pool (worker spawn + imports) so it
    is not charged to the first measured batch."""
    try:
        from app.services.batch_conversion import _cpu_pool, _worker_count

        pool = _cpu_pool(_worker_count())
        pool.submit(lambda: True).result(timeout=120)
    except Exception as exc:
        print(f"process-pool warmup skipped: {exc}")


def batch_convert_app(samples: list[Path], run_dir: Path, options: dict, count_key: str) -> dict:
    """Measure the real application batch path: process_batch_conversion,
    including pool scheduling (process or thread, by tool) and the ZIP build.
    Worker count comes from the app's own _worker_count()."""
    from app.services.batch_conversion import process_batch_conversion
    from app.core.storage import remove_job_files

    run_dir.mkdir(parents=True, exist_ok=True)
    batch_id = f"bench-{count_key}-{int(time.time())}"
    start = time.perf_counter()
    result = process_batch_conversion(
        batch_id,
        "pdf-to-word",
        samples,
        [path.name for path in samples],
        options,
        run_dir,
    )
    total_ms = round((time.perf_counter() - start) * 1000, 1)
    from app.services.batch_conversion import _worker_count

    entry: dict = {
        "total_ms": total_ms,
        "successful": result.successful,
        "failed": result.failed,
        "workers": _worker_count(),
    }
    remove_job_files(batch_id)
    return entry


def zip_benchmark(results_dir: Path) -> dict:
    """Time building a ZIP of 15 chosen-format results through the app's path.

    Uses the app's selective-compression builder when available (new code);
    on the pre-optimisation snapshot that module does not exist, so a plain
    deflate-6 archive is timed instead (name is annotated accordingly).
    """
    import zipfile

    results_dir.mkdir(parents=True, exist_ok=True)
    # Build 15 mixed-format sample results: PDF/DOCX (stored-eligible) and CSV (deflatable).
    from app.services.base import ConversionContext
    from app.services import pdf_to_docx, excel_ops

    files: list[Path] = []
    text_pdf = results_dir / "text_zip_src.pdf"
    if not text_pdf.exists():
        make_text_pdf(text_pdf, 2)
    ctx = ConversionContext(inputs=[text_pdf], original_names=[text_pdf.name], options={"mode": "editable"}, workdir=results_dir)
    docx = pdf_to_docx.convert(ctx).path
    files.append(docx)
    csv_src = results_dir / "csv_src.csv"
    csv_src.write_text("id;nilai\n" + "".join(f"{i};{i * 7}\n" for i in range(2000)), encoding="utf-8")
    csv_ctx = ConversionContext(inputs=[csv_src], original_names=[csv_src.name], options={}, workdir=results_dir)
    files.append(excel_ops.csv_to_xlsx(csv_ctx).path)
    while len(files) < 15:
        files.append(files[len(files) % 2])

    marks = [path.suffix for path in files]
    names: list[str] = []
    for index, path in enumerate(files):
        names.append(f"result-{index:02d}{path.suffix}")

    try:
        from app.services.batch_conversion import _zip_method  # new code

        def build(target: Path) -> None:
            with zipfile.ZipFile(target, "w") as archive:
                for name, path, ext in zip(names, files, marks):
                    method, level = _zip_method(ext.lower())
                    archive.write(path, name, compress_type=method)
    except ImportError:  # old snapshot: everything deflated at level 6
        def build(target: Path) -> None:
            with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
                for name, path in zip(names, files):
                    archive.write(path, name)

    zip_path = results_dir / "benchmark-15-results.zip"
    if zip_path.exists():
        zip_path.unlink()
    import time as _time
    gc.collect()
    start = _time.perf_counter()
    build(zip_path)
    ms = round((_time.perf_counter() - start) * 1000, 1)
    size_mb = round(zip_path.stat().st_size / (1024 * 1024), 2)
    inputs_mb = round(sum(path.stat().st_size for path in files) / (1024 * 1024), 2)
    zip_path.unlink(missing_ok=True)
    return {"ms": ms, "zip_mb": size_mb, "inputs_mb": inputs_mb, "file_count": len(files)}


def build_samples(base: Path) -> Path:
    samples_dir = base / "samples"
    samples_dir.mkdir(parents=True, exist_ok=True)
    plans = {
        "image_pdf_1p.pdf": ("image", 1),
        "image_pdf_8p.pdf": ("image", 8),
        "image_pdf_30p.pdf": ("image", 30),
        "text_pdf_10p.pdf": ("text", 10),
        "mixed_pdf_8p.pdf": ("mixed", 8),
    }
    for name, (kind, count) in plans.items():
        path = samples_dir / name
        if not path.exists():
            if kind == "image":
                make_image_pdf(path, count)
            elif kind == "text":
                make_text_pdf(path, count)
            else:
                make_mixed_pdf(path, count)
    docx_path = samples_dir / "docx_images.docx"
    if not docx_path.exists():
        make_docx_many_images(docx_path)
    return samples_dir


SCENARIOS = {
    "image_pdf_1p": ("image_pdf_1p.pdf", {"mode": "full-image"}),
    "image_pdf_8p": ("image_pdf_8p.pdf", {"mode": "full-image"}),
    "image_pdf_30p": ("image_pdf_30p.pdf", {"mode": "full-image"}),
    "text_pdf_10p": ("text_pdf_10p.pdf", {"mode": "editable"}),
    "mixed_pdf_8p": ("mixed_pdf_8p.pdf", {"mode": "auto"}),
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", default="run")
    parser.add_argument(
        "--skip-large",
        action="store_true",
        help="Skip the very slow 50/100 MB samples (their 'before' values are already recorded).",
    )
    args = parser.parse_args()

    base = Path(tempfile.mkdtemp(prefix=f"docuconvert-bench-{args.label}-"))
    snapshot: dict = {
        "label": args.label,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "rss_mb_start": round(rss_mb(), 1),
    }
    try:
        start = time.perf_counter()
        samples_dir = build_samples(base)
        snapshot["sample_generation_ms"] = round((time.perf_counter() - start) * 1000, 1)

        results: dict = {}
        for name, (filename, options) in SCENARIOS.items():
            sample = samples_dir / filename
            size_mb = sample.stat().st_size / (1024 * 1024)
            entry = convert_pdf_to_word(sample, options, base / "runs")
            entry["input_mb"] = round(size_mb, 2)
            results[f"pdf_to_word:{name}"] = entry

        # Word with images -> PDF (uses LibreOffice/Word if available on host).
        try:
            from app.services.base import ConversionContext
            from app.services import docx_ops

            sample = samples_dir / "docx_images.docx"
            workdir = base / "runs" / "docx-work"
            workdir.mkdir(parents=True, exist_ok=True)
            ctx = ConversionContext(
                inputs=[sample], original_names=[sample.name], options={}, workdir=workdir
            )
            start = time.perf_counter()
            result = docx_ops.docx_to_pdf(ctx)
            ms = round((time.perf_counter() - start) * 1000, 1)
            entry = {"ms": ms, "output_mb": round(result.path.stat().st_size / (1024 * 1024), 2)}
            shutil.rmtree(workdir, ignore_errors=True)
        except Exception as exc:
            entry = {"error": str(exc)[:200]}
        results["word_to_pdf:docx_many_images"] = entry

        # Large files (50 MB and 100 MB full-image PDFs).
        if not args.skip_large:
            for mb in (50, 100):
                sample = samples_dir / f"large_{mb}mb.pdf"
                if not sample.exists():
                    start = time.perf_counter()
                    fill_to_mb(sample, mb)
                    print(f"built {mb} MB sample in {round((time.perf_counter() - start))}s")
                entry = convert_pdf_to_word(sample, {"mode": "full-image"}, base / "runs" / "large")
                entry["input_mb"] = round(sample.stat().st_size / (1024 * 1024), 1)
                results[f"pdf_to_word:large_{mb}mb"] = entry

        # Batches of 5 / 10 / 15 files with 1 vs 2 workers (image + text mix).
        warm_process_pool()
        batch_results: dict = {}
        image_sample = samples_dir / "image_pdf_1p.pdf"
        text_sample = samples_dir / "text_pdf_10p.pdf"
        for count in (5, 10, 15):
            samples = [image_sample if i % 2 == 0 else text_sample for i in range(count)]
            for workers in (1, 2):
                key = f"batch_{count}_files_w{workers}"
                entry = batch_convert(samples, base / "runs" / key, workers, {"mode": "full-image"})
                batch_results[key] = entry

            # The real application path: process pool (auto workers) + ZIP.
            # "unique" = 15 genuinely distinct text PDFs; "duplicates" = the
            # same mixed list (identical contents) to measure the fast path.
            unique_samples = []
            for i in range(count):
                path = samples_dir / f"uniq_text_{count}_{i}.pdf"
                if not path.exists():
                    make_text_pdf(path, pages=2, stamp=i)
                unique_samples.append(path)
            key = f"batch_{count}_files_app_auto"
            entry = batch_convert_app(
                unique_samples, base / "runs" / f"{key}-unique", {"mode": "editable"}, f"{count}u"
            )
            batch_results[key] = entry
            entry = batch_convert_app(
                samples, base / "runs" / f"{key}-dups", {"mode": "full-image"}, f"{count}d"
            )
            batch_results[f"{key}_duplicates"] = entry
        results["batch"] = batch_results

        try:
            results["zip:15_results"] = zip_benchmark(base / "ziprun")
        except Exception as exc:
            results["zip:15_results"] = {"error": str(exc)[:200]}

        snapshot["results"] = results
        snapshot["peak_rss_mb"] = round(peak_rss_mb(), 1)
    finally:
        shutil.rmtree(base, ignore_errors=True)

    out = BACKEND_DIR / f"benchmark_results_{args.label}.json"
    out.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
    print(json.dumps(snapshot, indent=2))
    print(f"\nResults written to {out}")


if __name__ == "__main__":
    main()
