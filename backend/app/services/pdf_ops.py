"""PDF manipulation tools: merge, split, rotate, compress, numbers, watermark, reorder."""
from __future__ import annotations

import re
from pathlib import Path
import zipfile

import fitz

from app.core.errors import ConversionError, ValidationAppError
from app.services.base import ConversionContext, ConversionResult
from app.services.capabilities import ocr_available
from app.services.ocr import ocr_pdf_page

MAX_PAGES = 2000


def _open_pdf(path: Path) -> fitz.Document:
    try:
        doc = fitz.open(path)
    except Exception as exc:
        raise ConversionError("PDF tidak dapat dibuka. Berkas mungkin rusak atau terenkripsi.") from exc
    if doc.needs_pass:
        doc.close()
        raise ConversionError("PDF terproteksi kata sandi tidak didukung. Gunakan alat 'Buka Proteksi PDF' terlebih dahulu.")
    if doc.page_count == 0:
        doc.close()
        raise ConversionError("PDF tidak memiliki halaman.")
    if doc.page_count > MAX_PAGES:
        doc.close()
        raise ConversionError(f"PDF memiliki lebih dari {MAX_PAGES} halaman dan melebihi batas.")
    return doc


def parse_page_numbers(spec: str | None, total: int) -> list[int]:
    """Parse '1,3-5' into zero-based page indices. Raises on invalid input."""
    spec = (spec or "").strip().lower()
    if spec in ("", "all", "*"):
        return list(range(total))
    if spec in ("first", "pertama"):
        return [0]
    if spec in ("last", "terakhir"):
        return [total - 1]
    pages: list[int] = []
    for part in re.split(r"[,\s]+", spec):
        if not part:
            continue
        if "-" in part:
            match = re.fullmatch(r"(\d+)-(\d+)", part)
            if not match:
                raise ValidationAppError(f"Rentang halaman tidak valid: '{part}'.")
            start, end = int(match.group(1)), int(match.group(2))
            if start > end:
                start, end = end, start
            pages.extend(range(start, end + 1))
        else:
            if not part.isdigit():
                raise ValidationAppError(f"Nomor halaman tidak valid: '{part}'.")
            pages.append(int(part))
    unique: list[int] = []
    for page in pages:
        if page < 1 or page > total:
            raise ValidationAppError(f"Halaman {page} di luar jangkauan (dokumen memiliki {total} halaman).")
        if page - 1 not in unique:
            unique.append(page - 1)
    if not unique:
        raise ValidationAppError("Tidak ada halaman valid yang dipilih.")
    return unique


def _save(doc: fitz.Document, path: Path) -> None:
    doc.save(path, garbage=4, deflate=True)
    doc.close()


def merge_pdfs(ctx: ConversionContext) -> ConversionResult:
    if len(ctx.inputs) < 2:
        raise ValidationAppError("Pilih minimal dua berkas PDF untuk digabung.")
    merged = fitz.open()
    try:
        for path in ctx.inputs:
            doc = _open_pdf(path)
            merged.insert_pdf(doc)
            doc.close()
    except Exception:
        merged.close()
        raise
    output = ctx.workdir / "merged.pdf"
    _save(merged, output)
    return ConversionResult(output, "merged.pdf", "application/pdf")


def split_pdf(ctx: ConversionContext) -> ConversionResult:
    doc = _open_pdf(ctx.input)
    mode = str(ctx.options.get("mode", "each"))
    zip_path = ctx.workdir / "split-pages.zip"
    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
            if mode == "ranges":
                raw = str(ctx.options.get("ranges", "")).strip()
                if not raw:
                    raise ValidationAppError("Masukkan minimal satu rentang halaman, misalnya 1-3, 4-6.")
                groups = []
                for chunk in raw.split(","):
                    chunk = chunk.strip()
                    if chunk:
                        groups.append(parse_page_numbers(chunk, doc.page_count))
                for index, group in enumerate(groups, start=1):
                    part = fitz.open()
                    for page_index in group:
                        part.insert_pdf(doc, from_page=page_index, to_page=page_index)
                    data = part.tobytes(garbage=4, deflate=True)
                    part.close()
                    archive.writestr(f"part-{index:02d}.pdf", data)
            else:
                for page_index in range(doc.page_count):
                    part = fitz.open()
                    part.insert_pdf(doc, from_page=page_index, to_page=page_index)
                    data = part.tobytes(garbage=4, deflate=True)
                    part.close()
                    archive.writestr(f"page-{page_index + 1:03d}.pdf", data)
    finally:
        doc.close()
    return ConversionResult(zip_path, "split-pages.zip", "application/zip")


