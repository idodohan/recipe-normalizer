"""Shared error types and FastAPI exception handlers.

This module is dependency-free with respect to other recipe_normalizer modules —
it defines base exceptions that other modules subclass.
"""

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


class ApiError(Exception):
    """Base class for all API-level errors that map to HTTP responses."""

    status_code: int
    code: str
    extra: dict[str, object]

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        extra: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.extra = extra or {}


def _envelope(
    code: str,
    message: str,
    extra: dict[str, object] | None = None,
) -> dict[str, dict[str, object]]:
    payload: dict[str, object] = {"code": code, "message": message}
    if extra:
        payload.update(extra)
    return {"error": payload}


def install_error_handlers(app: FastAPI) -> None:
    """Register consistent error-envelope handlers on *app*."""

    @app.exception_handler(ApiError)
    async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=_envelope(exc.code, exc.message, exc.extra or None),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        first = exc.errors()[0] if exc.errors() else {}
        loc = ".".join(str(p) for p in first.get("loc", []))
        msg = first.get("msg", "Validation error")
        message = f"{loc}: {msg}" if loc else str(msg)
        return JSONResponse(
            status_code=422,
            content=_envelope("validation_error", message),
        )

    @app.exception_handler(Exception)
    async def fallback_handler(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=500,
            content=_envelope("internal_error", "An unexpected error occurred."),
        )
