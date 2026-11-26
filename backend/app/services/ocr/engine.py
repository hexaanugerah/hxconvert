"""OCR extraction. Uses Tesseract when installed, otherwise RapidOCR (ONNX)."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from PIL import Image

from app.core.errors import ConversionError
from app.services.capabilities import get_ocr_engine


def _normalize_lang(language: str) -> str:
    language = (language or "eng").strip().lower()
    aliases = {
        "id": "ind",
        "in": "ind",
        "indonesian": "ind",
        "english": "eng",
        "en": "eng",
        "eng+ind": "eng+ind",
    }
    return aliases.get(language, language or "eng")


@lru_cache(maxsize=1)
def _rapidocr_instance():
    from rapidocr_onnxruntime import RapidOCR

    return RapidOCR()


def _resolve_tesseract_lang(language: str) -> str:
    engine = get_ocr_engine()
    requested = _normalize_lang(language)
    if engine and engine.languages:
        parts = [part for part in requested.split("+") if part in engine.languages]
        if not parts:
            parts = ["eng"] if "eng" in engine.languages else [engine.languages[0]]
        requested = "+".join(parts)
    return requested


def _extract_tesseract(image: Image.Image, language: str) -> str:
    import pytesseract
    from pytesseract import TesseractError

    requested = _resolve_tesseract_lang(language)
    try:
        return pytesseract.image_to_string(image, lang=requested)
    except TesseractError:
        # Requested language data may be missing; fall back to English.
        try:
            return pytesseract.image_to_string(image, lang="eng")
        except Exception as exc:  # pragma: no cover - environment specific
            raise ConversionError(f"OCR failed: {exc}") from exc


def _extract_rapidocr(image: Image.Image) -> str:
    import numpy as np

    engine = _rapidocr_instance()
    result, _elapsed = engine(np.array(image.convert("RGB")))
    if not result:
        return ""
    # RapidOCR returns entries shaped like [box, text, score]
    lines = [str(entry[1]) for entry in result if len(entry) > 1]
    return "\n".join(lines)


def _load_image(source: Path | Image.Image) -> Image.Image:
    if isinstance(source, Image.Image):
        return source
    try:
        image = Image.open(source)
        image.load()
        return image
    except Exception as exc:
        raise ConversionError("The image could not be read for OCR.") from exc


def _prepare_image(image: Image.Image) -> tuple[Image.Image, float]:
    """Convert to a supported mode and upscale small scans.

    Returns the processed image and the upscale factor, which callers need to
    map OCR pixel boxes back to page coordinates.
    """
    if image.mode not in ("RGB", "L"):
        image = image.convert("RGB")
    upscale = 1.0
    if max(image.size) < 1000:
        upscale = 1000 / max(image.size)
        image = image.resize((int(image.width * upscale), int(image.height * upscale)), Image.LANCZOS)
    return image, upscale


def extract_text_from_image(source: Path | Image.Image, language: str = "eng") -> str:
    engine = get_ocr_engine()
    if engine is None:
        raise ConversionError(
            "OCR is not available on this server. Install Tesseract OCR (and its language data) "
            "or the rapidocr-onnxruntime package."
        )

    image, _upscale = _prepare_image(_load_image(source))
    if engine.name == "tesseract":
        text = _extract_tesseract(image, language)
    else:
        text = _extract_rapidocr(image)
    return text.strip()


def _lines_tesseract(image: Image.Image, language: str) -> list[dict]:
    """OCR text lines with their pixel bounding boxes."""
    import pytesseract
    from pytesseract import Output, TesseractError

    requested = _resolve_tesseract_lang(language)
    try:
        data = pytesseract.image_to_data(image, lang=requested, output_type=Output.DICT)
    except TesseractError:
        data = pytesseract.image_to_data(image, lang="eng", output_type=Output.DICT)

    grouped: dict[tuple[int, int, int], dict] = {}
    for index, raw in enumerate(data.get("text", [])):
        word = str(raw or "").strip()
        if not word:
            continue
        try:
            confidence = float(data["conf"][index])
        except (KeyError, TypeError, ValueError):
            confidence = 0.0
        # Tesseract marks non-word layout entries with a negative confidence.
        if confidence < 0:
            continue
        key = (
            int(data["block_num"][index]),
            int(data["par_num"][index]),
            int(data["line_num"][index]),
        )
        left = float(data["left"][index])
        top = float(data["top"][index])
        width = float(data["width"][index])
        height = float(data["height"][index])
        line = grouped.get(key)
        if line is None:
            grouped[key] = {
                "text": word,
                "x0": left,
                "y0": top,
                "x1": left + width,
                "y1": top + height,
            }
        else:
            line["text"] = f"{line['text']} {word}"
            line["x0"] = min(line["x0"], left)
            line["y0"] = min(line["y0"], top)
            line["x1"] = max(line["x1"], left + width)
            line["y1"] = max(line["y1"], top + height)
    return list(grouped.values())


def _lines_rapidocr(image: Image.Image) -> list[dict]:
    """RapidOCR already returns one box per text line."""
    import numpy as np

    engine = _rapidocr_instance()
    result, _elapsed = engine(np.array(image.convert("RGB")))
    lines: list[dict] = []
    for entry in result or []:
        if len(entry) < 2:
            continue
        text = str(entry[1] or "").strip()
        box = entry[0]
        if not text or not box:
            continue
        try:
            xs = [float(point[0]) for point in box]
            ys = [float(point[1]) for point in box]
        except (TypeError, ValueError, IndexError):
            continue
        lines.append(
            {
                "text": text,
                "x0": min(xs),
                "y0": min(ys),
                "x1": max(xs),
                "y1": max(ys),
            }
        )
    return lines


def extract_text_lines_from_image(
    source: Path | Image.Image, language: str = "eng"
) -> tuple[list[dict], float]:
    """Return OCR lines with pixel boxes plus the upscale factor that was applied."""
    engine = get_ocr_engine()
    if engine is None:
        raise ConversionError(
            "OCR is not available on this server. Install Tesseract OCR (and its language data) "
            "or the rapidocr-onnxruntime package."
        )
    image, upscale = _prepare_image(_load_image(source))
    if engine.name == "tesseract":
        lines = _lines_tesseract(image, language)
    else:
        lines = _lines_rapidocr(image)
    return lines, upscale


def ocr_pdf_page(page, language: str = "eng", dpi: int = 220) -> str:
    """Render a PyMuPDF page to an image and OCR it."""
    import fitz

    matrix = fitz.Matrix(dpi / 72, dpi / 72)
    pixmap = page.get_pixmap(matrix=matrix, alpha=False)
    image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
    return extract_text_from_image(image, language=language)


def ocr_pdf_page_lines(
    page, language: str = "eng", dpi: int = 220
) -> list[dict]:
    """OCR a page and return text lines with rectangles in PDF points."""
    import fitz

    matrix = fitz.Matrix(dpi / 72, dpi / 72)
    pixmap = page.get_pixmap(matrix=matrix, alpha=False)
    image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
    lines, upscale = extract_text_lines_from_image(image, language=language)
    # Rendered pixels per point, including the extra upscale factor applied by
    # the OCR helper for low-resolution scans.
    factor = 72.0 / (dpi * upscale)
    page_rect = page.rect
    result: list[dict] = []
    for line in lines:
        x0 = page_rect.x0 + float(line["x0"]) * factor
        y0 = page_rect.y0 + float(line["y0"]) * factor
        x1 = page_rect.x0 + float(line["x1"]) * factor
        y1 = page_rect.y0 + float(line["y1"]) * factor
        x0 = max(page_rect.x0, min(x0, page_rect.x1))
        y0 = max(page_rect.y0, min(y0, page_rect.y1))
        x1 = max(page_rect.x0, min(x1, page_rect.x1))
        y1 = max(page_rect.y0, min(y1, page_rect.y1))
        result.append(
            {
                "text": str(line["text"]),
                "x0": x0,
                "y0": y0,
                "x1": x1,
                "y1": y1,
            }
        )
    return result