def rotate_pdf(ctx: ConversionContext) -> ConversionResult:
    doc = _open_pdf(ctx.input)
    angle = int(ctx.options.get("angle", 90) or 90)
    if angle not in (90, 180, 270):
        doc.close()
        raise ValidationAppError("Sudut rotasi harus 90, 180, atau 270 derajat.")
    pages = parse_page_numbers(str(ctx.options.get("pages", "all")), doc.page_count)
    for page_index in pages:
        page = doc[page_index]
        page.set_rotation((page.rotation + angle) % 360)
    output = ctx.workdir / "rotated.pdf"
    _save(doc, output)
    return ConversionResult(output, "rotated.pdf", "application/pdf")


def compress_pdf(ctx: ConversionContext) -> ConversionResult:
    doc = _open_pdf(ctx.input)
    output = ctx.workdir / "compressed.pdf"
    try:
        doc.subset_fonts()
        doc.save(
            output,
            garbage=4,
            deflate=True,
            deflate_images=True,
            deflate_fonts=True,
            clean=True,
        )
    finally:
        doc.close()

    original = ctx.input.stat().st_size
    compressed = output.stat().st_size
    warnings = []
    if compressed >= original:
        warnings.append(
            "PDF ini sudah cukup padat, sehingga salinan yang dioptimalkan tidak lebih kecil. "
            "Gunakan alat 'Optimalkan PDF' untuk mengompresi ulang gambar."
        )
    return ConversionResult(output, "compressed.pdf", "application/pdf", warnings)


def extract_text(ctx: ConversionContext) -> ConversionResult:
    doc = _open_pdf(ctx.input)
    chunks: list[str] = []
    empty_pages: list[int] = []
    try:
        for index, page in enumerate(doc):
            text = page.get_text("text").strip()
            if not text:
                if ocr_available():
                    try:
                        text = ocr_pdf_page(page, str(ctx.options.get("language", "eng")))
                    except Exception:
                        text = ""
                if not text:
                    empty_pages.append(index + 1)
            chunks.append(f"--- Page {index + 1} ---\n{text}")
        total_pages = doc.page_count
    finally:
        doc.close()

    output = ctx.workdir / "extracted-text.txt"
    output.write_text("\n\n".join(chunks), encoding="utf-8")
    warnings = []
    if empty_pages:
        warnings.append(
            "Tidak ada lapisan teks pada halaman "
            + ", ".join(str(page) for page in empty_pages[:20])
            + (". Halaman tersebut mungkin perlu OCR." if not ocr_available() else ".")
        )
    return ConversionResult(output, "extracted-text.txt", "text/plain; charset=utf-8", warnings)


