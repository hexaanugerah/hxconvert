"""Image tools: format conversion, image-to-PDF and OCR."""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageOps

from app.core.errors import ConversionError, ValidationAppError
from app.services.base import ConversionContext, ConversionResult
from app.services.ocr import extract_text_from_image

MIME = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
}

PAGE_SIZES = {
    "a4": (1240, 1754),      # 150 DPI
    "letter": (1275, 1650),  # 150 DPI
    "legal": (1275, 2100),   # 8.5 x 14 in at 150 DPI
    "a3": (1754, 2480),      # 11.69 x 16.54 in at 150 DPI
    "f4": (1240, 1950),      # 8.27 x 13 in at 150 DPI
}
PAGE_DPI = 150
IMAGE_PAGE_SIZES = {"auto", "image"}
FIT_MODES = {"fit", "fill", "original"}


def _load(path: Path) -> Image.Image:
    try:
        image = Image.open(path)
        image.load()
    except Exception as exc:
        raise ConversionError("Gambar tidak dapat dibaca. Berkas mungkin rusak atau formatnya tidak didukung.") from exc
    return ImageOps.exif_transpose(image)


def _flatten(image: Image.Image, background: tuple[int, int, int] = (255, 255, 255)) -> Image.Image:
    if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        canvas = Image.new("RGB", rgba.size, background)
        canvas.paste(rgba, mask=rgba.split()[-1])
        return canvas
    return image.convert("RGB")


def _resize(image: Image.Image, scale: float) -> Image.Image:
    if abs(scale - 1.0) < 0.002:
        return image
    size = (max(1, int(round(image.width * scale))), max(1, int(round(image.height * scale))))
    return image.resize(size, Image.LANCZOS)


