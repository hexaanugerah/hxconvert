"""Embed the source PDF's fonts into the DOCX produced by the layout engine.

A layout-preserving PDF -> Word conversion keeps text editable, so Word (or
LibreOffice) lays the page out again with the font families named in the DOCX.
Those names come from the PDF, which usually embeds fonts that are not
installed anywhere else: the conversion server only ships DejaVu, Liberation
and Carlito/Caladea. The renderer then substitutes a wider face, line breaks
change, and fixed table cells clip or overlap.

Embedding the PDF's own (already subsetted) TrueType faces into the DOCX makes
the document carry its typography with it, so Word and LibreOffice use the
same glyph widths as the PDF. The package layout follows ECMA-376: obfuscated
font parts under ``word/fonts``, entries in ``fontTable.xml`` and the
``embedTrueTypeFonts`` setting.

Embedding is best effort: fonts with restrictions, PostScript/CFF outlines,
ambiguous subsets or unreadable data are skipped, and the DOCX is always left
valid.
"""
from __future__ import annotations

import hashlib
import io
import logging
import os
import re
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path

import fitz
from fontTools.ttLib import TTFont, TTLibError
from lxml import etree

logger = logging.getLogger(__name__)

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
FONT_REL = f"{R_NS}/font"
ODTTF_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.obfuscatedFont"
)

EMBED_SLOTS = ("regular", "bold", "italic", "boldItalic")
EMBED_TAGS = {
    "regular": "embedRegular",
    "bold": "embedBold",
    "italic": "embedItalic",
    "boldItalic": "embedBoldItalic",
}
# A single embedded face larger than this is almost certainly a full font
# rather than a PDF subset; embedding a handful of those would bloat the DOCX.
MAX_FACE_BYTES = 5 * 1024 * 1024
# Fonts whose license forbids document embedding (OS/2 fsType bit 1).
FS_TYPE_RESTRICTED = 0x0002


@dataclass(frozen=True)
class _Face:
    """One TrueType face extracted from the source PDF."""

    family: str
    subfamily: str
    full_name: str
    postscript_name: str
    data: bytes
    codepoints: frozenset[int]

    @property
    def digest(self) -> str:
        return hashlib.sha1(self.data).hexdigest()

    def covers(self, text: str) -> bool:
        """True when every printable character of ``text`` has a glyph.

        The PDF embeds subsets that only contain the glyphs the document used.
        Embedding such a subset is safe as long as the DOCX does not ask for
        another character: a missing glyph would render as a blank box.
        """
        return all(ord(char) in self.codepoints for char in text if ord(char) > 32)


def _key(value: str) -> str:
    """Lowercase alphanumeric key so ``SourceSansPro-SemiBold`` matches
    ``Source Sans Pro SemiBold``."""
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def _obfuscate(data: bytes, guid: str) -> bytes:
    """ECMA-376 font obfuscation: XOR the first 32 bytes with the GUID key."""
    key = bytes.fromhex(guid.strip("{}").replace("-", ""))[::-1]
    if not key:
        return data
    out = bytearray(data)
    for index in range(min(32, len(out))):
        out[index] ^= key[index % 16]
    return bytes(out)


def _face_from_bytes(data: bytes) -> _Face | None:
    """Read family/style metadata from a font stream, rejecting unusable ones."""
    if not data or len(data) > MAX_FACE_BYTES:
        return None
    try:
        font = TTFont(io.BytesIO(data), lazy=True)
        names = font["name"]
        family = names.getDebugName(16) or names.getDebugName(1) or ""
        subfamily = names.getDebugName(17) or names.getDebugName(2) or ""
        full_name = names.getDebugName(4) or ""
        postscript_name = names.getDebugName(6) or ""
        if "glyf" not in font:
            # Word embeds TrueType outlines; PostScript/CFF faces are skipped.
            logger.debug("font embedding: %s uses CFF outlines", full_name or family)
            return None
        os2 = font["OS/2"] if "OS/2" in font else None
        if os2 is not None and int(getattr(os2, "fsType", 0)) & FS_TYPE_RESTRICTED:
            logger.debug("font embedding: %s forbids embedding", full_name or family)
            return None
        codepoints = frozenset(font.getBestCmap() or ())
        font.close()
    except (TTLibError, KeyError, ValueError, AssertionError) as exc:
        logger.debug("font embedding: unreadable font stream (%s)", exc)
        return None
    if not family:
        return None
    return _Face(
        family.strip(),
        subfamily.strip(),
        full_name.strip(),
        postscript_name.strip(),
        data,
        codepoints,
    )