def pdf_to_images(ctx: ConversionContext) -> ConversionResult:
    doc = _open_pdf(ctx.input)
    fmt = str(ctx.options.get("format", "png")).lower()
    if fmt not in ("png", "jpg"):
        fmt = "png"
    dpi = max(72, min(400, int(ctx.options.get("dpi", 150) or 150)))
    zip_path = ctx.workdir / "pages.zip"
    warnings: list[str] = []
    failed_pages: list[int] = []
    rendered = 0
    try:
        if doc.page_count > 300:
            warnings.append("Hanya 300 halaman pertama yang dirender.")
        total_pages = min(doc.page_count, 300)
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for index in range(total_pages):
                page = doc[index]
                try:
                    pixmap = page.get_pixmap(
                        matrix=fitz.Matrix(dpi / 72, dpi / 72), alpha=False
                    )
                    if fmt == "jpg":
                        data = pixmap.tobytes("jpg", jpg_quality=90)
                    else:
                        data = pixmap.tobytes("png")
                    archive.writestr(f"page-{index + 1:03d}.{fmt}", data)
                    rendered += 1
                except Exception:
                    failed_pages.append(index + 1)
    finally:
        doc.close()
    if failed_pages:
        warnings.append(
            "Halaman berikut gagal dirender: "
            + ", ".join(str(number) for number in failed_pages[:20])
            + ("." if len(failed_pages) <= 20 else ", …")
        )
    if rendered == 0:
        raise ConversionError("Tidak ada halaman PDF yang berhasil dirender menjadi gambar.")
    return ConversionResult(zip_path, f"pdf-pages-{fmt}.zip", "application/zip", warnings)


def add_page_numbers(ctx: ConversionContext) -> ConversionResult:
    doc = _open_pdf(ctx.input)
    position = str(ctx.options.get("position", "bottom-center"))
    start_at = max(1, int(ctx.options.get("start_at", 1) or 1))
    template = str(ctx.options.get("format", "{n}") or "{n}")
    total = doc.page_count
    output = ctx.workdir / "numbered.pdf"
    try:
        for index, page in enumerate(doc):
            number = start_at + index
            label = template.replace("{n}", str(number)).replace("{total}", str(total))
            width = page.rect.width
            height = page.rect.height
            font_size = max(8.0, min(14.0, width * 0.022))
            text_width = fitz.get_text_length(label, fontname="helv", fontsize=font_size)
            margin = height * 0.035
            if position == "top-center":
                point = fitz.Point((width - text_width) / 2, margin + font_size)
            elif position == "bottom-right":
                point = fitz.Point(width - margin - text_width, height - margin)
            else:
                point = fitz.Point((width - text_width) / 2, height - margin)
            page.insert_text(point, label, fontname="helv", fontsize=font_size, color=(0.35, 0.35, 0.35))
        doc.save(output, garbage=4, deflate=True)
    finally:
        doc.close()
    return ConversionResult(output, "numbered.pdf", "application/pdf")


def add_watermark(ctx: ConversionContext) -> ConversionResult:
    text = str(ctx.options.get("text") or "CONFIDENTIAL")[:120]
    opacity = max(0.05, min(0.8, float(ctx.options.get("opacity", 15) or 15) / 100))
    font_size = max(12, min(120, int(ctx.options.get("font_size", 48) or 48)))
    doc = _open_pdf(ctx.input)
    output = ctx.workdir / "watermarked.pdf"
    try:
        for page in doc:
            width = page.rect.width
            height = page.rect.height
            text_width = fitz.get_text_length(text, fontname="helv", fontsize=font_size)
            point = fitz.Point((width - text_width) / 2, height / 2)
            try:
                pivot = fitz.Point(width / 2, height / 2)
                matrix = fitz.Matrix(45)
                page.insert_text(
                    point,
                    text,
                    fontname="helv",
                    fontsize=font_size,
                    color=(0.6, 0.6, 0.6),
                    fill_opacity=opacity,
                    morph=(pivot, matrix),
                )
            except Exception:
                page.insert_text(
                    point,
                    text,
                    fontname="helv",
                    fontsize=font_size,
                    color=(0.6, 0.6, 0.6),
                    fill_opacity=opacity,
                )
        doc.save(output, garbage=4, deflate=True)
    finally:
        doc.close()
    return ConversionResult(output, "watermarked.pdf", "application/pdf")


def remove_pages(ctx: ConversionContext) -> ConversionResult:
    doc = _open_pdf(ctx.input)
    try:
        remove = set(parse_page_numbers(str(ctx.options.get("pages", "")), doc.page_count))
        keep = [index for index in range(doc.page_count) if index not in remove]
        if not keep:
            raise ValidationAppError("Tidak dapat menghapus seluruh halaman dokumen.")
        doc.select(keep)
        output = ctx.workdir / "removed-pages.pdf"
        doc.save(output, garbage=4, deflate=True)
    finally:
        doc.close()
    return ConversionResult(output, "removed-pages.pdf", "application/pdf")


