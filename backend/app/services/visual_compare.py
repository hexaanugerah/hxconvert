"""Visual comparison of two PDFs, with a self-contained HTML report.

Used to check how closely a PDF -> Word -> PDF round trip matches the original.
Pages are rendered at the same resolution, normalised to equal size, lightly
blurred to ignore anti-aliasing, and compared pixel by pixel. Each page gets
three pictures in the report: the original, the result, and the original with
changed pixels marked in red.
"""
from __future__ import annotations

import base64
import difflib
import html
import io
import re
from dataclasses import dataclass, field
from pathlib import Path

import fitz
from PIL import Image, ImageChops, ImageFilter, ImageStat

DEFAULT_DPI = 110
# A pixel counts as changed when the brightness differs by more than this. The
# value tolerates font hinting and anti-aliasing differences between renderers,
# while still catching a moved colon or a re-flowed table row.
DIFF_THRESHOLD = 48
BLUR_RADIUS = 2
# A whole page can land one or two pixels off because of renderer margins. Find
# that translation first so it is reported instead of counted as content drift.
ALIGN_LIMIT = 6
# Below this share of changed pixels a page is reported as identical. Pages
# with only rendering (hinting/anti-aliasing) differences land under CLOSE_RATIO.
IDENTICAL_RATIO = 0.001
CLOSE_RATIO = 0.01


@dataclass
class PageComparison:
    page: int
    ratio: float
    mean_difference: float
    text_similarity: float | None
    offset: tuple[int, int]
    original_png: bytes
    result_png: bytes
    difference_png: bytes

    @property
    def identical(self) -> bool:
        return self.ratio < IDENTICAL_RATIO

    @property
    def close(self) -> bool:
        return self.ratio < CLOSE_RATIO

    @property
    def status(self) -> str:
        if self.identical:
            return "Sama"
        if self.close:
            return "Mirip"
        return "Berbeda"

    @property
    def css_class(self) -> str:
        if self.identical:
            return "ok"
        if self.close:
            return "warn"
        return "bad"


@dataclass
class ComparisonReport:
    first_name: str
    second_name: str
    dpi: int
    pages: list[PageComparison] = field(default_factory=list)
    only_first: list[int] = field(default_factory=list)
    only_second: list[int] = field(default_factory=list)
    first_page_count: int = 0
    second_page_count: int = 0

    @property
    def changed_pages(self) -> list[int]:
        return [page.page for page in self.pages if not page.identical]

    @property
    def average_ratio(self) -> float:
        if not self.pages:
            return 0.0
        return sum(page.ratio for page in self.pages) / len(self.pages)

    @property
    def identical(self) -> bool:
        return (
            not self.changed_pages
            and not self.only_first
            and not self.only_second
            and self.first_page_count == self.second_page_count
        )

    @property
    def nearly_identical(self) -> bool:
        """All pages within the close-rendering tolerance."""
        return (
            self.second_page_count == self.first_page_count
            and not self.only_first
            and not self.only_second
            and all(page.close for page in self.pages)
        )


def _normalise_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def _png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def _best_offset(first: Image.Image, second: Image.Image) -> tuple[int, int]:
    """Translation that best aligns ``second`` onto ``first``, in pixels."""
    best = (0, 0)
    best_score = ImageStat.Stat(ImageChops.difference(first, second)).mean[0]
    for dx in range(-ALIGN_LIMIT, ALIGN_LIMIT + 1):
        for dy in range(-ALIGN_LIMIT, ALIGN_LIMIT + 1):
            if dx == 0 and dy == 0:
                continue
            box_a, box_b = _overlap_boxes(first.size, dx, dy)
            score = ImageStat.Stat(
                ImageChops.difference(first.crop(box_a), second.crop(box_b))
            ).mean[0]
            if score < best_score:
                best_score = score
                best = (dx, dy)
    return best


def _overlap_boxes(
    size: tuple[int, int], dx: int, dy: int
) -> tuple[tuple[int, int, int, int], tuple[int, int, int, int]]:
    """Boxes covering the same content in both pages for a given translation."""
    width, height = size
    left, top = max(0, dx), max(0, dy)
    right, bottom = min(width, width + dx), min(height, height + dy)
    return (
        (left, top, right, bottom),
        (left - dx, top - dy, right - dx, bottom - dy),
    )


def _page_image(document: fitz.Document, index: int, dpi: int) -> Image.Image:
    pixmap = document[index].get_pixmap(dpi=dpi, alpha=False)
    return Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)


def _page_text(document: fitz.Document, index: int) -> str:
    try:
        return document[index].get_text("text")
    except Exception:  # pragma: no cover - malformed page contents
        return ""


