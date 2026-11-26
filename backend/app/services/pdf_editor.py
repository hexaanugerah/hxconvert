"""Edit an existing PDF in place: add text, images, shapes, and replace text.

The editor sends a JSON document in the ``edits`` option:

```
{
  "items": [
    {"type": "text", "page": 1, "x": 72, "y": 100, "text": "Hello",
     "size": 14, "color": "#111111", "font": "helv", "bold": false, "italic": false},
    {"type": "image", "page": 1, "x": 72, "y": 140, "w": 120, "h": 80, "asset": 0},
    {"type": "rect", "page": 1, "x": 72, "y": 240, "w": 180, "h": 60,
     "stroke": "#e11d48", "fill": null, "stroke_width": 2},
    {"type": "ellipse", "page": 1, "x": 72, "y": 320, "w": 120, "h": 60,
     "stroke": "#2563eb", "fill": "#dbeafe", "stroke_width": 2},
    {"type": "line", "page": 1, "x": 72, "y": 400, "x2": 300, "y2": 400,
     "stroke": "#111111", "stroke_width": 1},
    {"type": "text-edit", "page": 1, "x": 72, "y": 60, "w": 140, "h": 24,
     "text": "Edited text", "size": 14, "color": "#111111", "font": "helv"}
  ],
  "replaces": [
    {"page": 1, "search": "Old text", "text": "New text", "size": 12,
     "color": "#111111", "font": "helv"}
  ]
}
```

All coordinates are PDF points with the origin at the top-left corner of the
page, which is exactly what the browser editor measures. Image items reference
the uploaded images by their position in the upload list after the PDF (0 is
the first image).

Text replacement works on the PDF text layer. When a target page has no text
layer (a scan or photo), the page is OCR'd automatically before the search so
replacement can still work on the recognisable text.
"""
from __future__ import annotations

import json
import re

import fitz

from app.core.errors import ValidationAppError
from app.services.base import ConversionContext, ConversionResult
from app.services.capabilities import ocr_available
from app.services.ocr import ocr_pdf_page_lines

MAX_ITEMS = 500
MAX_REPLACEMENT_HITS = 200
MAX_OCR_PAGES = 10

_FONT_CODES: dict[str, tuple[str, str, str, str]] = {
    # regular, bold, italic, bold-italic
    "helv": ("helv", "hebo", "heit", "hebi"),
    "times": ("tiro", "tibo", "tiit", "tibi"),
    "courier": ("cour", "cobo", "coit", "cobi"),
}


def _font_code(family: str, bold: bool, italic: bool) -> str:
    codes = _FONT_CODES.get(family, _FONT_CODES["helv"])
    return codes[(1 if bold else 0) + (2 if italic else 0)]


def _font_from_span(font_name: str, flags: int) -> tuple[str, bool, bool]:
    """Map a PDF span font name/flags onto the built-in PDF font families."""
    name = (font_name or "").lower()
    bold = bool(flags & 16) or any(
        token in name for token in ("bold", "black", "heavy", "semibold", "demi")
    )
    italic = bool(flags & 2) or any(token in name for token in ("italic", "oblique"))
    if "courier" in name or "mono" in name:
        family = "courier"
    elif (
        "times" in name
        or "georgia" in name
        or "garamond" in name
        or "cambria" in name
        or "roman" in name
        or (flags & 4)
    ):
        family = "times"
    else:
        family = "helv"
    return family, bold, italic


def _color_from_int(
    value: object, default: tuple[float, float, float] = (0.0, 0.0, 0.0)
) -> tuple[float, float, float]:
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return (
        ((number >> 16) & 255) / 255,
        ((number >> 8) & 255) / 255,
        (number & 255) / 255,
    )