def reorder_pdf(ctx: ConversionContext) -> ConversionResult:
    doc = _open_pdf(ctx.input)
    try:
        order = parse_page_numbers(str(ctx.options.get("order", "")), doc.page_count)
        if len(order) != doc.page_count or len(set(order)) != doc.page_count:
            raise ValidationAppError(
                f"Berikan setiap halaman tepat satu kali (dokumen memiliki {doc.page_count} halaman)."
            )
        doc.select(order)
        output = ctx.workdir / "reordered.pdf"
        doc.save(output, garbage=4, deflate=True)
    finally:
        doc.close()
    return ConversionResult(output, "reordered.pdf", "application/pdf")


PAGE_DIMENSIONS: dict[str, tuple[float, float]] = {
    "a4": (595.28, 841.89),
    "letter": (612.0, 792.0),
    "legal": (612.0, 1008.0),
    "a3": (841.89, 1190.55),
    "f4": (595.28, 935.43),
}


def extract_pages(ctx: ConversionContext) -> ConversionResult:
    """Extract the selected pages into a new PDF, preserving the originals."""
    doc = _open_pdf(ctx.input)
    try:
        pages = parse_page_numbers(str(ctx.options.get("pages", "")), doc.page_count)
        part = fitz.open()
        try:
            for index in pages:
                part.insert_pdf(doc, from_page=index, to_page=index)
            output = ctx.workdir / "extracted-pages.pdf"
            part.save(output, garbage=4, deflate=True)
        finally:
            part.close()
    finally:
        doc.close()
    return ConversionResult(output, "extracted-pages.pdf", "application/pdf")


def crop_pages(ctx: ConversionContext) -> ConversionResult:
    """Crop page margins by a percentage of each side."""
    doc = _open_pdf(ctx.input)
    sides = {
        name: max(0.0, min(45.0, float(ctx.options.get(name, 0) or 0))) / 100
        for name in ("top", "right", "bottom", "left")
    }
    if not any(sides.values()):
        doc.close()
        raise ValidationAppError(
            "Atur minimal satu sisi pemotongan (atas, kanan, bawah, atau kiri)."
        )
    pages = parse_page_numbers(str(ctx.options.get("pages", "all")), doc.page_count)
    try:
        for index in pages:
            page = doc[index]
            rect = page.rect
            clip = fitz.Rect(
                rect.x0 + rect.width * sides["left"],
                rect.y0 + rect.height * sides["top"],
                rect.x1 - rect.width * sides["right"],
                rect.y1 - rect.height * sides["bottom"],
            )
            if clip.width < 36 or clip.height < 36:
                raise ValidationAppError(
                    "Pemotongan terlalu besar sehingga halaman menjadi terlalu kecil."
                )
            page.set_cropbox(clip)
        output = ctx.workdir / "cropped.pdf"
        doc.save(output, garbage=4, deflate=True)
    finally:
        doc.close()
    return ConversionResult(
        output,
        "cropped.pdf",
        "application/pdf",
        ["Halaman dipotong menggunakan crop box PDF; konten di luar area potong tidak dihapus."],
    )