def compare_pdfs(
    first_path: Path,
    second_path: Path,
    *,
    dpi: int = DEFAULT_DPI,
    first_name: str | None = None,
    second_name: str | None = None,
) -> ComparisonReport:
    """Render and compare page by page. The report keeps PNG previews."""
    first = fitz.open(str(first_path))
    second = fitz.open(str(second_path))
    report = ComparisonReport(
        first_name=first_name or first_path.name,
        second_name=second_name or second_path.name,
        dpi=dpi,
        first_page_count=first.page_count,
        second_page_count=second.page_count,
    )
    try:
        compared = min(first.page_count, second.page_count)
        report.only_first = list(range(compared + 1, first.page_count + 1))
        report.only_second = list(range(compared + 1, second.page_count + 1))
        for index in range(compared):
            original = _page_image(first, index, dpi)
            result = _page_image(second, index, dpi)
            if result.size != original.size:
                result = result.resize(original.size, Image.LANCZOS)

            grey_a = original.convert("L")
            grey_b = result.convert("L")
            offset = _best_offset(grey_a, grey_b) if grey_a.size == grey_b.size else (0, 0)
            box_a, box_b = _overlap_boxes(grey_a.size, *offset)
            crop_a = grey_a.crop(box_a).filter(ImageFilter.GaussianBlur(BLUR_RADIUS))
            crop_b = grey_b.crop(box_b).filter(ImageFilter.GaussianBlur(BLUR_RADIUS))
            difference = ImageChops.difference(crop_a, crop_b)
            mask = difference.point(
                lambda value: 255 if value > DIFF_THRESHOLD else 0
            )
            total = crop_a.width * crop_a.height
            changed = total - mask.histogram()[0]
            ratio = changed / total if total else 0.0

            # Mark the changed pixels on the original page for the report.
            full_mask = Image.new("L", grey_a.size, 0)
            full_mask.paste(mask, (box_a[0], box_a[1]))
            colour = original.copy()
            colour.paste(
                Image.new("RGB", colour.size, (255, 40, 40)), mask=full_mask
            )

            text_a = _normalise_text(_page_text(first, index))
            text_b = _normalise_text(_page_text(second, index))
            similarity = (
                difflib.SequenceMatcher(None, text_a, text_b, autojunk=False).ratio()
                if text_a or text_b
                else None
            )

            report.pages.append(
                PageComparison(
                    page=index + 1,
                    ratio=ratio,
                    mean_difference=ImageStat.Stat(difference).mean[0],
                    text_similarity=similarity,
                    offset=offset,
                    original_png=_png(original),
                    result_png=_png(result),
                    difference_png=_png(colour),
                )
            )
    finally:
        first.close()
        second.close()
    return report


def _image_tag(payload: bytes, caption: str) -> str:
    encoded = base64.b64encode(payload).decode("ascii")
    return (
        "<figure>"
        f'<img alt="{html.escape(caption)}" src="data:image/png;base64,{encoded}"/>'
        f"<figcaption>{html.escape(caption)}</figcaption>"
        "</figure>"
    )


def _percent(value: float) -> str:
    return f"{value * 100:.3f}%"