def _pdf_faces(pdf_path: Path) -> list[_Face]:
    """Extract every usable embedded font face from the PDF, once each."""
    faces: list[_Face] = []
    seen_xrefs: set[int] = set()
    seen_digests: set[str] = set()
    document = fitz.open(str(pdf_path))
    try:
        for page in document:
            for info in page.get_fonts(full=True):
                xref = int(info[0])
                if xref in seen_xrefs:
                    continue
                seen_xrefs.add(xref)
                try:
                    _name, _extension, _type, content = document.extract_font(xref)
                except Exception as exc:  # pragma: no cover - PyMuPDF internals
                    logger.debug("font embedding: extract_font(%s) failed: %s", xref, exc)
                    continue
                if not content:
                    continue
                # Only sfnt (TrueType) streams can be packaged as ODTTF.
                if content[:4] not in (b"\x00\x01\x00\x00", b"true", b"OTTO", b"ttcf"):
                    continue
                face = _face_from_bytes(content)
                if face is None or face.digest in seen_digests:
                    continue
                seen_digests.add(face.digest)
                faces.append(face)
    finally:
        document.close()
    return faces


def _on_off(element: etree._Element | None) -> bool:
    if element is None:
        return False
    value = (element.get(f"{{{W_NS}}}val") or "true").lower()
    return value not in ("0", "false", "off", "none")


def _docx_font_usage(docx_path: Path) -> dict[str, dict[str, set[str]]]:
    """Characters each run font name is asked to render, per embed slot."""
    usage: dict[str, dict[str, set[str]]] = {}
    with zipfile.ZipFile(docx_path) as archive:
        for entry in archive.namelist():
            if not (entry.startswith("word/") and entry.endswith(".xml")):
                continue
            try:
                root = etree.fromstring(archive.read(entry))
            except etree.XMLSyntaxError:
                continue
            for run in root.iter(f"{{{W_NS}}}r"):
                properties = run.find(f"{{{W_NS}}}rPr")
                if properties is None:
                    continue
                fonts = properties.find(f"{{{W_NS}}}rFonts")
                if fonts is None:
                    continue
                name = fonts.get(f"{{{W_NS}}}ascii") or fonts.get(f"{{{W_NS}}}hAnsi")
                if not name:
                    continue
                text = "".join(
                    node.text or "" for node in run.iter(f"{{{W_NS}}}t")
                )
                if not text:
                    continue
                slot = _role_from_flags(
                    _on_off(properties.find(f"{{{W_NS}}}b")),
                    _on_off(properties.find(f"{{{W_NS}}}i")),
                )
                usage.setdefault(name, {}).setdefault(slot, set()).update(text)
    return usage


def _role_from_flags(bold: bool, italic: bool) -> str:
    if bold and italic:
        return "boldItalic"
    if bold:
        return "bold"
    if italic:
        return "italic"
    return "regular"


def _docx_font_names(docx_path: Path) -> set[str]:
    """Every font family named by a run, style, header or footer in the DOCX."""
    pattern = re.compile(rb'w:(?:ascii|hAnsi|eastAsia|cs)="([^"]+)"')
    names: set[str] = set()
    with zipfile.ZipFile(docx_path) as archive:
        for entry in archive.namelist():
            if not (entry.startswith("word/") and entry.endswith(".xml")):
                continue
            for match in pattern.finditer(archive.read(entry)):
                value = match.group(1).decode("utf-8", "replace").strip()
                if value and not value.endswith("Theme"):
                    names.add(value)
    return names


def _role(subfamily: str) -> str:
    value = subfamily.lower()
    bold = any(word in value for word in ("bold", "black", "heavy", "semibold"))
    italic = "italic" in value or "oblique" in value
    return _role_from_flags(bold, italic)