def resize_pages(ctx: ConversionContext) -> ConversionResult:
    """Resize every page to a standard paper size without rasterising text."""
    doc = _open_pdf(ctx.input)
    size_key = str(ctx.options.get("page_size", "a4")).lower()
    if size_key not in PAGE_DIMENSIONS:
        doc.close()
        raise ValidationAppError("Ukuran halaman tidak dikenal.")
    orientation = str(ctx.options.get("orientation", "auto"))
    margin_mm = max(0, min(25, int(ctx.options.get("margin_mm", 0) or 0)))
    margin = margin_mm / 25.4 * 72
    output = ctx.workdir / "resized.pdf"
    resized = fitz.open()
    try:
        for page in doc:
            width, height = PAGE_DIMENSIONS[size_key]
            landscape = orientation == "landscape" or (
                orientation == "auto" and page.rect.width > page.rect.height
            )
            if landscape:
                width, height = height, width
            new_page = resized.new_page(width=width, height=height)
            target = fitz.Rect(
                margin,
                margin,
                max(margin + 1, width - margin),
                max(margin + 1, height - margin),
            )
            new_page.show_pdf_page(
                target, doc, page.number, keep_proportion=True
            )
        resized.save(output, garbage=4, deflate=True)
    finally:
        resized.close()
        doc.close()
    return ConversionResult(
        output,
        "resized.pdf",
        "application/pdf",
        [
            "Konten halaman diskalakan secara vektor agar muat pada ukuran kertas baru; "
            "teks tetap dapat dipilih dan dicari."
        ],
    )


def optimize_pdf(ctx: ConversionContext) -> ConversionResult:
    """Aggressive optimisation: downsample images, subset fonts, clean objects."""
    doc = _open_pdf(ctx.input)
    output = ctx.workdir / "optimized.pdf"
    quality = max(40, min(95, int(ctx.options.get("quality", 75) or 75)))
    dpi_target = max(72, min(300, int(ctx.options.get("dpi", 150) or 150)))
    warnings: list[str] = []
    try:
        if hasattr(doc, "rewrite_images"):
            try:
                doc.rewrite_images(
                    dpi_target=dpi_target,
                    quality=quality,
                    lossy=True,
                    lossless=True,
                )
            except Exception:
                warnings.append(
                    "Sebagian gambar tidak dapat dikompresi ulang; optimasi struktur tetap dilakukan."
                )
        doc.subset_fonts()
        doc.save(output, garbage=4, deflate=True, clean=True)
    finally:
        doc.close()

    original = ctx.input.stat().st_size
    optimized = output.stat().st_size
    if optimized >= original:
        import shutil

        shutil.copyfile(ctx.input, output)
        warnings.append(
            "Tidak ada penghematan ukuran yang aman untuk berkas ini, jadi PDF asli "
            "dikembalikan tanpa perubahan."
        )
    else:
        saved = (original - optimized) / original * 100
        warnings.append(
            f"Ukuran berkurang {saved:.1f}% ({original} → {optimized} byte). "
            f"Gambar diturunkan ke sekitar {dpi_target} DPI dengan kualitas {quality}."
        )
    return ConversionResult(output, "optimized.pdf", "application/pdf", warnings)


def repair_pdf(ctx: ConversionContext) -> ConversionResult:
    """Attempt to repair a damaged PDF by rebuilding its file structure."""
    try:
        doc = fitz.open(ctx.input)
    except Exception as exc:
        raise ConversionError(
            "PDF tidak dapat dibuka sama sekali sehingga perbaikan tidak mungkin dilakukan."
        ) from exc
    if doc.needs_pass:
        doc.close()
        raise ValidationAppError(
            "PDF terproteksi kata sandi. Gunakan alat 'Buka Proteksi PDF' terlebih dahulu."
        )
    output = ctx.workdir / "repaired.pdf"
    warnings: list[str] = []
    try:
        try:
            doc.save(output, garbage=4, deflate=True, clean=True)
        except Exception:
            doc.save(output, garbage=4, deflate=True)
    finally:
        doc.close()

    try:
        check = fitz.open(output)
        pages = check.page_count
        check.close()
    except Exception as exc:
        raise ConversionError("PDF tidak dapat diperbaiki.") from exc
    warnings.append(
        f"Struktur PDF dibangun ulang dan diverifikasi: {pages} halaman dapat dibuka. "
        "Objek rusak yang tidak dapat dipulihkan akan hilang."
    )
    return ConversionResult(output, "repaired.pdf", "application/pdf", warnings)


