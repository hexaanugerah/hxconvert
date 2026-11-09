"""Large-batch benchmark for the batch pipeline (convert + copy + ZIP).

This is the reproducible version of the ad-hoc measurement used to tune the
parallel conversion path. It generates realistic, image-heavy samples, warms
the process pool exactly like the server does, then runs the real application
entry point (``process_batch_conversion``) several times and reports the
end-to-end time split into conversion and ZIP packaging.

Usage:
    python benchmark_large_batch.py                      # 20 PDF, mode auto, 3 runs
    python benchmark_large_batch.py --mode fast          # fast JPEG mode
    python benchmark_large_batch.py --mode fast --runs 5 --json fast.json
    python benchmark_large_batch.py --tool docx-to-pdf --files 20
    python benchmark_large_batch.py --keep               # keep generated samples

Samples are generated once per invocation and reused across runs. A DOCX
conversion uses the tool's default engine (LibreOffice when installed, else
the deterministic Python renderer); add --option engine=word to compare with
Microsoft Word automation.
"""
from __future__ import annotations

import argparse
import gc
import json
import random
import shutil
import sys
import tempfile
import time
from io import BytesIO
from pathlib import Path
from statistics import median

BACKEND_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))


# ------------------------------------------------------------------- samples


def _photo_jpeg(width: int = 900, height: int = 620, seed: int = 7) -> bytes:
    """A photo-like JPEG with enough grain to be realistically incompressible.

    A flat gradient compresses to a few kilobytes; grain makes the sample cost
    what a real scanned/photo page costs (roughly 120-180 KB at this size).
    """
    import numpy as np
    from PIL import Image

    rng = np.random.default_rng(seed)
    vertical = np.linspace(0, 1, height, dtype=np.float32)[:, None, None]
    horizontal = np.linspace(0, 1, width, dtype=np.float32)[None, :, None]
    base = np.concatenate(
        [
            40 + 130 * vertical + 30 * horizontal,
            70 + 40 * vertical - 20 * horizontal,
            150 - 90 * vertical + 20 * horizontal,
        ],
        axis=2,
    )
    grain = rng.normal(0, 14, (height, width, 3)).astype(np.float32)
    pixels = np.clip(base + grain, 0, 255).astype(np.uint8)
    buffer = BytesIO()
    Image.fromarray(pixels, "RGB").save(buffer, format="JPEG", quality=82)
    return buffer.getvalue()


def make_large_pdf(path: Path, photos: list[bytes], pages: int = 12, seed: int = 1) -> Path:
    """A4 pages with a real text layer and one JPEG photo per page.

    Pages like these are classified as "mixed" by the converter, which is the
    expensive case: auto mode runs the pdf2docx layout analysis on every page.
    """
    import fitz

    document = fitz.open()
    rng = random.Random(seed)
    for index in range(pages):
        photo = photos[index % len(photos)]
        page = document.new_page(width=595, height=842)
        page.insert_text((64, 64), f"Laporan Tahunan {seed} - Halaman {index + 1}", fontsize=18)
        for line in range(16):
            left = 64
            page.insert_text(
                (left, 100 + line * 24),
                f"Paragraf {line + 1}: pendapatan tumbuh {rng.randint(3, 24)} persen dan "
                "biaya operasional tetap terkendali pada kuartal ini.",
                fontsize=10.5,
            )
        page.insert_image(fitz.Rect(64, 500, 531, 800), stream=photo)
    path.write_bytes(document.tobytes(deflate=True))
    document.close()
    return path


def make_large_docx(path: Path, photos: list[bytes], sections: int = 6, seed: int = 1) -> Path:
    """Six illustrated sections with headings, paragraphs, tables and images."""
    from docx import Document
    from docx.shared import Inches

    document = Document()
    document.add_heading(f"Laporan Besar {seed}", 0)
    for index in range(sections):
        document.add_heading(f"Bagian {index + 1}", level=1)
        for line in range(8):
            document.add_paragraph(
                f"Paragraf {line + 1} bagian {index + 1}: pembahasan lengkap mengenai "
                "hasil operasional, proyeksi, dan rekomendasi untuk periode berikutnya."
            )
        document.add_picture(BytesIO(photos[index % len(photos)]), width=Inches(6.0))
        table = document.add_table(rows=4, cols=4)
        for row in range(4):
            for column in range(4):
                table.cell(row, column).text = f"R{row + 1}C{column + 1}"
            if row == 0:
                table.style = "Table Grid"
    document.save(str(path))
    return path


def build_samples(directory: Path, tool: str, count: int) -> list[Path]:
    """Create the samples once. Each page/section gets a *different* photo,
    because both PyMuPDF and python-docx deduplicate identical images inside one
    document, which would silently make the samples far smaller than real ones."""
    directory.mkdir(parents=True, exist_ok=True)
    photos = [_photo_jpeg(seed=100 + index) for index in range(12)]
    paths: list[Path] = []
    for index in range(count):
        if tool == "pdf-to-word":
            paths.append(make_large_pdf(directory / f"large_{index + 1:02d}.pdf", photos, seed=index + 1))
        else:
            paths.append(make_large_docx(directory / f"large_{index + 1:02d}.docx", photos, seed=index + 1))
    return paths


# ------------------------------------------------------------------ measuring


def _warmup_task() -> bool:
    return True