def _match_faces(name: str, faces: list[_Face]) -> dict[str, _Face] | None:
    """Choose the faces of one DOCX font name, one per embed slot."""
    wanted = _key(name)
    if not wanted:
        return None

    family_faces = [face for face in faces if _key(face.family) == wanted]
    if family_faces:
        by_role: dict[str, _Face] = {}
        for face in family_faces:
            role = _role(face.subfamily)
            previous = by_role.get(role)
            if previous is not None and previous.digest != face.digest:
                # The PDF keeps several different subsets of this face (one per
                # page). Embedding just one would lose glyphs from the others.
                logger.debug(
                    "font embedding: ambiguous %s subsets for %s", role, name
                )
                return None
            by_role[role] = face
        if len(family_faces) == 1:
            # A single-face family such as "Source Sans Pro SemiBold" is used
            # for its bold runs too; Word may synthesize weight but keeps the
            # exact widths.
            face = family_faces[0]
            return {slot: face for slot in EMBED_SLOTS}
        if "regular" not in by_role and len(by_role) > 1:
            return None
        return by_role

    # Fall back to the exact face name when the family is not what the DOCX
    # writer recorded (common for style-suffixed names).
    exact = [
        face
        for face in faces
        if wanted in (_key(face.full_name), _key(face.postscript_name))
    ]
    if len(exact) == 1:
        return {slot: exact[0] for slot in EMBED_SLOTS}
    return None


class FontMeasurer:
    """Measure text width in points with the faces embedded in a PDF.

    Used to decide whether a numeric table cell still fits its column once the
    PDF font is rendered by Word/LibreOffice instead of the PDF viewer.
    """

    def __init__(self, pdf_path: Path) -> None:
        self._faces = _pdf_faces(pdf_path)
        self._resolved: dict[str, _Face | None] = {}
        self._fonts: dict[tuple[str, int], object] = {}

    def _face(self, family: str) -> _Face | None:
        if family in self._resolved:
            return self._resolved[family]
        matched = _match_faces(family, self._faces)
        face = None
        if matched:
            face = matched.get("regular") or next(iter(matched.values()))
        self._resolved[family] = face
        return face

    def width(self, family: str, size_pt: float, text: str) -> float | None:
        """Width of ``text`` in points, or None when the face is unavailable."""
        from PIL import ImageFont

        face = self._face(family)
        if face is None or size_pt <= 0 or not text:
            return None
        # FreeType sizes are in pixels; measure at 4x and divide back so the
        # result is in points with sub-point precision.
        scale = 4
        key = (face.digest, max(1, round(size_pt * scale)))
        font = self._fonts.get(key)
        if font is None:
            try:
                font = ImageFont.truetype(
                    io.BytesIO(face.data), max(1, round(size_pt * scale))
                )
            except Exception:  # pragma: no cover - unreadable face
                return None
            self._fonts[key] = font
        return float(font.getlength(text)) / scale


def _new_guid() -> str:
    return "{" + str(uuid.uuid4()).upper() + "}"


def _add_default_content_type(root: etree._Element) -> None:
    for node in root.findall(f"{{{CT_NS}}}Default"):
        if (node.get("Extension") or "").lower() == "odttf":
            return
    node = etree.SubElement(root, f"{{{CT_NS}}}Default")
    node.set("Extension", "odttf")
    node.set("ContentType", ODTTF_CONTENT_TYPE)


def _font_table_node(root: etree._Element, name: str) -> etree._Element:
    for node in root.findall(f"{{{W_NS}}}font"):
        if (node.get(f"{{{W_NS}}}name") or "").strip().lower() == name.lower():
            return node
    node = etree.SubElement(root, f"{{{W_NS}}}font")
    node.set(f"{{{W_NS}}}name", name)
    return node


