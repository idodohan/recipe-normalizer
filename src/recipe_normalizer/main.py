"""Application factory for Recipe Normalizer."""

from __future__ import annotations

import logging
import time as _time
import uuid
from collections.abc import Awaitable, Callable
from importlib.metadata import version

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from recipe_normalizer.ai.router import router as ai_router
from recipe_normalizer.api_deps import get_current_user, limit_by_ip
from recipe_normalizer.catalog.router import router as catalog_router
from recipe_normalizer.config import settings
from recipe_normalizer.cookbook import service as cookbook_service
from recipe_normalizer.cookbook.cookbook_router import router as cookbooks_router
from recipe_normalizer.cookbook.models import Cookbook, CookbookRecipe, CookbookVisibility, Recipe
from recipe_normalizer.cookbook.router import router as cookbook_router
from recipe_normalizer.db import get_db
from recipe_normalizer.errors import ApiError, install_error_handlers
from recipe_normalizer.filestore import FileStore, get_file_store, serve_stored_file
from recipe_normalizer.ingestion.router import router as ingestion_router
from recipe_normalizer.sharing.router import public_router as sharing_public_router
from recipe_normalizer.sharing.router import router as sharing_router
from recipe_normalizer.sharing.schemas import PublicRecipeOut
from recipe_normalizer.users.models import User
from recipe_normalizer.users.router import router as users_router

access_logger = logging.getLogger("recipe_normalizer.access")

_public_cookbook_limit = limit_by_ip("public_cookbook", 60, 60.0)


class PublicCookbookRecipeOut(PublicRecipeOut):
    """``PublicRecipeOut``, minus every raw user id, for recipes nested in a public cookbook.

    ``PublicRecipeOut`` (the per-recipe public-link allowlist) carries two
    fields that resolve to a real account id — acceptable there since a
    bare, unguessable UUID isn't PII on its own, and that route's threat
    model already accepted it:

    - ``owner_id`` — always the recipe owner.
    - ``last_edited_by`` — whoever last full-replace-edited the recipe
      (``PATCH /api/recipes/{id}``). For a cookbook with editor MEMBERS this
      is NOT necessarily the owner — it can be any editor's account id, so
      leaking it here would expose a member's identity to anyone holding
      the cookbook's public link, which they never consented to. (Every
      other field on the base class was audited: ``derived_from`` is a
      RECIPE id, not a user id, and everything else is non-identifying.)

    This endpoint's contract is stricter ("MUST NOT leak owner_id [or]
    member emails" — a member's raw user id falls under that same "internal
    fields" umbrella), so this subclass re-declares both fields
    ``exclude=True`` to drop them from serialization while still reusing
    every other field/validator (and ``from_recipe_out``) from the base
    class verbatim — no duplication of the allowlist itself.
    """

    owner_id: uuid.UUID = Field(exclude=True)
    last_edited_by: uuid.UUID | None = Field(default=None, exclude=True)


class PublicCookbookOut(BaseModel):
    """Cookbook payload for the anonymous ``GET /api/public/cookbooks/{token}`` route.

    Deliberately an ALLOWLIST, mirroring ``PublicRecipeOut``'s discipline:
    no ``id``, no ``owner_id``, no member rows/emails, no ``public_token`` —
    only what an anonymous visitor holding a valid unlisted/public link needs
    to browse the cookbook. Recipes reuse ``PublicRecipeOut``'s allowlist via
    ``PublicCookbookRecipeOut`` (owner_id stripped — see its docstring), so a
    cookbook's recipes never carry more than a shared recipe link would.

    Lives here (not in cookbook/schemas.py) because it composes
    ``PublicRecipeOut``, and ``cookbook`` may never import from ``sharing``
    (see .importlinter's cookbook-cannot-import-sharing contract) — main.py
    has no such restriction, and this is the one call site that needs both.
    """

    name: str
    description: str | None = None
    cover_image_url: str | None = None
    recipes: list[PublicCookbookRecipeOut] = []


def _cover_image_url(ref: str | None) -> str | None:
    """Expose a stored cover-image ref as a servable URL path (or pass through)."""
    if ref is None or ref.startswith("/") or "://" in ref:
        return ref
    return f"/api/files/{ref}"


def _resolve_public_cookbook(db: Session, token: str) -> Cookbook:
    """Resolve *token* to a Cookbook, or raise ApiError 404.

    A private cookbook never has a ``public_token`` set (moving to private
    clears it — see ``cookbook.service.set_cookbook_visibility``), so a
    lookup by token alone already can't resolve one; the explicit visibility
    check below is pure defense-in-depth. Passing a cookbook's own ``id`` as
    *token* also 404s here — ids and tokens are different values, so the
    lookup simply finds no row.
    """
    cookbook = db.scalars(select(Cookbook).where(Cookbook.public_token == token)).first()
    if cookbook is None or cookbook.visibility == CookbookVisibility.private:
        raise ApiError(404, "not_found", "Cookbook not found.")
    return cookbook


