"""HXconvertX API application."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
import logging
import time

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.api.v1 import auth, conversions, pdf, tools
from app.core.config import settings
from app.core.database import Base, SessionLocal, engine
from app.core.errors import register_exception_handlers
from app.schemas.conversion import HealthOut
from app.services.capabilities import capabilities_snapshot
from app.workers.runner import cleanup_expired_jobs, cleanup_loop

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("docuconvert")

VERSION = "1.0.0"


@asynccontextmanager
async def lifespan(app: FastAPI):
    import app.models  # noqa: F401  (register ORM models on the metadata)

    Base.metadata.create_all(bind=engine)
    try:
        await asyncio.to_thread(cleanup_expired_jobs)
    except Exception:  # pragma: no cover
        logger.exception("Initial expiry cleanup failed")
    task = asyncio.create_task(cleanup_loop())
    logger.info(
        "HXconvertX API started (environment=%s, OCR=%s, LibreOffice=%s)",
        settings.environment,
        capabilities_snapshot()["ocr"]["available"],
        capabilities_snapshot()["libreoffice"]["available"],
    )
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


app = FastAPI(
    title="HXconvertX API",
    description="Document conversion and PDF processing API.",
    version=VERSION,
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
    expose_headers=["Content-Disposition"],
)

register_exception_handlers(app)


@app.middleware("http")
async def security_and_logging(request: Request, call_next):
    started = time.perf_counter()
    response = await call_next(request)
    duration_ms = (time.perf_counter() - started) * 1000
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    logger.info("%s %s -> %s (%.0f ms)", request.method, request.url.path, response.status_code, duration_ms)
    return response


api = settings.api_prefix
app.include_router(auth.router, prefix=api)
app.include_router(conversions.router, prefix=api)
app.include_router(tools.router, prefix=api)
app.include_router(pdf.router, prefix=api)
app.include_router(pdf.ocr_router, prefix=api)


@app.get("/api/v1/health", response_model=HealthOut, tags=["health"])
def health():
    database = "ok"
    try:
        with SessionLocal() as db:
            db.execute(text("SELECT 1"))
    except Exception:
        database = "error"
    return HealthOut(
        status="ok" if database == "ok" else "degraded",
        version=VERSION,
        database=database,
        capabilities=capabilities_snapshot(),
    )


@app.get("/", include_in_schema=False)
def root():
    return {
        "name": "HXconvertX API",
        "version": VERSION,
        "docs": "/docs",
        "health": f"{api}/health",
    }