def _write_package(docx_path: Path, entries: dict[str, bytes]) -> None:
    temp = docx_path.with_name(docx_path.name + ".fonts.tmp")
    try:
        with zipfile.ZipFile(temp, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, blob in entries.items():
                archive.writestr(name, blob)
        os.replace(temp, docx_path)
    finally:
        if temp.exists():
            try:
                temp.unlink()
            except OSError:  # pragma: no cover - defensive
                pass


def embed_pdf_fonts(docx_path: Path, pdf_path: Path) -> list[str]:
    """Embed the PDF's TrueType faces used by ``docx_path``.

    Returns the DOCX font names that now carry their original face. Any
    failure leaves the document untouched, so callers can treat the result as
    optional quality information.
    """
    try:
        names = _docx_font_names(docx_path)
        if not names:
            return []
        faces = _pdf_faces(pdf_path)
        if not faces:
            return []
        usage = _docx_font_usage(docx_path)
        assignments: dict[str, dict[str, _Face]] = {}
        for name in sorted(names):
            matched = _match_faces(name, faces)
            if not matched:
                continue
            # Never embed a subset for text it cannot render: the glyph would
            # come out as an empty box. Skip the whole family when any slot
            # falls short, so one paragraph is not half original, half
            # substituted.
            incomplete = False
            for slot, text in usage.get(name, {}).items():
                face = matched.get(slot)
                if face is None or not face.covers("".join(text)):
                    incomplete = True
                    break
            if incomplete:
                logger.debug("font embedding: subset of %s is incomplete", name)
                continue
            assignments[name] = matched
        if not assignments:
            return []

        with zipfile.ZipFile(docx_path) as archive:
            entries = {entry: archive.read(entry) for entry in archive.namelist()}
        if "word/fontTable.xml" not in entries:
            return []

        font_table = etree.fromstring(entries["word/fontTable.xml"])
        rels_name = "word/_rels/fontTable.xml.rels"
        if rels_name in entries:
            rels = etree.fromstring(entries[rels_name])
        else:
            rels = etree.fromstring(
                f'<Relationships xmlns="{REL_NS}"/>'.encode("utf-8")
            )

        # digest -> (part name, relationship id, font key)
        parts: dict[str, tuple[str, str, str]] = {}
        used_ids = {
            node.get("Id") for node in rels.findall(f"{{{REL_NS}}}Relationship")
        }

        def ensure_part(face: _Face) -> tuple[str, str]:
            existing = parts.get(face.digest)
            if existing:
                return existing
            index = len(parts) + 1
            part = f"word/fonts/font{index}.odttf"
            relationship_id = f"rIdFont{index}"
            while relationship_id in used_ids:
                index += 1
                part = f"word/fonts/font{index}.odttf"
                relationship_id = f"rIdFont{index}"
            used_ids.add(relationship_id)
            guid = _new_guid()
            entries[part] = _obfuscate(face.data, guid)
            node = etree.SubElement(rels, f"{{{REL_NS}}}Relationship")
            node.set("Id", relationship_id)
            node.set("Type", FONT_REL)
            node.set("Target", part[len("word/"):])
            parts[face.digest] = (part, relationship_id, guid)
            return parts[face.digest]

        embedded: list[str] = []
        for name, slots in assignments.items():
            node = _font_table_node(font_table, name)
            for children in list(node):
                if children.tag in {
                    f"{{{W_NS}}}{EMBED_TAGS[slot]}" for slot in EMBED_SLOTS
                }:
                    node.remove(children)
            wrote = False
            for slot in EMBED_SLOTS:
                face = slots.get(slot)
                if face is None:
                    continue
                _part, relationship_id, guid = ensure_part(face)
                child = etree.SubElement(node, f"{{{W_NS}}}{EMBED_TAGS[slot]}")
                child.set(f"{{{R_NS}}}id", relationship_id)
                child.set(f"{{{W_NS}}}fontKey", guid)
                wrote = True
            if wrote:
                embedded.append(name)

        if not embedded:
            return []

        settings = etree.fromstring(entries["word/settings.xml"])
        if settings.find(f"{{{W_NS}}}embedTrueTypeFonts") is None:
            etree.SubElement(settings, f"{{{W_NS}}}embedTrueTypeFonts")

        content_types = etree.fromstring(entries["[Content_Types].xml"])
        _add_default_content_type(content_types)

        entries["word/fontTable.xml"] = etree.tostring(
            font_table, xml_declaration=True, encoding="UTF-8", standalone=True
        )
        entries[rels_name] = etree.tostring(
            rels, xml_declaration=True, encoding="UTF-8", standalone=True
        )
        entries["word/settings.xml"] = etree.tostring(
            settings, xml_declaration=True, encoding="UTF-8", standalone=True
        )
        entries["[Content_Types].xml"] = etree.tostring(
            content_types, xml_declaration=True, encoding="UTF-8", standalone=True
        )
        _write_package(docx_path, entries)
        logger.debug("font embedding: embedded %s", ", ".join(embedded))
        return embedded
    except Exception as exc:  # pragma: no cover - never break a conversion
        logger.warning("font embedding failed for %s: %s", docx_path.name, exc)
        return []