def _redact_path(path: str) -> str:
    """Redact public-link/cookbook tokens from paths for safe logging.

    Converts /api/public/{token} to /api/public/<token> to prevent bearer
    tokens from appearing in access logs. The cookbooks route nests its
    token one segment deeper (/api/public/cookbooks/{token}) — without a
    special case for that literal "cookbooks" prefix, the naive "redact the
    first segment" rule below would treat "cookbooks" as the token and leave
    the REAL token exposed right after it, so that shape is redacted first.

    Examples:
        /api/public/abc -> /api/public/<token>
        /api/public/abc/scaled -> /api/public/<token>/scaled
        /api/public/cookbooks/abc -> /api/public/cookbooks/<token>
        /api/health -> /api/health (unchanged)
        /api/public/ -> /api/public/ (unchanged, no token)
    """
    if not path.startswith("/api/public/"):
        return path

    # Remove the /api/public/ prefix
    rest = path[len("/api/public/") :]

    # If rest is empty, no token to redact
    if not rest:
        return path

    cookbooks_prefix = "cookbooks/"
    if rest.startswith(cookbooks_prefix):
        token_and_rest = rest[len(cookbooks_prefix) :]
        if not token_and_rest:
            return path
        next_slash = token_and_rest.find("/")
        if next_slash == -1:
            return "/api/public/cookbooks/<token>"
        return "/api/public/cookbooks/<token>" + token_and_rest[next_slash:]

    # Find the next "/" after the token (if it exists)
    next_slash = rest.find("/")

    if next_slash == -1:
        # No "/" after token: /api/public/{token}
        return "/api/public/<token>"
    else:
        # "/" exists after token: /api/public/{token}/...
        return "/api/public/<token>" + rest[next_slash:]


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
                _redact_path(request.url.path),
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
    app.include_router(cookbooks_router)
    app.include_router(ingestion_router)
    app.include_router(sharing_router)
    app.include_router(sharing_public_router)
    app.include_router(ai_router)

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
        return serve_stored_file(store, ref)

    @app.get("/api/public/cookbooks/{token}", response_model=PublicCookbookOut, tags=["public"])
    def get_public_cookbook(
        token: str,
        db: Session = Depends(get_db),  # noqa: B008
        _: None = Depends(_public_cookbook_limit),  # noqa: B008
    ) -> PublicCookbookOut:
        """Anonymous read of a public/unlisted cookbook — no session required.

        Mirrors ``sharing.router``'s public recipe route: token-only lookup,
        rate-limited by IP, and never carries a ``get_current_user``
        dependency. Recipes are fetched via ``cookbook_service.get_recipe_unscoped``
        — the same trusted-internal-caller path ``sharing.service`` uses for its
        own public-link resolution — since this route has already established
        the caller's right to see every recipe under this cookbook via the
        token check above.
        """
        cookbook = _resolve_public_cookbook(db, token)
        # Recipes come from the `cookbook_recipes` join — the sole source of
        # placement — so a recipe merely PLACED into this cookbook (not just
        # created here) is visible to anonymous visitors too.
        recipe_ids = db.scalars(
            select(Recipe.id)
            .join(CookbookRecipe, CookbookRecipe.recipe_id == Recipe.id)
            .where(CookbookRecipe.cookbook_id == cookbook.id)
            # Total order — see cookbook.service.list_recipes' ORDER BY
            # comment; all three recipe listings sort identically.
            .order_by(Recipe.created_at.desc(), Recipe.id.desc())
        ).all()
        # `PublicRecipeOut.from_recipe_out`'s return annotation is fixed to
        # `PublicRecipeOut` (not `Self`), so calling it via the subclass still
        # types as the base — re-validate into PublicCookbookRecipeOut (its
        # `from_attributes=True` config reads the base instance's fields,
        # including the exclude=True override) rather than duplicating
        # from_recipe_out's own logic here.
        recipes = [
            PublicCookbookRecipeOut.model_validate(
                PublicRecipeOut.from_recipe_out(
                    cookbook_service.get_recipe_unscoped(db, recipe_id), token=token
                )
            )
            for recipe_id in recipe_ids
        ]
        return PublicCookbookOut(
            name=cookbook.name,
            description=cookbook.description,
            cover_image_url=_cover_image_url(cookbook.cover_image_ref),
            recipes=recipes,
        )

    return app


app = create_app()