def _normalise_font_name(value: str) -> str:
    name = re.sub(r"^[A-Z]{6}\+", "", value or "")
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _embedded_font_for_span(
    doc: fitz.Document,
    page: fitz.Page,
    font_name: str,
    text: str,
    resource_name: str,
) -> dict | None:
    """Return an embeddable font matching a span, if it covers the new text.

    Reusing the original font keeps glyph shapes identical. Subset fonts that
    lack characters in the replacement are skipped so base-14 fallback stays
    available.
    """
    target = _normalise_font_name(font_name)
    if not target or not text:
        return None
    try:
        fonts = page.get_fonts(full=True)
    except Exception:
        return None
    exact: list[tuple[int, str]] = []
    loose: list[tuple[int, str]] = []
    for entry in fonts:
        if len(entry) < 5:
            continue
        xref, ext, _subtype, basefont, name = entry[:5]
        if not xref or ext in (None, "", "n/a"):
            continue
        candidate = _normalise_font_name(str(basefont or ""))
        if not candidate:
            continue
        pair = (int(xref), str(name or ""))
        if candidate == target:
            exact.append(pair)
        elif target in candidate or candidate in target:
            loose.append(pair)
    for xref, _name in exact + loose:
        try:
            _basefont, _ext, _subtype, buffer = doc.extract_font(xref)
        except Exception:
            continue
        if not buffer:
            continue
        try:
            font = fitz.Font(fontbuffer=buffer)
            if not all(
                font.has_glyph(ord(character))
                for character in text
                if not character.isspace()
            ):
                continue
        except Exception:
            continue
        return {"fontname": resource_name, "fontbuffer": bytes(buffer)}
    return None


def _detect_text_style(page: fitz.Page, rect: fitz.Rect) -> dict | None:
    """Find the original span that overlaps an edited rectangle.

    Returns its colour, size, font name and flags so replacement text can look
    like the text it replaced. Must run before the area is redacted.
    """
    try:
        data = page.get_text("dict")
    except Exception:
        return None
    best: dict | None = None
    best_ratio = 0.0
    for block in data.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                bbox = span.get("bbox")
                if not bbox or len(bbox) != 4:
                    continue
                span_rect = fitz.Rect(bbox)
                if span_rect.is_empty:
                    continue
                x0 = max(rect.x0, span_rect.x0)
                y0 = max(rect.y0, span_rect.y0)
                x1 = min(rect.x1, span_rect.x1)
                y1 = min(rect.y1, span_rect.y1)
                if x1 <= x0 or y1 <= y0:
                    continue
                overlap = (x1 - x0) * (y1 - y0)
                ratio = overlap / max(1.0, span_rect.get_area())
                if ratio > best_ratio:
                    best_ratio = ratio
                    best = span
    if best is None or best_ratio < 0.15:
        return None
    try:
        style = {
            "font": str(best.get("font") or ""),
            "flags": int(best.get("flags") or 0),
            "size": float(best.get("size") or 0),
            "color": int(best.get("color") or 0),
        }
    except (TypeError, ValueError):
        return None
    return style


def _color(value: object, default: tuple[float, float, float] | None = None):
    """Parse '#rgb' or '#rrggbb' into a PyMuPDF colour tuple."""
    if value is None or value == "":
        return default
    text = str(value).strip().lstrip("#")
    if len(text) == 3:
        text = "".join(character * 2 for character in text)
    if len(text) != 6:
        return default
    try:
        red, green, blue = (int(text[index : index + 2], 16) for index in (0, 2, 4))
    except ValueError:
        return default
    return (red / 255, green / 255, blue / 255)


def _number(value: object, default: float, minimum: float, maximum: float) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))


def _page_index(raw: object, total: int) -> int:
    try:
        page = int(str(raw))
    except (TypeError, ValueError):
        raise ValidationAppError("Setiap elemen edit harus memiliki nomor halaman.")
    if page < 1 or page > total:
        raise ValidationAppError(
            f"Halaman {page} di luar jangkauan (dokumen memiliki {total} halaman)."
        )
    return page - 1


