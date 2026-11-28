"""In-browser DOCX text editing endpoints."""
from __future__ import annotations

from difflib import SequenceMatcher
from io import BytesIO
import json
from pathlib import Path
import re
from urllib.parse import quote

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph
from docx.text.run import Run
from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import Response

from app.core.config import settings
from app.core.errors import ConversionError, UnsupportedFormatError, ValidationAppError
from app.core.rate_limit import enforce_rate_limit
from app.core.storage import extension_of

router = APIRouter(prefix="/word", tags=["word editor"])
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
MAX_PARAGRAPHS = 20_000
MAX_ADDED_PARAGRAPHS = 500
MAX_TEXT_CHARS = 1_000_000


async def _read_docx(upload: UploadFile) -> tuple[bytes, str]:
    if extension_of(upload.filename) != ".docx":
        raise UnsupportedFormatError("Editor Word menerima berkas .docx.")
    data = await upload.read()
    limit = settings.max_upload_mb * 1024 * 1024
    if len(data) > limit:
        raise ValidationAppError(
            f"Berkas melebihi batas {settings.max_upload_mb} MB."
        )
    if not data:
        raise ValidationAppError("Berkas Word kosong.")
    return data, Path(upload.filename or "dokumen.docx").name


PROTECTION_TAGS = ("w:documentProtection", "w:writeProtection")


def _remove_editing_protection(document) -> bool:
    """Drop Word's read-only / restrict-editing settings from the saved copy.

    A form DOCX often carries ``w:documentProtection`` (restrict editing) or
    ``w:writeProtection`` (password to modify). python-docx round-trips both, so
    the edited file would still open read-only in Word even though the user has
    just edited it here. The download is a new copy that is meant to be edited
    further, so those settings are removed.
    """
    settings = document.settings.element
    changed = False
    for tag in PROTECTION_TAGS:
        for node in settings.findall(qn(tag)):
            settings.remove(node)
            changed = True
    return changed


def _is_encrypted_docx(data: bytes) -> bool:
    """True for an OOXML file saved with a password (an OLE compound file)."""
    if not data.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return False
    # Encrypted OOXML packets expose this stream name as UTF-16LE. Legacy
    # binary .doc files use the same container but carry "WordDocument".
    return "EncryptedPackage".encode("utf-16-le") in data


def _open_docx(data: bytes):
    if _is_encrypted_docx(data):
        raise ValidationAppError(
            "Dokumen Word ini dilindungi kata sandi. Buka proteksinya di Word "
            "(File > Info > Protect Document) lalu unggah ulang berkasnya."
        )
    try:
        return Document(BytesIO(data))
    except Exception as exc:
        raise ConversionError(
            "Dokumen Word tidak dapat dibuka. Pastikan berkas DOCX tidak rusak."
        ) from exc


def _paragraphs(document) -> list[Paragraph]:
    # Iterating the document XML includes paragraphs inside table cells while
    # keeping the same order used by Word.
    return [Paragraph(element, document) for element in document.element.body.iter(qn("w:p"))]


def _replacement_run_indexes(old_text: str, new_text: str, run_ranges: list[tuple[int, int]]) -> list[str]:
    assignments = [""] * len(run_ranges)
    if not run_ranges:
        return assignments
    char_runs: list[int] = []
    for run_index, (start, end) in enumerate(run_ranges):
        char_runs.extend([run_index] * (end - start))
    matcher = SequenceMatcher(a=old_text, b=new_text, autojunk=False)
    for opcode, old_start, old_end, new_start, new_end in matcher.get_opcodes():
        if opcode == "equal":
            for offset in range(old_end - old_start):
                run_index = char_runs[old_start + offset]
                assignments[run_index] += new_text[new_start + offset]
            continue
        if opcode in {"replace", "insert"} and new_start < new_end:
            if old_start > 0:
                run_index = char_runs[min(old_start - 1, len(char_runs) - 1)]
            elif old_start < len(char_runs):
                run_index = char_runs[old_start]
            else:
                run_index = 0
            assignments[run_index] += new_text[new_start:new_end]
    return assignments


def _text_runs(paragraph: Paragraph) -> list[Run]:
    """Runs that make up ``Paragraph.text``, including runs inside hyperlinks.

    python-docx exposes hyperlink text through ``Paragraph.text`` but not through
    ``Paragraph.runs``. Editing with only the direct runs therefore leaves the
    hyperlink text untouched and duplicates it in the saved document.
    """
    runs: list[Run] = []
    for child in paragraph._p:
        if child.tag == qn("w:r"):
            runs.append(Run(child, paragraph))
        elif child.tag == qn("w:hyperlink"):
            runs.extend(Run(run, paragraph) for run in child.iter(qn("w:r")))
    return runs