def protect_pdf(ctx: ConversionContext) -> ConversionResult:
    """Add password protection with AES-256 encryption."""
    password = str(ctx.options.get("password") or "")
    if len(password) < 4:
        raise ValidationAppError("Kata sandi minimal 4 karakter.")
    doc = _open_pdf(ctx.input)
    permissions = fitz.PDF_PERM_ACCESSIBILITY
    if bool(ctx.options.get("allow_printing", True)):
        permissions |= fitz.PDF_PERM_PRINT | fitz.PDF_PERM_PRINT_HQ
    if bool(ctx.options.get("allow_copying", False)):
        permissions |= fitz.PDF_PERM_COPY
    output = ctx.workdir / "protected.pdf"
    try:
        doc.save(
            output,
            garbage=4,
            deflate=True,
            encryption=fitz.PDF_ENCRYPT_AES_256,
            owner_pw=password,
            user_pw=password,
            permissions=permissions,
        )
    finally:
        doc.close()
    return ConversionResult(
        output,
        "protected.pdf",
        "application/pdf",
        [
            "PDF dienkripsi AES-256 dan memerlukan kata sandi untuk dibuka. "
            "Simpan kata sandi di tempat aman; kata sandi tidak dapat dipulihkan."
        ],
    )


def unlock_pdf(ctx: ConversionContext) -> ConversionResult:
    """Remove password protection from a PDF when the correct password is given."""
    password = str(ctx.options.get("password") or "")
    try:
        doc = fitz.open(ctx.input)
    except Exception as exc:
        raise ConversionError(
            "PDF tidak dapat dibuka. Berkas mungkin rusak."
        ) from exc
    try:
        if doc.needs_pass:
            if not password:
                raise ValidationAppError(
                    "PDF ini terproteksi. Masukkan kata sandi yang benar."
                )
            if not doc.authenticate(password):
                raise ValidationAppError(
                    "Kata sandi salah. PDF tidak dapat dibuka."
                )
        output = ctx.workdir / "unlocked.pdf"
        doc.save(
            output,
            garbage=4,
            deflate=True,
            encryption=fitz.PDF_ENCRYPT_NONE,
        )
    finally:
        doc.close()
    return ConversionResult(
        output,
        "unlocked.pdf",
        "application/pdf",
        ["Proteksi kata sandi dihapus. Berkas hasil tidak lagi terenkripsi."],
    )


_SIGNATURE_POSITIONS = {
    "bottom-right",
    "bottom-left",
    "bottom-center",
    "top-right",
    "top-left",
    "center",
}