def _insert_text(page: fitz.Page, item: dict) -> None:
    text = str(item.get("text") or "")[:5000]
    if not text.strip():
        return
    size = _number(item.get("size"), 14.0, 4.0, 200.0)
    base_font = _font_code(
        str(item.get("font") or "helv").lower(),
        bool(item.get("bold")),
        bool(item.get("italic")),
    )
    font = base_font
    embedded_name = str(item.get("fontname") or "")
    embedded_buffer = item.get("fontbuffer")
    if (
        embedded_name
        and isinstance(embedded_buffer, (bytes, bytearray))
        and embedded_buffer
    ):
        try:
            page.insert_font(fontname=embedded_name, fontbuffer=bytes(embedded_buffer))
            font = embedded_name
        except Exception:
            font = base_font
    raw_color = item.get("color")
    color = _color(raw_color, None)
    if color is None and isinstance(raw_color, (tuple, list)) and len(raw_color) == 3:
        try:
            color = tuple(max(0.0, min(1.0, float(channel))) for channel in raw_color)
        except (TypeError, ValueError):
            color = (0.0, 0.0, 0.0)
    if color is None:
        color = (0.0, 0.0, 0.0)
    x = _number(item.get("x"), 0.0, 0.0, page.rect.width)
    y = _number(item.get("y"), 0.0, 0.0, page.rect.height)
    line_height = size * 1.25
    for index, line in enumerate(text.splitlines() or [""]):
        if not line:
            continue
        point = fitz.Point(x, y + size + index * line_height)
        if font != base_font:
            try:
                page.insert_text(
                    point,
                    line,
                    fontname=font,
                    fontsize=size,
                    color=color,
                    overlay=True,
                )
                continue
            except Exception:
                # A subset/CID font can still reject a glyph; finish with a
                # built-in font rather than failing the whole edit.
                font = base_font
        page.insert_text(
            point,
            line,
            fontname=font,
            fontsize=size,
            color=color,
            overlay=True,
        )


def _insert_image(page: fitz.Page, item: dict, image_paths: list) -> str | None:
    try:
        asset = int(str(item.get("asset")))
    except (TypeError, ValueError):
        return "Satu gambar tidak dapat ditempatkan karena indeks gambar tidak valid."
    if asset < 0 or asset >= len(image_paths):
        return "Satu gambar tidak dapat ditempatkan karena berkas gambar tidak ditemukan."
    x = _number(item.get("x"), 0.0, 0.0, page.rect.width)
    y = _number(item.get("y"), 0.0, 0.0, page.rect.height)
    width = _number(item.get("w"), 120.0, 4.0, page.rect.width * 2)
    height = _number(item.get("h"), 80.0, 4.0, page.rect.height * 2)
    rect = fitz.Rect(x, y, x + width, y + height)
    try:
        data = image_paths[asset].read_bytes()
        page.insert_image(rect, stream=data, keep_proportion=False, overlay=True)
    except Exception:
        return "Satu gambar gagal disisipkan ke halaman."
    return None


def _draw_shape(page: fitz.Page, item: dict) -> None:
    kind = str(item.get("type") or "").lower()
    stroke = _color(item.get("stroke"), (0.0, 0.0, 0.0)) or (0.0, 0.0, 0.0)
    fill = _color(item.get("fill"), None)
    width = _number(item.get("stroke_width"), 1.0, 0.1, 30.0)
    x = _number(item.get("x"), 0.0, 0.0, page.rect.width)
    y = _number(item.get("y"), 0.0, 0.0, page.rect.height)

    if kind == "line":
        x2 = _number(item.get("x2"), x, -page.rect.width, page.rect.width * 2)
        y2 = _number(item.get("y2"), y, -page.rect.height, page.rect.height * 2)
        page.draw_line(fitz.Point(x, y), fitz.Point(x2, y2), color=stroke, width=width)
        return

    w = _number(item.get("w"), 120.0, 2.0, page.rect.width * 2)
    h = _number(item.get("h"), 60.0, 2.0, page.rect.height * 2)
    rect = fitz.Rect(x, y, x + w, y + h)
    if kind == "ellipse":
        page.draw_oval(rect, color=stroke, fill=fill, width=width)
    else:
        page.draw_rect(rect, color=stroke, fill=fill, width=width)