def _set_paragraph_text(paragraph: Paragraph, text: str) -> None:
    runs = _text_runs(paragraph)
    if not runs:
        paragraph.add_run(text)
        return
    old_text = "".join(run.text for run in runs)
    if old_text == text:
        return
    ranges: list[tuple[int, int]] = []
    cursor = 0
    for run in runs:
        start = cursor
        cursor += len(run.text)
        ranges.append((start, cursor))
    assignments = _replacement_run_indexes(old_text, text, ranges)
    if not old_text:
        # The paragraph holds pictures, tabs, or other non-text objects but no
        # editable text. Add the new text in its own run; writing through
        # Run.text would delete those objects.
        paragraph.add_run(text)
        return
    for run, replacement in zip(runs, assignments):
        if not _replace_run_text(run, replacement):
            # This run has no text/tab/break children (for example it only
            # holds an inline picture). Leave it untouched so the picture
            # survives the edit.
            if replacement:
                paragraph.add_run(replacement)


# Text-like run children. Run.text expands tabs/breaks to "\t"/"\n", so they
# must be rebuilt as real OOXML elements instead of literal characters.
_TEXT_RUN_TAGS = frozenset(
    {
        qn("w:t"),
        qn("w:tab"),
        qn("w:br"),
        qn("w:cr"),
        qn("w:noBreakHyphen"),
        qn("w:ptab"),
    }
)


def _run_text_elements(text: str) -> list:
    elements = []
    for chunk in re.split(r"(\t|\r\n|\r|\n)", text):
        if chunk == "\t":
            elements.append(OxmlElement("w:tab"))
        elif chunk in ("\r\n", "\r", "\n"):
            elements.append(OxmlElement("w:br"))
        elif chunk:
            node = OxmlElement("w:t")
            node.text = chunk
            node.set(qn("xml:space"), "preserve")
            elements.append(node)
    return elements


def _replace_run_text(run, text: str) -> bool:
    """Replace a run's text while keeping drawings, fields, and breaks intact."""
    element = run._r
    children = list(element)
    textual = [child for child in children if child.tag in _TEXT_RUN_TAGS]
    if not textual:
        return False
    position = children.index(textual[0])
    for child in textual:
        element.remove(child)
    for child in _run_text_elements(text):
        element.insert(position, child)
        position += 1
    return True


@router.post("/inspect", dependencies=[Depends(enforce_rate_limit)])
async def inspect_word_document(file: UploadFile = File(...)):
    data, filename = await _read_docx(file)
    document = _open_docx(data)
    paragraphs = _paragraphs(document)
    if len(paragraphs) > MAX_PARAGRAPHS:
        raise ValidationAppError(
            f"Dokumen memiliki terlalu banyak paragraf (maksimal {MAX_PARAGRAPHS})."
        )
    return {
        "filename": filename,
        "paragraphs": [
            {
                "index": index,
                "text": paragraph.text,
                "style": paragraph.style.name if paragraph.style else "Normal",
            }
            for index, paragraph in enumerate(paragraphs)
        ],
    }


@router.post("/save", dependencies=[Depends(enforce_rate_limit)])
async def save_word_document(
    file: UploadFile = File(...),
    changes: str = Form("[]"),
    additions: str = Form("[]"),
):
    data, filename = await _read_docx(file)
    try:
        raw_changes = json.loads(changes)
        raw_additions = json.loads(additions)
    except (TypeError, ValueError) as exc:
        raise ValidationAppError("Perubahan dokumen tidak valid.") from exc
    if not isinstance(raw_changes, list) or not isinstance(raw_additions, list):
        raise ValidationAppError("Perubahan dokumen harus berupa daftar.")
    if len(raw_changes) > MAX_PARAGRAPHS or len(raw_additions) > MAX_ADDED_PARAGRAPHS:
        raise ValidationAppError("Jumlah perubahan dokumen melebihi batas.")
    if any(not isinstance(item, dict) for item in raw_changes):
        raise ValidationAppError("Format perubahan paragraf tidak valid.")
    if any(not isinstance(item, str) for item in raw_additions):
        raise ValidationAppError("Paragraf tambahan harus berupa teks.")
    total_chars = sum(len(str(item.get("text", ""))) for item in raw_changes)
    total_chars += sum(len(item) for item in raw_additions)
    if total_chars > MAX_TEXT_CHARS:
        raise ValidationAppError("Teks dokumen melebihi batas editor.")

    document = _open_docx(data)
    paragraphs = _paragraphs(document)
    for change in raw_changes:
        index = change.get("index")
        text = change.get("text")
        if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(paragraphs):
            raise ValidationAppError("Nomor paragraf tidak valid.")
        if not isinstance(text, str):
            raise ValidationAppError("Isi paragraf harus berupa teks.")
        _set_paragraph_text(paragraphs[index], text)
    for text in raw_additions:
        document.add_paragraph(text)

    _remove_editing_protection(document)

    output = BytesIO()
    document.save(output)
    output_name = f"{Path(filename).stem}-edited.docx"
    return Response(
        content=output.getvalue(),
        media_type=DOCX_MIME,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(output_name)}"
        },
    )
