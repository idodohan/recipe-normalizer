"""Application factory for Recipe Normalizer."""

from __future__ import annotations

from importlib.metadata import version

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from recipe_normalizer.catalog.router import router as catalog_router
from recipe_normalizer.cookbook import service as cookbook_service
from recipe_normalizer.cookbook.router import router as cookbook_router
from recipe_normalizer.errors import install_error_handlers
from recipe_normalizer.users.router import router as users_router


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
        allow_origins=["http://localhost:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Install consistent error-envelope handlers
    install_error_handlers(app)

    # Register cookbook merge hooks (idempotent)
    cookbook_service.register_hooks()

    # Include routers
    app.include_router(users_router)
    app.include_router(catalog_router)
    app.include_router(cookbook_router)

    @app.get("/api/health", tags=["health"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
