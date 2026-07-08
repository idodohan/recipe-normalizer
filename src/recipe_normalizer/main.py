"""Application factory for Recipe Normalizer."""

from __future__ import annotations

import logging
import mimetypes
import time as _time
from collections.abc import Awaitable, Callable
from importlib.metadata import version

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response

from recipe_normalizer.api_deps import get_current_user
from recipe_normalizer.catalog.router import router as catalog_router
from recipe_normalizer.config import settings
from recipe_normalizer.cookbook import service as cookbook_service
from recipe_normalizer.cookbook.router import router as cookbook_router
from recipe_normalizer.errors import ApiError, install_error_handlers
from recipe_normalizer.filestore import FileStore, get_file_store
from recipe_normalizer.ingestion.router import router as ingestion_router
from recipe_normalizer.sharing.router import router as sharing_router
from recipe_normalizer.users.models import User
from recipe_normalizer.users.router import router as users_router

access_logger = logging.getLogger("recipe_normalizer.access")


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    try:
        pkg_version = version("recipe-normalizer")
    except Exception:
        pkg_version = "0.0.0"

    app = FastAPI(title="Recipe Normalizer", version=pkg_version)

    # CORS for the local frontend dev server
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def access_log(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Log every request: method, path, status, and duration.

        Logs even when the handler raises: ServerErrorMiddleware (outside this
        middleware) renders the 500 envelope and then re-raises, so `call_next`
        raises too. The `finally` block ensures the access log line is still
        emitted for crashing requests.
        """
        start = _time.perf_counter()
        status = 500  # if call_next raises, the client gets a 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            access_logger.info(
                "%s %s -> %d (%.0f ms)",
                request.method,
                request.url.path,
                status,
                (_time.perf_counter() - start) * 1000,
            )

    # Install consistent error-envelope handlers
    install_error_handlers(app)

    # Register cookbook merge hooks (idempotent)
    cookbook_service.register_hooks()

    # Include routers
    app.include_router(users_router)
    app.include_router(catalog_router)
    app.include_router(cookbook_router)
    app.include_router(ingestion_router)
    app.include_router(sharing_router)

    @app.get("/api/health", tags=["health"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/files/{ref:path}", tags=["files"])
    def serve_file(
        ref: str,
        _user: User = Depends(get_current_user),  # noqa: B008
        store: FileStore = Depends(get_file_store),  # noqa: B008
    ) -> Response:
        """Serve a stored file by its content-addressed ref.

        Returns the raw bytes with a guessed media type.
        Invalid or missing refs produce a 404 envelope (no detail leaked).
        """
        try:
            data = store.open(ref)
        except ValueError as exc:
            raise ApiError(404, "not_found", "File not found.") from exc
        except FileNotFoundError as exc:
            raise ApiError(404, "not_found", "File not found.") from exc

        media_type, _ = mimetypes.guess_type(ref)
        if not media_type:
            media_type = "application/octet-stream"

        return Response(content=data, media_type=media_type)

    return app


app = create_app()
