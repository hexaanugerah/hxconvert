"""FastAPI exception handlers for the application error format.

Kept separate from ``app.core.errors`` so conversion workers can import the
error classes without importing FastAPI and the authentication stack.
"""
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.errors import AppError
from app.core.security import AuthError


def _error_response(status_code: int, code: str, message: str, details=None) -> JSONResponse:
    body: dict = {"error": {"code": code, "message": message}}
    if details is not None:
        body["error"]["details"] = details
    return JSONResponse(status_code=status_code, content=body)


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_request: Request, exc: AppError):
        return _error_response(exc.status_code, exc.code, exc.message, exc.details)

    @app.exception_handler(AuthError)
    async def _auth_error(_request: Request, exc: AuthError):
        return _error_response(401, "unauthorized", exc.message)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_request: Request, exc: RequestValidationError):
        details = [
            {"field": ".".join(str(part) for part in err.get("loc", [])), "message": err.get("msg", "")}
            for err in exc.errors()
        ]
        return _error_response(422, "validation_error", "The request data is invalid.", details)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_request: Request, exc: StarletteHTTPException):
        code = "not_found" if exc.status_code == 404 else "http_error"
        return _error_response(exc.status_code, code, str(exc.detail))