def _insert_ocr_line(page: fitz.Page, text: str, rect: fitz.Rect) -> bool:
    """Write one invisible text line over the scanned image.

    Invisible text (render mode 3) is searchable and selectable but does not
    change how the page looks, so the original scan stays intact.
    """
    if rect.width < 2 or rect.height < 2 or not text.strip():
        return False
    size = min(rect.height * 0.85, 40.0)
    if size < 3:
        return False
    width = fitz.get_text_length(text, fontname="helv", fontsize=size)
    if width > rect.width > 0:
        size *= rect.width / width
    if size < 3:
        return False
    baseline = rect.y1 - (rect.height - size) / 2
    try:
        page.insert_text(
            fitz.Point(rect.x0, baseline),
            text[:500],
            fontname="helv",
            fontsize=size,
            render_mode=3,
        )
    except Exception:
        return False
    return True


def _add_ocr_text_layers(
    doc: fitz.Document,
    page_indices: set[int],
    language: str,
    warnings: list[str],
) -> set[int]:
    """OCR pages that have no usable text layer and embed an invisible layer."""
    applied: set[int] = set()
    if not page_indices:
        return applied
    if not ocr_available():
        warnings.append(
            "Sebagian halaman tidak memiliki lapisan teks (hasil pindai). "
            "OCR otomatis tidak tersedia di server ini, sehingga ganti teks pada "
            "halaman tersebut tidak dapat dilakukan."
        )
        return applied

    targets = sorted(page_indices)
    if len(targets) > MAX_OCR_PAGES:
        warnings.append(
            f"OCR otomatis dibatasi pada {MAX_OCR_PAGES} halaman pertama dari "
            f"{len(targets)} halaman yang memerlukannya."
        )
        targets = targets[:MAX_OCR_PAGES]

    for page_index in targets:
        page = doc[page_index]
        if page.rotation % 360:
            warnings.append(
                f"Halaman {page_index + 1} diputar, sehingga OCR otomatis dilewati."
            )
            continue
        try:
            lines = ocr_pdf_page_lines(page, language=language)
        except Exception:
            warnings.append(
                f"Halaman {page_index + 1}: OCR otomatis gagal dijalankan."
            )
            continue

        written = 0
        for line in lines:
            rect = fitz.Rect(line["x0"], line["y0"], line["x1"], line["y1"])
            if _insert_ocr_line(page, str(line.get("text") or ""), rect):
                written += 1
        if written:
            applied.add(page_index)
            warnings.append(
                f"Halaman {page_index + 1}: OCR otomatis menambahkan lapisan teks "
                "agar teks pada hasil pindai dapat diganti."
            )
        else:
            warnings.append(
                f"Halaman {page_index + 1}: OCR tidak menemukan teks yang dapat diganti."
            )
    return applied


