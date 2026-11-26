"""Convenience endpoints for the core PDF operations and OCR.

They share the same validation and job pipeline as POST /conversions, so there is
a single implementation behind both surfaces.
"""
from __future__ import annotations

import asyncio

import fitz
from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Request, Response, UploadFile
from sqlalchemy.orm import Session

from app.api.serializers import job_to_out
from app.core.config import settings
from app.core.database import get_db
from app.core.errors import ConversionError, UnsupportedFormatError, ValidationAppError
from app.core.rate_limit import enforce_rate_limit
from app.core.security import get_optional_user
from app.core.storage import extension_of
from app.models.user import User
from app.schemas.conversion import ConversionJobOut
from app.services.capabilities import ocr_available
from app.services.job_creation import create_job
from app.services.ocr import ocr_pdf_page_lines
from app.services.pdf_editor import _remove_page_graphics, extract_page_drawings
from app.workers.runner import run_conversion_job

router = APIRouter(prefix="/pdf", tags=["pdf"])
ocr_router = APIRouter(tags=["ocr"])


@router.post("/drawings")
async def pdf_drawings(
    files: list[UploadFile] = File(...),
    _rate=Depends(enforce_rate_limit),
):
    """List editable vector paths from a PDF for the browser editor."""
    if len(files) != 1 or extension_of(files[0].filename) != ".pdf":
        raise UnsupportedFormatError("Editor shape menerima tepat satu berkas PDF.")
    data = await files[0].read()
    if len(data) > settings.max_upload_mb * 1024 * 1024:
        raise ValidationAppError(
            f"Berkas melebihi batas {settings.max_upload_mb} MB untuk editor PDF."
        )
    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception as exc:
        raise ConversionError("PDF tidak dapat dibuka.") from exc
    try:
        if doc.needs_pass:
            raise ValidationAppError("PDF ini dilindungi kata sandi.")
        page_count = min(doc.page_count, 20)
        return {
            "pages": [
                {
                    "page": index + 1,
                    "drawings": extract_page_drawings(doc[index]),
                    "truncated": len(doc[index].get_drawings()) > 2000,
                }
                for index in range(page_count)
            ],
            "page_count": doc.page_count,
        }
    finally:
        doc.close()


@router.post("/background-without-shapes")
async def pdf_background_without_shapes(
    files: list[UploadFile] = File(...),
    page_number: int = Form(1),
    _rate=Depends(enforce_rate_limit),
):
    """Render a page with vector paths removed but text and images untouched."""
    if len(files) != 1 or extension_of(files[0].filename) != ".pdf":
        raise UnsupportedFormatError("Pilih satu berkas PDF untuk merender halaman.")
    data = await files[0].read()
    if len(data) > settings.max_upload_mb * 1024 * 1024:
        raise ValidationAppError(
            f"Berkas melebihi batas {settings.max_upload_mb} MB untuk editor PDF."
        )
    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception as exc:
        raise ConversionError("PDF tidak dapat dibuka.") from exc
    try:
        if doc.needs_pass:
            raise ValidationAppError("PDF ini dilindungi kata sandi.")
        if page_number < 1 or page_number > doc.page_count:
            raise ValidationAppError("Nomor halaman PDF tidak valid.")
        page = doc[page_number - 1]
        _remove_page_graphics(page)
        pixmap = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5), alpha=False)
        return Response(content=pixmap.tobytes("png"), media_type="image/png")
    finally:
        doc.close()