def warm_process_pool() -> None:
    """Spawn and import inside every pool worker before measuring anything."""
    from app.services.batch_conversion import _cpu_pool, _worker_count

    pool = _cpu_pool(_worker_count())
    pool.submit(_warmup_task).result(timeout=120)


def run_once(tool: str, samples: list[Path], options: dict, batch_tag: str) -> dict:
    """Run the real application batch path once and split out ZIP packaging."""
    import app.services.batch_conversion as batch
    from app.core.storage import remove_job_files

    captured: dict[str, float] = {}
    original_zip = batch.create_batch_zip

    def timed_zip(*args, **kwargs):
        started = time.perf_counter()
        try:
            return original_zip(*args, **kwargs)
        finally:
            captured["zip_ms"] = (time.perf_counter() - started) * 1000

    batch.create_batch_zip = timed_zip
    batch_id = f"bench-large-{batch_tag}-{int(time.time() * 1000)}"
    workdir = Path(tempfile.mkdtemp(prefix=f"bench-{batch_tag}-"))
    try:
        gc.collect()
        started = time.perf_counter()
        result = batch.process_batch_conversion(
            batch_id,
            tool,
            samples,
            [path.name for path in samples],
            options,
            workdir,
        )
        total_ms = (time.perf_counter() - started) * 1000
        zip_mb = (
            round(result.zip_path.stat().st_size / (1024 * 1024), 1)
            if result.zip_path and result.zip_path.exists()
            else None
        )
    finally:
        batch.create_batch_zip = original_zip
        shutil.rmtree(workdir, ignore_errors=True)
        remove_job_files(batch_id)

    zip_ms = captured.get("zip_ms", 0.0)
    return {
        "total_ms": round(total_ms, 1),
        "convert_and_copy_ms": round(total_ms - zip_ms, 1),
        "zip_ms": round(zip_ms, 1),
        "workers": batch._worker_count(),
        "successful": result.successful,
        "failed": result.failed,
        "zip_mb": zip_mb,
    }


def summarise(runs: list[dict]) -> dict:
    def spread(key: str) -> dict:
        values = [run[key] for run in runs if isinstance(run.get(key), (int, float))]
        if not values:
            return {}
        return {
            "min": round(min(values), 1),
            "median": round(median(values), 1),
            "max": round(max(values), 1),
        }

    return {
        "total_ms": spread("total_ms"),
        "convert_and_copy_ms": spread("convert_and_copy_ms"),
        "zip_ms": spread("zip_ms"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tool", default="pdf-to-word", choices=("pdf-to-word", "docx-to-pdf"))
    parser.add_argument(
        "--mode",
        default=None,
        help="pdf-to-word mode (auto, fast, layout, editable, full-image). Ignored for docx-to-pdf.",
    )
    parser.add_argument(
        "--option",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Extra conversion option for the tool, e.g. --option fast_dpi=90 (repeatable).",
    )
    parser.add_argument("--files", type=int, default=20, help="Number of files per batch (default 20).")
    parser.add_argument("--runs", type=int, default=3, help="Measured runs (default 3).")
    parser.add_argument("--samples", type=Path, default=None, help="Reuse a sample directory instead of a temp one.")
    parser.add_argument("--keep", action="store_true", help="Keep generated samples and print their directory.")
    parser.add_argument("--json", type=Path, default=None, help="Write the full report to this JSON file.")
    args = parser.parse_args()

    options: dict = {}
    if args.tool == "pdf-to-word" and args.mode:
        options["mode"] = args.mode
    for item in args.option:
        key, separator, value = item.partition("=")
        if not separator or not key.strip():
            parser.error(f"--option expects KEY=VALUE, got {item!r}")
        options[key.strip()] = int(value) if value.strip().isdigit() else value.strip()

    temporary = args.samples is None and not args.keep
    sample_dir = args.samples or Path(tempfile.mkdtemp(prefix="bench-large-samples-"))
    report: dict = {
        "tool": args.tool,
        "options": options,
        "files": args.files,
        "runs_requested": args.runs,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    runs: list[dict] = []
    try:
        started = time.perf_counter()
        samples = build_samples(sample_dir, args.tool, args.files)
        total_mb = sum(path.stat().st_size for path in samples) / (1024 * 1024)
        report["sample_generation_ms"] = round((time.perf_counter() - started) * 1000, 1)
        report["sample_dir"] = str(sample_dir)
        report["sample_total_mb"] = round(total_mb, 1)
        report["sample_avg_mb"] = round(total_mb / len(samples), 2)
        print(
            f"{len(samples)} sample {args.tool} ({report['sample_avg_mb']} MB avg, "
            f"{report['sample_total_mb']} MB total) in {report['sample_generation_ms']:.0f} ms"
        )

        warm_process_pool()
        for run in range(1, args.runs + 1):
            entry = run_once(args.tool, samples, options, f"{args.tool}-{run}")
            runs.append(entry)
            print(
                f"run {run}: total {entry['total_ms']:.0f} ms "
                f"(convert+copy {entry['convert_and_copy_ms']:.0f} ms, "
                f"zip {entry['zip_ms']:.0f} ms), "
                f"{entry['successful']}/{len(samples)} ok, {entry['zip_mb']} MB zip"
            )
    finally:
        if temporary:
            shutil.rmtree(sample_dir, ignore_errors=True)

    report["runs"] = runs
    report["summary"] = summarise(runs)
    print(json.dumps(report["summary"], indent=2))
    if args.json:
        args.json.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"report written to {args.json}")


if __name__ == "__main__":
    main()