def _compose_on_page(
    image: Image.Image,
    *,
    page_size: str,
    orientation: str,
    fit: str,
) -> tuple[Image.Image, bool]:
    """Place one image on one page without stretching it. Returns (page, cropped)."""
    image = _flatten(image)
    if page_size in IMAGE_PAGE_SIZES:
        # Page follows the image: exact aspect ratio, no resampling.
        return image, False

    width, height = PAGE_SIZES.get(page_size, PAGE_SIZES["a4"])
    is_landscape = orientation == "landscape" or (
        orientation == "auto" and image.width > image.height
    )
    if is_landscape:
        width, height = height, width

    if fit == "fill":
        # Cover the page proportionally, then crop the overflow from the center.
        scale = max(width / image.width, height / image.height)
        resized = _resize(image, scale)
        left = max(0, (resized.width - width) // 2)
        top = max(0, (resized.height - height) // 2)
        page = resized.crop((left, top, left + width, top + height))
        return page, True

    page = Image.new("RGB", (width, height), (255, 255, 255))
    margin = int(min(width, height) * 0.04)
    available_w = max(1, width - 2 * margin)
    available_h = max(1, height - 2 * margin)
    if fit == "original":
        # Keep the image at its pixel size (at PAGE_DPI); only shrink if it would not fit.
        scale = min(1.0, available_w / image.width, available_h / image.height)
    else:
        scale = min(available_w / image.width, available_h / image.height)
    resized = _resize(image, scale)
    page.paste(resized, ((width - resized.width) // 2, (height - resized.height) // 2))
    return page, False


def images_to_pdf(ctx: ConversionContext) -> ConversionResult:
    page_size = str(ctx.options.get("page_size", "a4")).lower()
    orientation = str(ctx.options.get("orientation", "auto")).lower()
    fit = str(ctx.options.get("fit", "fit")).lower()
    if fit not in FIT_MODES:
        fit = "fit"
    if orientation not in ("auto", "portrait", "landscape"):
        orientation = "auto"

    pages: list[Image.Image] = []
    warnings: list[str] = []
    skipped: list[str] = []
    cropped = False

    # ctx.inputs already follows the upload order recorded by the worker.
    for index, path in enumerate(ctx.inputs):
        display_name = (
            ctx.original_names[index]
            if index < len(ctx.original_names)
            else path.name
        )
        try:
            image = _load(path)
        except ConversionError:
            skipped.append(display_name)
            continue
        page, was_cropped = _compose_on_page(
            image, page_size=page_size, orientation=orientation, fit=fit
        )
        cropped = cropped or was_cropped
        pages.append(page)

    if skipped:
        names = ", ".join(skipped[:10]) + ("…" if len(skipped) > 10 else "")
        warnings.append(
            f"{len(skipped)} gambar tidak dapat dibaca dan dilewati: {names}."
        )
    if not pages:
        raise ValidationAppError(
            "Tidak ada gambar yang dapat dibaca. "
            + ("Semua gambar yang dipilih gagal dibuka." if skipped else "")
        )
    if cropped:
        warnings.append(
            "Mode 'isi halaman' memotong tepi gambar agar menutupi seluruh halaman. "
            "Gunakan mode 'sesuaikan halaman' jika seluruh gambar harus terlihat."
        )

    output = ctx.workdir / "images.pdf"
    try:
        pages[0].save(
            output,
            format="PDF",
            save_all=True,
            append_images=pages[1:],
            resolution=PAGE_DPI,
        )
    except Exception as exc:
        raise ConversionError("Failed to build the PDF from the images.") from exc
    return ConversionResult(output, "images.pdf", "application/pdf", warnings)


def convert_image(ctx: ConversionContext) -> ConversionResult:
    target = str(ctx.options.get("target", "png")).lower()
    if target not in ("png", "jpg", "webp"):
        raise ValidationAppError("Target format must be PNG, JPG or WebP.")
    quality = max(40, min(100, int(ctx.options.get("quality", 88) or 88)))
    image = _load(ctx.input)

    output = ctx.workdir / f"image.{target}"
    warnings: list[str] = []
    if target == "jpg":
        image = _flatten(image)
        image.save(output, format="JPEG", quality=quality, optimize=True, progressive=True)
        if ctx.input.suffix.lower() in (".png", ".webp", ".gif") and _has_alpha(ctx.input):
            warnings.append("Transparent areas were filled with white for JPG output.")
    elif target == "webp":
        has_alpha = _has_alpha(ctx.input)
        image = image.convert("RGBA") if has_alpha else image.convert("RGB")
        image.save(output, format="WEBP", quality=quality, method=4)
    else:
        image.save(output, format="PNG", optimize=True)

    stem = Path(ctx.original_name).stem or "image"
    return ConversionResult(output, f"{stem}.{target}", MIME[target], warnings)


def _has_alpha(path: Path) -> bool:
    try:
        with Image.open(path) as image:
            return image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info)
    except Exception:
        return False


def _original_format(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in (".jpg", ".jpeg"):
        return "jpg"
    if suffix == ".webp":
        return "webp"
    if suffix == ".png":
        return "png"
    return "png"


def _save_with_format(
    image: Image.Image, output: Path, fmt: str, quality: int
) -> None:
    if fmt == "jpg":
        _flatten(image).save(
            output, format="JPEG", quality=quality, optimize=True, progressive=True
        )
    elif fmt == "webp":
        has_alpha = image.mode in ("RGBA", "LA") or (
            image.mode == "P" and "transparency" in image.info
        )
        prepared = image.convert("RGBA") if has_alpha else image.convert("RGB")
        prepared.save(output, format="WEBP", quality=quality, method=6)
    else:
        if image.mode == "P":
            image = image.convert("RGBA")
        image.save(output, format="PNG", optimize=True, compress_level=9)


def compress_image(ctx: ConversionContext) -> ConversionResult:
    """Re-encode an image with a lower quality / stronger compression."""
    quality = max(30, min(100, int(ctx.options.get("quality", 75) or 75)))
    fmt = str(ctx.options.get("format", "auto") or "auto").lower()
    if fmt == "auto":
        fmt = _original_format(ctx.input)
    if fmt not in ("png", "jpg", "webp"):
        fmt = "png"
    image = _load(ctx.input)
    output = ctx.workdir / f"compressed.{fmt}"
    _save_with_format(image, output, fmt, quality)

    original = ctx.input.stat().st_size
    compressed = output.stat().st_size
    warnings: list[str] = []
    if compressed >= original:
        # No gain: return the original bytes, but only under the original
        # extension. Keeping a copy of a PNG under a .jpg name would produce
        # a file the browser cannot decode.
        fmt = _original_format(ctx.input)
        output = ctx.workdir / f"compressed.{fmt}"
        import shutil

        shutil.copyfile(ctx.input, output)
        warnings.append(
            "Gambar asli sudah optimal; berkas asli dikembalikan tanpa perubahan. "
            "Coba kualitas lebih rendah atau format WebP untuk hasil lebih kecil."
        )
    else:
        saved = (original - compressed) / original * 100
        warnings.append(
            f"Ukuran berkurang {saved:.1f}% ({original} → {compressed} byte). "
            f"Kualitas {quality}{' (lossless)' if fmt == 'png' else ''}."
        )
    stem = Path(ctx.original_name).stem or "image"
    return ConversionResult(output, f"{stem}.{fmt}", MIME[fmt], warnings)


def resize_image(ctx: ConversionContext) -> ConversionResult:
    """Resize by percentage or exact pixel dimensions, preserving aspect ratio."""
    image = _load(ctx.input)
    mode = str(ctx.options.get("mode", "percent")).lower()
    keep_aspect = bool(ctx.options.get("keep_aspect", True))
    if mode == "pixels":
        width = max(0, min(12000, int(ctx.options.get("width", 0) or 0)))
        height = max(0, min(12000, int(ctx.options.get("height", 0) or 0)))
        if not width and not height:
            raise ValidationAppError(
                "Masukkan lebar atau tinggi dalam piksel."
            )
        if width and height and not keep_aspect:
            new_size = (width, height)
        elif width and height:
            scale = min(width / image.width, height / image.height)
            new_size = (
                max(1, round(image.width * scale)),
                max(1, round(image.height * scale)),
            )
        elif width:
            scale = width / image.width
            new_size = (width, max(1, round(image.height * scale)))
        else:
            scale = height / image.height
            new_size = (max(1, round(image.width * scale)), height)
    else:
        percent = max(1, min(400, int(ctx.options.get("percent", 100) or 100)))
        if percent == 100:
            raise ValidationAppError(
                "Persentase 100% tidak mengubah ukuran. Pilih nilai lain."
            )
        new_size = (
            max(1, round(image.width * percent / 100)),
            max(1, round(image.height * percent / 100)),
        )

    resized = image.resize(new_size, Image.LANCZOS)
    fmt = _original_format(ctx.input)
    output = ctx.workdir / f"resized.{fmt}"
    _save_with_format(resized, output, fmt, 92)
    stem = Path(ctx.original_name).stem or "image"
    return ConversionResult(
        output,
        f"{stem}.{fmt}",
        MIME[fmt],
        [
            f"Ukuran diubah dari {image.width}×{image.height} menjadi "
            f"{resized.width}×{resized.height} piksel."
        ],
    )


def rotate_image(ctx: ConversionContext) -> ConversionResult:
    """Rotate an image clockwise by 90, 180 or 270 degrees."""
    angle = int(ctx.options.get("angle", 90) or 90)
    if angle not in (90, 180, 270):
        raise ValidationAppError("Sudut rotasi harus 90, 180, atau 270 derajat.")
    image = _load(ctx.input)
    rotated = image.rotate(-angle, expand=True)
    fmt = _original_format(ctx.input)
    output = ctx.workdir / f"rotated.{fmt}"
    _save_with_format(rotated, output, fmt, 92)
    stem = Path(ctx.original_name).stem or "image"
    return ConversionResult(
        output,
        f"{stem}.{fmt}",
        MIME[fmt],
        [f"Gambar diputar {angle}° searah jarum jam."],
    )


def image_to_text(ctx: ConversionContext) -> ConversionResult:
    try:
        image = _load(ctx.input)
    except ConversionError:
        raise

    rotate = int(ctx.options.get("rotate", 0) or 0)
    if rotate in (90, 180, 270):
        image = image.rotate(-rotate, expand=True)
    if bool(ctx.options.get("grayscale", True)):
        image = ImageOps.autocontrast(image.convert("L"))

    text = extract_text_from_image(image, language=str(ctx.options.get("language", "eng")))
    if not text:
        raise ConversionError(
            "Tidak ada teks yang dapat dibaca dari gambar. Gunakan pindaian yang lebih jelas atau putar gambar sebelum OCR."
        )
    output = ctx.workdir / "ocr-text.txt"
    output.write_text(text, encoding="utf-8")
    return ConversionResult(output, "ocr-text.txt", "text/plain; charset=utf-8")
