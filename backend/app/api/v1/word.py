"""In-browser DOCX text editing endpoints."""
from __future__ import annotations

from difflib import SequenceMatcher
from io import BytesIO
import json
from pathlib import Path
from urllib.parse import quote

from docx import Document
from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph
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


def _open_docx(data: bytes):
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


def _set_paragraph_text(paragraph: Paragraph, text: str) -> None:
    runs = paragraph.runs
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
        assignments[0] = text
    for run, replacement in zip(runs, assignments):
        run.text = replacement


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