def build_html_report(report: ComparisonReport, *, warnings: list[str] | None = None) -> str:
    """Render the comparison as one HTML file with embedded images."""
    rows = []
    for page in report.pages:
        similarity = (
            f"{page.text_similarity * 100:.2f}%"
            if page.text_similarity is not None
            else "&mdash;"
        )
        rows.append(
            "<tr>"
            f'<td class="num">{page.page}</td>'
            f'<td class="num">{_percent(page.ratio)}</td>'
            f'<td class="num">{page.mean_difference:.2f}</td>'
            f'<td class="num">{similarity}</td>'
            f'<td class="num">{page.offset[0]:+d}, {page.offset[1]:+d}</td>'
            f'<td class="{page.css_class}">{page.status}</td>'
            "</tr>"
        )

    sections = []
    for page in report.pages:
        sections.append(
            '<section class="page">'
            f'<h2>Halaman {page.page} '
            f'<span class="{page.css_class}">{page.status}</span></h2>'
            '<p class="metrics">'
            f"Piksel berbeda: <strong>{_percent(page.ratio)}</strong> &middot; "
            f"rata-rata selisih: <strong>{page.mean_difference:.2f}/255</strong> &middot; "
            f"perataan: <strong>{page.offset[0]:+d}, {page.offset[1]:+d} px</strong> &middot; "
            "kemiripan teks: <strong>"
            + (
                f"{page.text_similarity * 100:.2f}%"
                if page.text_similarity is not None
                else "tidak tersedia"
            )
            + "</strong></p>"
            '<div class="panels">'
            + _image_tag(page.original_png, "PDF asli")
            + _image_tag(page.result_png, "Hasil PDF ke Word ke PDF")
            + _image_tag(page.difference_png, "Selisih (area berubah merah)")
            + "</div></section>"
        )

    extra_pages = []
    if report.only_first:
        extra_pages.append(
            "<p>Halaman yang hanya ada di PDF asli: "
            + ", ".join(map(str, report.only_first))
            + ".</p>"
        )
    if report.only_second:
        extra_pages.append(
            "<p>Halaman yang hanya ada di PDF hasil: "
            + ", ".join(map(str, report.only_second))
            + ".</p>"
        )

    warning_html = ""
    if warnings:
        items = "".join(f"<li>{html.escape(item)}</li>" for item in warnings)
        warning_html = f'<div class="warnings"><h2>Catatan konversi</h2><ul>{items}</ul></div>'

    if report.identical:
        verdict = "Kedua PDF identik secara visual pada semua halaman."
    elif report.nearly_identical:
        verdict = (
            "Kedua PDF hampir identik: semua halaman berada di bawah 1% piksel "
            "berbeda, sehingga selisihnya hanya dari rasterisasi font."
        )
    else:
        verdict = (
            f"{len(report.changed_pages)} dari {len(report.pages)} halaman "
            "memiliki perbedaan visual."
        )

    return f"""<!DOCTYPE html>
<html lang="id">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Laporan Perbandingan Visual</title>
<style>
  :root {{ color-scheme: light; }}
  body {{ font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
         margin: 0; padding: 32px; background: #f8fafc; color: #0f172a; }}
  h1 {{ font-size: 24px; margin: 0 0 6px; }}
  h2 {{ font-size: 18px; margin: 0 0 6px; }}
  .verdict {{ font-size: 15px; color: #334155; margin: 0 0 18px; }}
  .meta, .summary {{ background: #fff; border: 1px solid #e2e8f0; border-radius: 10px;
                     padding: 16px 18px; margin: 0 0 20px; }}
  .meta table, .summary table {{ border-collapse: collapse; width: 100%; font-size: 14px; }}
  .meta td, .meta th, .summary td, .summary th {{ padding: 6px 8px; border-bottom: 1px solid #f1f5f9;
                                                  text-align: left; }}
  .summary td.num, .summary th.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  .ok {{ color: #15803d; font-weight: 600; }}
  .warn {{ color: #b45309; font-weight: 600; }}
  .bad {{ color: #b91c1c; font-weight: 600; }}
  .page {{ background: #fff; border: 1px solid #e2e8f0; border-radius: 10px;
           padding: 18px; margin: 0 0 22px; }}
  .metrics {{ font-size: 13px; color: #475569; margin: 0 0 14px; }}
  .panels {{ display: grid; gap: 14px; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); }}
  figure {{ margin: 0; }}
  figure img {{ width: 100%; height: auto; display: block; border: 1px solid #cbd5e1;
                border-radius: 6px; background: #fff; }}
  figcaption {{ font-size: 12px; color: #64748b; margin-top: 6px; }}
  .warnings {{ background: #fffbeb; border: 1px solid #fde68a; border-radius: 10px;
               padding: 14px 18px; margin: 0 0 20px; }}
  .warnings h2 {{ font-size: 15px; }}
  .warnings ul {{ margin: 6px 0 0 18px; padding: 0; font-size: 13px; color: #78350f; }}
  .note {{ font-size: 12px; color: #64748b; margin-top: 8px; }}
</style>
</head>
<body>
<h1>Laporan Perbandingan Visual</h1>
<p class="verdict">{html.escape(verdict)}</p>
<div class="meta"><table>
  <tr><th>PDF asli</th><td>{html.escape(report.first_name)}</td></tr>
  <tr><th>PDF hasil</th><td>{html.escape(report.second_name)}</td></tr>
  <tr><th>Resolusi pembandingan</th><td>{report.dpi} DPI</td></tr>
  <tr><th>Jumlah halaman</th><td>{report.first_page_count} &rarr; {report.second_page_count}</td></tr>
  <tr><th>Rata-rata piksel berbeda</th><td>{_percent(report.average_ratio)}</td></tr>
</table></div>
<div class="summary">
  <h2>Ringkasan per halaman</h2>
  <table>
    <thead><tr><th class="num">Halaman</th><th class="num">Piksel berbeda</th>
      <th class="num">Rata-rata selisih</th><th class="num">Kemiripan teks</th>
      <th class="num">Perataan (px)</th><th>Status</th></tr></thead>
    <tbody>{''.join(rows) or '<tr><td colspan="6">Tidak ada halaman yang dapat dibandingkan.</td></tr>'}</tbody>
  </table>
  {''.join(extra_pages)}
  <p class="note">Setiap halaman diselaraskan dulu (geser maksimal {ALIGN_LIMIT} px)
  agar perbedaan margin renderer tidak dihitung sebagai perbedaan isi. Piksel
  dianggap berubah bila selisih kecerahan lebih dari {DIFF_THRESHOLD}/255 setelah
  blur {BLUR_RADIUS} px; halaman disebut sama bila kurang dari
  {IDENTICAL_RATIO * 100:.1f}% piksel berubah; di bawah {CLOSE_RATIO * 100:.0f}%
  disebut mirip (hanya perbedaan rasterisasi).</p>
</div>
{warning_html}
{''.join(sections)}
</body>
</html>
"""