@router.post("/ocr-layout")
async def ocr_page_layout(
    files: list[UploadFile] = File(...),
    page_number: int = Form(1),
    language: str = Form("eng+ind"),
    _rate=Depends(enforce_rate_limit),
):
    """Return OCR text boxes for one page so scanned text can be clicked.

    This is a read-only helper for the PDF editor: it does not create a
    conversion job and does not write anything to storage.
    """
    if len(files) != 1:
        raise ValidationAppError("OCR layout menerima tepat satu berkas PDF.")
    upload = files[0]
    if extension_of(upload.filename) != ".pdf":
        raise UnsupportedFormatError("OCR layout hanya menerima berkas PDF.")

    data = await upload.read()
    limit = settings.max_upload_mb * 1024 * 1024
    if len(data) > limit:
        raise ValidationAppError(
            f"Berkas melebihi batas {settings.max_upload_mb} MB untuk OCR per halaman."
        )
    if not data:
        raise ValidationAppError("Berkas PDF kosong.")

    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception as exc:
        raise ConversionError("PDF tidak dapat dibuka. Berkas mungkin rusak.") from exc
    try:
        if doc.needs_pass:
            raise ValidationAppError(
                "PDF terproteksi kata sandi. Buka proteksinya terlebih dahulu."
            )
        if page_number < 1 or page_number > doc.page_count:
            raise ValidationAppError(
                f"Halaman {page_number} di luar jangkauan "
                f"(dokumen memiliki {doc.page_count} halaman)."
            )
        if not ocr_available():
            raise UnsupportedFormatError(
                "OCR belum tersedia di server ini. Pasang Tesseract OCR atau "
                "rapidocr-onnxruntime untuk mendeteksi teks pada PDF pindai."
            )
        page = doc[page_number - 1]
        if page.rotation % 360:
            raise ValidationAppError(
                "Halaman ini diputar, sehingga OCR per halaman belum didukung."
            )
        lines = await asyncio.to_thread(
            ocr_pdf_page_lines, page, language=language or "eng+ind", dpi=200
        )
        runs = [
            {
                "text": str(line.get("text") or ""),
                "x": float(line["x0"]),
                "y": float(line["y0"]),
                "w": max(0.0, float(line["x1"]) - float(line["x0"])),
                "h": max(0.0, float(line["y1"]) - float(line["y0"])),
            }
            for line in lines
            if str(line.get("text") or "").strip()
        ]
        return {
            "page": page_number,
            "page_count": doc.page_count,
            "width_pt": page.rect.width,
            "height_pt": page.rect.height,
            "runs": runs,
        }
    finally:
        doc.close()


def _parse_options(raw: str | None) -> dict:
    import json

    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        raise ValidationAppError("The options must be valid JSON.")
    if not isinstance(value, dict):
        raise ValidationAppError("The options must be a JSON object.")
    return value


@router.post("/merge", response_model=ConversionJobOut, status_code=202)
async def merge_pdfs_endpoint(
    background_tasks: BackgroundTasks,
    request: Request,
    response: Response,
    files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
    user: User | None = Depends(get_optional_user),
    _rate=Depends(enforce_rate_limit),
):
    job = await create_job(
        request=request, response=response, db=db, user=user,
        tool_id="merge-pdf", uploads=files,
    )
    background_tasks.add_task(run_conversion_job, job.id)
    return job_to_out(job)


@router.post("/split", response_model=ConversionJobOut, status_code=202)
async def split_pdf_endpoint(
    background_tasks: BackgroundTasks,
    request: Request,
    response: Response,
    files: list[UploadFile] = File(...),
    options: str = Form("{}"),
    db: Session = Depends(get_db),
    user: User | None = Depends(get_optional_user),
    _rate=Depends(enforce_rate_limit),
):
    job = await create_job(
        request=request, response=response, db=db, user=user,
        tool_id="split-pdf", uploads=files, raw_options=_parse_options(options),
    )
    background_tasks.add_task(run_conversion_job, job.id)
    return job_to_out(job)


@router.post("/compress", response_model=ConversionJobOut, status_code=202)
async def compress_pdf_endpoint(
    background_tasks: BackgroundTasks,
    request: Request,
    response: Response,
    files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
    user: User | None = Depends(get_optional_user),
    _rate=Depends(enforce_rate_limit),
):
    job = await create_job(
        request=request, response=response, db=db, user=user,
        tool_id="compress-pdf", uploads=files,
    )
    background_tasks.add_task(run_conversion_job, job.id)
    return job_to_out(job)


@ocr_router.post("/ocr", response_model=ConversionJobOut, status_code=202)
async def ocr_endpoint(
    background_tasks: BackgroundTasks,
    request: Request,
    response: Response,
    files: list[UploadFile] = File(...),
    output_format: str = Form("txt"),
    language: str = Form("eng"),
    db: Session = Depends(get_db),
    user: User | None = Depends(get_optional_user),
    _rate=Depends(enforce_rate_limit),
):
    if len(files) != 1:
        raise ValidationAppError("OCR accepts exactly one file per job.")
    extension = extension_of(files[0].filename)
    output_format = output_format.lower()
    if extension == ".pdf":
        tool_id = "pdf-ocr-to-word" if output_format == "docx" else "extract-text"
    elif extension in (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"):
        tool_id = "image-ocr"
    else:
        raise ValidationAppError("OCR supports PDF and image files.")

    job = await create_job(
        request=request, response=response, db=db, user=user,
        tool_id=tool_id, uploads=files, raw_options={"language": language},
    )
    background_tasks.add_task(run_conversion_job, job.id)
    return job_to_out(job)
