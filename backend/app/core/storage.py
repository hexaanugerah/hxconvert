"""Storage layout, secure file writes and path containment checks."""
from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import uuid

from fastapi import UploadFile

from app.core.config import settings
from app.core.errors import FileTooLargeError, UnsupportedFormatError, ValidationAppError

INPUT_DIR = settings.storage_dir / "inputs"
OUTPUT_DIR = settings.storage_dir / "outputs"
TEMP_DIR = settings.storage_dir / "tmp"

for _directory in (INPUT_DIR, OUTPUT_DIR, TEMP_DIR):
    _directory.mkdir(parents=True, exist_ok=True)

ALLOWED_EXTENSIONS = {
    ".pdf", ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".gif",
    ".docx", ".txt", ".md", ".markdown", ".html", ".htm", ".csv", ".xlsx", ".pptx", ".rtf",
}

_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._ -]+")
INPUT_MANIFEST = "_upload-order.json"


def sanitize_filename(name: str | None) -> str:
    """Return a display-only, path-safe filename (never used as a storage path)."""
    base = Path(name or "file").name
    base = base.replace("\\", "_").replace("/", "_")
    base = _SAFE_FILENAME.sub("_", base).strip(" .")
    if not base:
        base = "file"
    return base[:180]


def extension_of(name: str | None) -> str:
    return Path(name or "").suffix.lower()


def job_input_dir(job_id: str) -> Path:
    path = INPUT_DIR / job_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def job_output_dir(job_id: str) -> Path:
    path = OUTPUT_DIR / job_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def job_temp_dir(job_id: str) -> Path:
    path = TEMP_DIR / job_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def stored_name(original: str | None) -> str:
    ext = extension_of(original)
    return f"{uuid.uuid4().hex}{ext if ext in ALLOWED_EXTENSIONS else ''}"


def write_input_manifest(job_id: str, entries: list[dict[str, str]]) -> None:
    """Record upload order and original names next to the stored files.

    Stored filenames are random UUIDs, so directory iteration order does not
    reflect the order the user selected the files in. The manifest is the
    authoritative order used by the worker.
    """
    path = job_input_dir(job_id) / INPUT_MANIFEST
    path.write_text(json.dumps({"files": entries}, ensure_ascii=False), encoding="utf-8")


def read_input_manifest(job_id: str) -> list[dict[str, str]]:
    """Return the recorded upload order, or an empty list for legacy jobs."""
    path = job_input_dir(job_id) / INPUT_MANIFEST
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(files, list):
        return []
    entries: list[dict[str, str]] = []
    for item in files:
        if not isinstance(item, dict):
            continue
        stored = str(item.get("stored") or "")
        if stored and Path(stored).name == stored:
            entries.append({"stored": stored, "original": str(item.get("original") or stored)})
    return entries


def ensure_within(path: Path, root: Path) -> Path:
    resolved = path.resolve()
    if root.resolve() not in resolved.parents and resolved != root.resolve():
        raise ValidationAppError("Invalid file path.")
    return resolved


def remove_job_files(job_id: str) -> None:
    for directory in (INPUT_DIR / job_id, OUTPUT_DIR / job_id, TEMP_DIR / job_id):
        shutil.rmtree(directory, ignore_errors=True)


def _magic_ok(ext: str, head: bytes) -> bool:
    if ext == ".pdf":
        return head.startswith(b"%PDF")
    if ext in {".docx", ".xlsx", ".pptx"}:
        return head.startswith(b"PK\x03\x04")
    if ext == ".png":
        return head.startswith(b"\x89PNG\r\n\x1a\n")
    if ext in {".jpg", ".jpeg"}:
        return head.startswith(b"\xff\xd8\xff")
    if ext == ".gif":
        return head.startswith((b"GIF87a", b"GIF89a"))
    if ext == ".bmp":
        return head.startswith(b"BM")
    if ext in {".tif", ".tiff"}:
        return head.startswith((b"II*\x00", b"MM\x00*"))
    if ext == ".webp":
        return head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    if ext == ".rtf":
        return head.lstrip().startswith(b"{\\rtf")
    if ext in {".txt", ".md", ".markdown", ".csv", ".html", ".htm"}:
        return b"\x00" not in head
    return False


def _office_kind(path: Path) -> str | None:
    import zipfile

    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
    except (zipfile.BadZipFile, OSError):
        return None
    if any(name.startswith("word/") for name in names):
        return ".docx"
    if any(name.startswith("xl/") for name in names):
        return ".xlsx"
    if any(name.startswith("ppt/") for name in names):
        return ".pptx"
    return None


async def save_upload(upload: UploadFile, destination: Path) -> int:
    """Stream an upload to disk while enforcing the configured size limit.

    Returns the number of bytes written. Raises and removes the file on any
    validation failure. Validation inspects file content, not just the name.
    """
    ext = extension_of(upload.filename)
    if ext not in ALLOWED_EXTENSIONS:
        raise UnsupportedFormatError(
            f"File type '{ext or 'unknown'}' is not supported.",
            details={"filename": sanitize_filename(upload.filename)},
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    head = b""
    try:
        with destination.open("wb") as handle:
            while chunk := await upload.read(1024 * 1024):
                if not head:
                    head = chunk[:16]
                total += len(chunk)
                if total > settings.max_upload_bytes:
                    raise FileTooLargeError(
                        f"File exceeds the {settings.max_upload_mb} MB limit.",
                        details={"filename": sanitize_filename(upload.filename)},
                    )
                handle.write(chunk)
    except Exception:
        destination.unlink(missing_ok=True)
        raise

    if total == 0:
        destination.unlink(missing_ok=True)
        raise ValidationAppError(f"'{sanitize_filename(upload.filename)}' is empty.")

    if not _magic_ok(ext, head):
        destination.unlink(missing_ok=True)
        raise UnsupportedFormatError(
            f"The content of '{sanitize_filename(upload.filename)}' does not match its extension ({ext})."
        )

    if ext in {".docx", ".xlsx", ".pptx"}:
        if _office_kind(destination) != ext:
            destination.unlink(missing_ok=True)
            raise UnsupportedFormatError(
                f"'{sanitize_filename(upload.filename)}' is not a valid {ext.upper().lstrip('.')} file."
            )

    return total