def _apply_text_edits(
    doc: fitz.Document,
    edits: list[dict],
    warnings: list[str],
) -> None:
    """Direct text edits: blank the clicked text rectangle and write the new text.

    The browser sends the rectangle of the text the user clicked, so no search
    is involved. All redactions on a page are applied before any replacement
    text is inserted.
    """
    grouped: dict[int, list[dict]] = {}
    for entry in edits:
        page_index = _page_index(entry.get("page", 1), doc.page_count)
        grouped.setdefault(page_index, []).append(entry)

    for page_index, entries in grouped.items():
        page = doc[page_index]
        planned: list[tuple[dict, fitz.Rect, dict | None, dict | None]] = []
        for entry in entries:
            x = _number(entry.get("x"), 0.0, 0.0, page.rect.width)
            y = _number(entry.get("y"), 0.0, 0.0, page.rect.height)
            width = _number(entry.get("w"), 1.0, 1.0, page.rect.width * 2)
            height = _number(entry.get("h"), 1.0, 1.0, page.rect.height * 2)
            rect = fitz.Rect(x, y, x + width, y + height)
            if rect.width < 2 or rect.height < 2:
                warnings.append(
                    f"Halaman {page_index + 1}: satu area teks terlalu kecil untuk diedit."
                )
                continue
            # Detect before the redaction removes the original span.
            style = (
                _detect_text_style(page, rect)
                if entry.get("auto_style") is True
                else None
            )
            embedded = None
            if style:
                embedded = _embedded_font_for_span(
                    doc,
                    page,
                    style["font"],
                    str(entry.get("text") or ""),
                    f"EDF{len(planned)}",
                )
            page.add_redact_annot(rect, fill=(1.0, 1.0, 1.0))
            planned.append((entry, rect, style, embedded))
        if not planned:
            continue
        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)
        for entry, rect, style, embedded in planned:
            size = _number(entry.get("size"), max(6.0, rect.height * 0.8), 4.0, 200.0)
            font = str(entry.get("font") or "helv")
            bold = bool(entry.get("bold"))
            italic = bool(entry.get("italic"))
            color = _color(entry.get("color"), (0.0, 0.0, 0.0)) or (0.0, 0.0, 0.0)
            if style:
                font, bold, italic = _font_from_span(style["font"], style["flags"])
                color = _color_from_int(style["color"], color)
            baseline = _number(entry.get("baseline"), rect.y1, rect.y0, rect.y1)
            baseline = max(rect.y0 + size * 0.35, min(baseline, rect.y1))
            item = {
                "text": str(entry.get("text") or ""),
                "x": rect.x0,
                "y": baseline - size,
                "size": size,
                "color": color,
                "font": font,
                "bold": bold,
                "italic": italic,
            }
            if embedded:
                item["fontname"] = embedded["fontname"]
                item["fontbuffer"] = embedded["fontbuffer"]
            _insert_text(page, item)


def _replace_text(
    doc: fitz.Document,
    replacements: list[dict],
    warnings: list[str],
    ocr_pages: set[int] | None = None,
) -> None:
    """Blank the found text (redaction) and write the replacement in its place."""
    ocr_pages = ocr_pages or set()
    grouped: dict[int, list[dict]] = {}
    for entry in replacements:
        if not isinstance(entry, dict):
            continue
        search = str(entry.get("search") or "").strip()
        if not search:
            continue
        page_index = _page_index(entry.get("page", 1), doc.page_count)
        grouped.setdefault(page_index, []).append(entry)

    hits = 0
    for page_index, entries in grouped.items():
        page = doc[page_index]
        # Collect the rectangles before redacting: after apply_redactions the
        # matched text is gone and can no longer be found by searching.
        planned: list[tuple[dict, fitz.Rect]] = []
        for entry in entries:
            search = str(entry.get("search") or "").strip()[:300]
            if not search:
                continue
            rects = page.search_for(search)
            if not rects:
                if page_index in ocr_pages:
                    warnings.append(
                        f"Teks '{search[:60]}' tidak cocok dengan hasil OCR pada "
                        f"halaman {page_index + 1}. Periksa ejaan atau gunakan teks "
                        "yang terbaca jelas pada pindaian."
                    )
                else:
                    warnings.append(
                        f"Teks '{search[:60]}' tidak ditemukan pada halaman {page_index + 1}; "
                        "mungkin berupa bagian dari gambar atau PDF hasil pindai."
                    )
                continue
            for rect in rects:
                if hits >= MAX_REPLACEMENT_HITS:
                    break
                page.add_redact_annot(rect, fill=(1.0, 1.0, 1.0))
                planned.append((entry, rect))
                hits += 1
            if hits >= MAX_REPLACEMENT_HITS:
                break
        if not planned:
            continue
        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)
        for entry, rect in planned:
            size = _number(entry.get("size"), max(6.0, rect.height * 0.8), 4.0, 200.0)
            _insert_text(
                page,
                {
                    "text": str(entry.get("text") or ""),
                    "x": rect.x0,
                    "y": rect.y1 - size,
                    "size": size,
                    "color": entry.get("color"),
                    "font": entry.get("font"),
                },
            )
        if hits >= MAX_REPLACEMENT_HITS:
            warnings.append("Penggantian teks dibatasi pada 200 kemunculan pertama.")
            break