def add_signature(ctx: ConversionContext) -> ConversionResult:
    """Place a visual signature image on one or more pages.

    This inserts a raster/vector image only. It is NOT a certificate-based
    digital signature and does not provide cryptographic verification.
    """
    import io

    from PIL import Image

    pdf_inputs = [path for path in ctx.inputs if path.suffix.lower() == ".pdf"]
    image_inputs = [
        path
        for path in ctx.inputs
        if path.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp", ".bmp")
    ]
    if len(pdf_inputs) != 1 or len(image_inputs) != 1:
        raise ValidationAppError(
            "Pilih tepat satu PDF dan satu gambar tanda tangan (PNG/JPG)."
        )

    doc = _open_pdf(pdf_inputs[0])
    try:
        data = image_inputs[0].read_bytes()
        if bool(ctx.options.get("remove_background", True)):
            try:
                with Image.open(io.BytesIO(data)) as opened:
                    image = opened.convert("RGBA")
                    pixels = [
                        (r, g, b, 0) if r > 235 and g > 235 and b > 235 else (r, g, b, a)
                        for r, g, b, a in image.getdata()
                    ]
                    image.putdata(pixels)
                    buffer = io.BytesIO()
                    image.save(buffer, format="PNG")
                    data = buffer.getvalue()
            except Exception:
                pass

        with Image.open(io.BytesIO(data)) as opened:
            image_width, image_height = opened.size
        if image_width <= 0 or image_height <= 0:
            raise ValidationAppError("Gambar tanda tangan tidak valid.")

        pages = parse_page_numbers(
            str(ctx.options.get("pages", "last")), doc.page_count
        )
        position = str(ctx.options.get("position", "bottom-right"))
        if position not in _SIGNATURE_POSITIONS:
            position = "bottom-right"
        width_percent = max(5.0, min(60.0, float(ctx.options.get("width", 25) or 25)))
        margin = 36.0

        for index in pages:
            page = doc[index]
            rect = page.rect
            target_width = rect.width * width_percent / 100
            target_height = target_width * image_height / image_width
            if target_height > rect.height * 0.5:
                target_height = rect.height * 0.5
                target_width = target_height * image_width / image_height
            if position == "bottom-right":
                x, y = rect.x1 - margin - target_width, rect.y1 - margin - target_height
            elif position == "bottom-left":
                x, y = rect.x0 + margin, rect.y1 - margin - target_height
            elif position == "bottom-center":
                x, y = (rect.width - target_width) / 2, rect.y1 - margin - target_height
            elif position == "top-right":
                x, y = rect.x1 - margin - target_width, rect.y0 + margin
            elif position == "top-left":
                x, y = rect.x0 + margin, rect.y0 + margin
            else:
                x, y = (rect.width - target_width) / 2, (rect.height - target_height) / 2
            page.insert_image(
                fitz.Rect(x, y, x + target_width, y + target_height),
                stream=data,
                keep_proportion=True,
                overlay=True,
            )
        output = ctx.workdir / "signed.pdf"
        doc.save(output, garbage=4, deflate=True)
    finally:
        doc.close()

    return ConversionResult(
        output,
        "signed.pdf",
        "application/pdf",
        [
            "Tanda tangan ini adalah gambar visual, bukan tanda tangan digital "
            "bersertifikat. Tanda tangan visual tidak dapat diverifikasi secara "
            "kriptografis dan tidak membuktikan keaslian dokumen."
        ],
    )


