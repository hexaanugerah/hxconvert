"""Application error types.

Kept free of FastAPI/Starlette imports: conversion services import these
classes inside worker processes, where pulling in the web framework added
seconds to every worker start. HTTP rendering lives in ``error_handlers``.
"""


class AppError(Exception):
    status_code = 400
    code = "bad_request"

    def __init__(self, message: str, *, code: str | None = None, details=None):
        self.message = message
        self.code = code or self.code
        self.details = details
        super().__init__(message)


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"


class ForbiddenError(AppError):
    status_code = 403
    code = "forbidden"


class ConflictError(AppError):
    status_code = 409
    code = "conflict"


class ExpiredError(AppError):
    status_code = 410
    code = "expired"


class ValidationAppError(AppError):
    status_code = 422
    code = "validation_error"


class ConversionError(AppError):
    status_code = 422
    code = "conversion_failed"


class UnsupportedFormatError(AppError):
    status_code = 415
    code = "unsupported_format"


class FileTooLargeError(AppError):
    status_code = 413
    code = "file_too_large"


class RateLimitError(AppError):
    status_code = 429
    code = "rate_limited"