def apply_edits(ctx: ConversionContext) -> ConversionResult:
    pdf_inputs = [path for path in ctx.inputs if path.suffix.lower() == ".pdf"]
    image_paths = [
        path
        for path in ctx.inputs
        if path.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff")
    ]
    if len(pdf_inputs) != 1:
        raise ValidationAppError("Pilih tepat satu berkas PDF untuk diedit.")

    raw = ctx.options.get("edits")
    if isinstance(raw, (dict, list)):
        payload = raw
    else:
        try:
            payload = json.loads(str(raw or "{}"))
        except ValueError:
            raise ValidationAppError("Data edit PDF tidak valid. Muat ulang editor lalu coba lagi.")
    if not isinstance(payload, dict):
        raise ValidationAppError("Data edit PDF harus berupa objek JSON.")

    items = payload.get("items") or []
    replacements = payload.get("replaces") or payload.get("replace") or []
    if not isinstance(items, list) or not isinstance(replacements, list):
        raise ValidationAppError("Data edit PDF tidak valid.")
    items = [item for item in items if isinstance(item, dict)]
    replacements = [entry for entry in replacements if isinstance(entry, dict)]
    if not items and not replacements:
        raise ValidationAppError(
            "Tambahkan minimal satu elemen (teks, gambar, bentuk, atau penggantian teks) "
            "sebelum menerapkan edit."
        )
    if len(items) > MAX_ITEMS:
        raise ValidationAppError(f"Maksimal {MAX_ITEMS} elemen per dokumen.")

    from app.services.pdf_ops import _open_pdf

    doc = _open_pdf(pdf_inputs[0])
    warnings: list[str] = []
    output = ctx.workdir / "edited.pdf"
    try:
        direct_edits = [
            item
            for item in items
            if str(item.get("type") or "").lower() in ("text-edit", "replace-rect")
        ]
        other_items = [
            item
            for item in items
            if str(item.get("type") or "").lower() not in ("text-edit", "replace-rect")
        ]
        if direct_edits:
            _apply_text_edits(doc, direct_edits, warnings)

        if replacements:
            # A scan has no text to search, so give the replacement something to
            # find first. OCR only the pages that actually need it.
            ocr_targets: set[int] = set()
            for entry in replacements:
                search = str(entry.get("search") or "").strip()
                if not search:
                    continue
                page_index = _page_index(entry.get("page", 1), doc.page_count)
                page = doc[page_index]
                if page.search_for(search[:300]):
                    continue
                if len(page.get_text("text").strip()) >= 4:
                    continue
                ocr_targets.add(page_index)
            language = str(ctx.options.get("ocr_language") or "eng+ind")
            ocr_pages = _add_ocr_text_layers(doc, ocr_targets, language, warnings)
            _replace_text(doc, replacements, warnings, ocr_pages)

        for item in other_items:
            page_index = _page_index(item.get("page", 1), doc.page_count)
            page = doc[page_index]
            kind = str(item.get("type") or "text").lower()
            if kind == "text":
                _insert_text(page, item)
            elif kind == "image":
                message = _insert_image(page, item, image_paths)
                if message:
                    warnings.append(message)
            elif kind in ("rect", "ellipse", "line"):
                _draw_shape(page, item)

        doc.save(output, garbage=4, deflate=True)
    finally:
        doc.close()

    if not warnings:
        warnings.append(
            "Teks dan bentuk baru ditambahkan sebagai lapisan di atas PDF asli. "
            "Teks lama yang Anda klik dan ubah ditutup dan diganti dengan teks baru."
        )
    return ConversionResult(output, "edited.pdf", "application/pdf", warnings)