def compare_pdfs(ctx: ConversionContext) -> ConversionResult:
    """Compare two PDFs page by page and produce a visual difference report."""
    import io

    from PIL import Image, ImageChops

    pdf_inputs = [path for path in ctx.inputs if path.suffix.lower() == ".pdf"]
    if len(pdf_inputs) != 2:
        raise ValidationAppError("Pilih tepat dua berkas PDF untuk dibandingkan.")
    first = _open_pdf(pdf_inputs[0])
    second = _open_pdf(pdf_inputs[1])
    warnings: list[str] = []
    render_dpi = 96
    comparisons: list[dict] = []
    changed_pages: list[int] = []
    only_first: list[int] = []
    only_second: list[int] = []
    try:
        compared = min(first.page_count, second.page_count)
        if first.page_count > compared:
            only_first = list(range(compared + 1, first.page_count + 1))
        if second.page_count > compared:
            only_second = list(range(compared + 1, second.page_count + 1))
        for index in range(compared):
            pix_a = first[index].get_pixmap(dpi=render_dpi, colorspace=fitz.csGRAY, alpha=False)
            pix_b = second[index].get_pixmap(dpi=render_dpi, colorspace=fitz.csGRAY, alpha=False)
            image_a = Image.frombytes("L", (pix_a.width, pix_a.height), pix_a.samples)
            image_b = Image.frombytes("L", (pix_b.width, pix_b.height), pix_b.samples)
            if image_b.size != image_a.size:
                image_b = image_b.resize(image_a.size, Image.LANCZOS)
            difference = ImageChops.difference(image_a, image_b)
            mask = difference.point(lambda value: 255 if value > 32 else 0)
            total = image_a.width * image_a.height
            changed_pixels = sum(1 for value in mask.getdata() if value)
            ratio = changed_pixels / total if total else 0.0
            if ratio >= 0.001:
                changed_pages.append(index + 1)
            color_a = Image.frombytes(
                "RGB",
                (pix_a.width, pix_a.height),
                first[index].get_pixmap(dpi=render_dpi, alpha=False).samples,
            )
            highlight = Image.new("RGB", color_a.size, (255, 40, 40))
            color_a.paste(highlight, mask=mask)
            buffer = io.BytesIO()
            color_a.save(buffer, format="PNG", optimize=True)
            comparisons.append(
                {
                    "page": index + 1,
                    "ratio": ratio,
                    "image": buffer.getvalue(),
                    "width": color_a.width,
                    "height": color_a.height,
                }
            )
    finally:
        first.close()
        second.close()

    report = fitz.open()
    try:
        summary = report.new_page(width=595, height=842)
        cursor = 60
        summary.insert_text((40, cursor), "Laporan Perbandingan PDF", fontsize=18, fontname="hebo")
        cursor += 30
        first_name = ctx.original_names[0] if ctx.original_names else pdf_inputs[0].name
        second_name = (
            ctx.original_names[1]
            if len(ctx.original_names) > 1
            else pdf_inputs[1].name
        )
        lines = [
            f"Dokumen A: {first_name}",
            f"Dokumen B: {second_name}",
            f"Halaman dibandingkan: {len(comparisons)}",
            f"Halaman berbeda: {len(changed_pages)}"
            + (f" (halaman {', '.join(map(str, changed_pages[:40]))})" if changed_pages else ""),
        ]
        if only_first:
            lines.append(
                "Halaman hanya ada di dokumen A: "
                + ", ".join(map(str, only_first[:40]))
            )
        if only_second:
            lines.append(
                "Halaman hanya ada di dokumen B: "
                + ", ".join(map(str, only_second[:40]))
            )
        lines.append(
            "Perbandingan dilakukan secara visual pada hasil render halaman, bukan "
            "perbandingan semantik teks. Area yang berubah ditandai merah."
        )
        for line in lines:
            for chunk in _wrap_text(line, 78):
                summary.insert_text((40, cursor), chunk, fontsize=10, fontname="helv")
                cursor += 15
        cursor += 10
        summary.insert_text(
            (40, cursor),
            "Ringkasan per halaman:",
            fontsize=11,
            fontname="hebo",
        )
        cursor += 18
        for item in comparisons:
            label = (
                f"Halaman {item['page']}: "
                + ("berbeda" if item["ratio"] >= 0.001 else "sama")
                + f" ({item['ratio'] * 100:.2f}% piksel berbeda)"
            )
            summary.insert_text((40, cursor), label, fontsize=9, fontname="helv")
            cursor += 14
            if cursor > 810:
                summary = report.new_page(width=595, height=842)
                cursor = 60

        for item in comparisons:
            page = report.new_page(width=595, height=842)
            page.insert_text(
                (40, 50),
                f"Halaman {item['page']} — selisih visual",
                fontsize=14,
                fontname="hebo",
            )
            page.insert_text(
                (40, 70),
                f"{item['ratio'] * 100:.2f}% piksel berbeda (ambang 0,1%)",
                fontsize=9,
                fontname="helv",
            )
            max_width, max_height = 515, 720
            scale = min(max_width / item["width"], max_height / item["height"])
            width = item["width"] * scale
            height = item["height"] * scale
            page.insert_image(
                fitz.Rect(40, 90, 40 + width, 90 + height),
                stream=item["image"],
            )

        output = ctx.workdir / "comparison-report.pdf"
        report.save(output, garbage=4, deflate=True)
    finally:
        report.close()

    if not changed_pages and first.page_count == second.page_count:
        warnings.append("Kedua dokumen tampak identik secara visual pada semua halaman.")
    else:
        warnings.append(
            f"{len(changed_pages)} dari {len(comparisons)} halaman yang dibandingkan "
            "memiliki perbedaan visual. Buka laporan untuk melihat area yang ditandai."
        )
    return ConversionResult(
        output, "comparison-report.pdf", "application/pdf", warnings
    )


def _wrap_text(text: str, width: int) -> list[str]:
    """Wrap a line for the fixed-width Helvetica report text."""
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) > width and current:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines or [""]
