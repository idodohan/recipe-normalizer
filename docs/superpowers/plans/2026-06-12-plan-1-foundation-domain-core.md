# Plan 1: Foundation & Domain Core — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up the recipe-normalizer modular monolith — repo, tooling, docker-compose, users/auth, catalog (units/densities/seed), cookbook (recipes, dual quantities, scaling), manual entry, and the polished UI shell — i.e. spec §11 phases 1–2, producing working software: log in, manually add a recipe, browse it, see dual quantities, scale it.

**Architecture:** Modular monolith per spec §4. Backend `src/recipe_normalizer/` with modules `users`, `catalog`, `cookbook` (this plan; `ingestion`/`extraction`/`sharing`/`ai` come in Plans 2–4). Each module owns its tables; cross-module access only via each module's `service.py`/`schemas.py`, enforced by import-linter in CI. Frontend is React+TS with a typed client generated from OpenAPI; drift fails CI.

**Tech Stack:** Python 3.12, uv, FastAPI, SQLAlchemy 2.x, Alembic, Pydantic v2, Postgres 16, psycopg3, argon2-cffi, pytest + testcontainers, ruff, mypy, import-linter. React 18, TypeScript, Vite, TanStack Query, react-router, openapi-typescript + openapi-fetch, Playwright (E2E).

**Plan sequence:** This is Plan 1 of 4. Plan 2 = extraction pipeline (spec §6, §11.3). Plan 3 = search/filters, collections, sharing (§11.4–5). Plan 4 = AI features (§11.6). Later plans are written when their phase starts.

**Spec:** `docs/superpowers/specs/2026-06-12-recipe-normalizer-design.md` — read it before starting. Where this plan and the spec disagree, the spec wins.

**UI quality bar (spec §4):** Frontend tasks (18–22) MUST invoke the `frontend-design:frontend-design` skill and follow it. Design direction locked here so all tasks agree: *editorial cookbook* aesthetic — warm paper-white background (`#FAF7F2`), ink text (`#1C1917`), terracotta accent (`#C2410C`), serif display type (Fraunces) for headings, Inter for body/UI, generous whitespace, hairline rules, no card-shadow grids, no purple, no gradients, no emoji in UI chrome.

---

## Conventions (read once, apply everywhere)

- **Layout:** backend in `src/recipe_normalizer/<module>/`, tests in `tests/<module>/`, frontend in `web/`.
- **Module public API:** other modules may import only `recipe_normalizer.<module>.service` and `recipe_normalizer.<module>.schemas`. Never `models`. import-linter enforces this (Task 4).
- **IDs:** UUIDv4 primary keys (`uuid.uuid4`), `Mapped[uuid.UUID]` columns.
- **Tests:** every DB test runs against real Postgres via the testcontainers fixture from Task 2 (`db_session`). No SQLite, no mocks of the DB.
- **Commits:** after every green test, as written in each task. Conventional-commit style messages.
- **Run commands from the repo root** (`~/recipe-normalizer`). Backend commands are `uv run <cmd>`.

---

### Task 1: Repo scaffold and Python tooling

**Files:**
- Create: `pyproject.toml`, `.gitignore`, `.pre-commit-config.yaml`, `src/recipe_normalizer/__init__.py`, `src/recipe_normalizer/{users,catalog,cookbook}/__init__.py`, `tests/__init__.py`, `tests/conftest.py` (empty for now), `README.md`

- [ ] **Step 1: Create `pyproject.toml`**

```toml
[project]
name = "recipe-normalizer"
version = "0.1.0"
description = "Ingest recipes from anywhere; normalize into one dual-quantity schema"
requires-python = ">=3.12"
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.32",
    "sqlalchemy>=2.0",
    "alembic>=1.14",
    "psycopg[binary]>=3.2",
    "pydantic>=2.9",
    "pydantic-settings>=2.6",
    "argon2-cffi>=23.1",
]

[dependency-groups]
dev = [
    "pytest>=8.3",
    "httpx>=0.27",
    "testcontainers[postgres]>=4.8",
    "ruff>=0.7",
    "mypy>=1.13",
    "import-linter>=2.1",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/recipe_normalizer"]

[tool.ruff]
line-length = 100
src = ["src", "tests"]

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "SIM"]

[tool.mypy]
strict = true
packages = ["recipe_normalizer"]
mypy_path = "src"

[tool.pytest.ini_options]
testpaths = ["tests"]
```

- [ ] **Step 2: Create `.gitignore`** (Python + Node + IDE: `__pycache__/`, `.venv/`, `*.egg-info/`, `.mypy_cache/`, `.ruff_cache/`, `.pytest_cache/`, `node_modules/`, `web/dist/`, `.env`, `.DS_Store`, `filestore/`)

- [ ] **Step 3: Create `.pre-commit-config.yaml`**

```yaml
repos:
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.7.4
    hooks:
      - id: ruff
        args: [--fix]
      - id: ruff-format
```

- [ ] **Step 4: Create package dirs** — `src/recipe_normalizer/__init__.py` and empty `__init__.py` in `users/`, `catalog/`, `cookbook/`; empty `tests/__init__.py` and `tests/conftest.py`; one-paragraph `README.md` (what the project is + `docker compose up` quickstart placeholder to be completed in Task 24).

- [ ] **Step 5: Verify and commit**

Run: `uv sync && uv run ruff check . && uv run python -c "import recipe_normalizer"`
Expected: all succeed.

```bash
git add -A && git commit -m "chore: repo scaffold, python tooling (uv, ruff, mypy, pytest)"
```

---

### Task 2: Database core + testcontainers harness + Alembic

**Files:**
- Create: `src/recipe_normalizer/config.py`, `src/recipe_normalizer/db.py`, `alembic.ini`, `alembic/env.py`, `alembic/versions/` (empty dir)
- Modify: `tests/conftest.py`
- Test: `tests/test_db.py`

- [ ] **Step 1: Write `config.py`**

```python
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RN_")

    database_url: str = "postgresql+psycopg://rn:rn@localhost:5432/rn"
    session_ttl_hours: int = 24 * 30
    file_store_root: str = "./filestore"


settings = Settings()
```

- [ ] **Step 2: Write `db.py`**

```python
import uuid
from collections.abc import Iterator
from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from recipe_normalizer.config import settings


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=datetime.utcnow, onupdate=datetime.utcnow)


def new_uuid() -> uuid.UUID:
    return uuid.uuid4()


engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    with SessionLocal() as session:
        yield session
```

- [ ] **Step 3: Write the testcontainers fixture in `tests/conftest.py`**

```python
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from testcontainers.postgres import PostgresContainer

from recipe_normalizer.db import Base


@pytest.fixture(scope="session")
def pg_url() -> Iterator[str]:
    with PostgresContainer("postgres:16-alpine", driver="psycopg") as pg:
        yield pg.get_connection_url()


@pytest.fixture(scope="session")
def engine(pg_url: str):
    eng = create_engine(pg_url)
    # Import all module models so Base.metadata is complete.
    import recipe_normalizer.users.models  # noqa: F401
    import recipe_normalizer.catalog.models  # noqa: F401
    import recipe_normalizer.cookbook.models  # noqa: F401

    Base.metadata.create_all(eng)
    yield eng


@pytest.fixture()
def db_session(engine) -> Iterator[Session]:
    connection = engine.connect()
    txn = connection.begin()
    session = sessionmaker(bind=connection, expire_on_commit=False)()
    yield session
    session.close()
    txn.rollback()
    connection.close()
```

(The model imports will fail until those modules exist — create empty `models.py` files with just `from recipe_normalizer.db import Base  # noqa: F401` in users/catalog/cookbook now; Tasks 5/8/13 fill them in.)

- [ ] **Step 4: Write the failing test `tests/test_db.py`**

```python
from sqlalchemy import text


def test_postgres_roundtrip(db_session):
    assert db_session.execute(text("select 1")).scalar() == 1
```

Run: `uv run pytest tests/test_db.py -v` — Expected: PASS (Docker must be running). If it fails, fix before continuing.

- [ ] **Step 5: Init Alembic** — `uv run alembic init alembic`; edit `alembic/env.py` to set `target_metadata = Base.metadata` (import `Base` and the three module `models` modules like conftest does) and read the URL from `recipe_normalizer.config.settings.database_url`; set `sqlalchemy.url =` (blank) in `alembic.ini`.

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "feat: db core, alembic, real-postgres test harness"
```

---

### Task 3: docker-compose and Dockerfiles

**Files:**
- Create: `docker-compose.yml`, `docker/api.Dockerfile`, `docker/web.Dockerfile`

- [ ] **Step 1: Write `docker/api.Dockerfile`**

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY alembic ./alembic
COPY alembic.ini ./
RUN uv sync --frozen --no-dev
CMD ["uv", "run", "uvicorn", "recipe_normalizer.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

- [ ] **Step 2: Write `docker/web.Dockerfile`** (multi-stage: `node:22-alpine` build of `web/` → `nginx:alpine` serving `dist/` with `/api` proxied to `api:8000`; include the tiny `nginx.conf` inline via COPY).

- [ ] **Step 3: Write `docker-compose.yml`**

```yaml
services:
  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: rn
      POSTGRES_PASSWORD: rn
      POSTGRES_DB: rn
    volumes: ["pgdata:/var/lib/postgresql/data"]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U rn"]
      interval: 2s
      retries: 30

  api:
    build: { context: ., dockerfile: docker/api.Dockerfile }
    environment:
      RN_DATABASE_URL: postgresql+psycopg://rn:rn@postgres:5432/rn
      RN_FILE_STORE_ROOT: /data/filestore
    volumes: ["filestore:/data/filestore"]
    depends_on:
      postgres: { condition: service_healthy }
    command: sh -c "uv run alembic upgrade head && uv run uvicorn recipe_normalizer.main:app --host 0.0.0.0 --port 8000"
    ports: ["8000:8000"]

  web:
    build: { context: ., dockerfile: docker/web.Dockerfile }
    ports: ["5173:80"]
    depends_on: [api]

volumes:
  pgdata:
  filestore:
```

(`main:app` doesn't exist until Task 17 — compose isn't expected to fully boot until then; that's fine.)

- [ ] **Step 4: Commit**

```bash
git add -A && git commit -m "chore: docker-compose + api/web dockerfiles"
```

---

### Task 4: Module-boundary enforcement + CI

**Files:**
- Create: `.importlinter`, `.github/workflows/ci.yml`

- [ ] **Step 1: Write `.importlinter`**

```ini
[importlinter]
root_package = recipe_normalizer

[importlinter:contract:module-internals]
name = Modules expose only service/schemas
type = forbidden
source_modules =
    recipe_normalizer.users
    recipe_normalizer.catalog
    recipe_normalizer.cookbook
forbidden_modules =
    recipe_normalizer.users.models
    recipe_normalizer.catalog.models
    recipe_normalizer.cookbook.models
ignore_imports =
    recipe_normalizer.users.* -> recipe_normalizer.users.models
    recipe_normalizer.catalog.* -> recipe_normalizer.catalog.models
    recipe_normalizer.cookbook.* -> recipe_normalizer.cookbook.models
```

Run: `uv run lint-imports` — Expected: contracts pass.

- [ ] **Step 2: Write `.github/workflows/ci.yml`** — jobs on push/PR: (a) `uv sync` → `ruff check` → `mypy` → `lint-imports` → `pytest` (ubuntu runner has Docker, so testcontainers works); (b) frontend (added in Task 18): `npm ci && npm run typecheck && npm run generate:client && git diff --exit-code web/src/api/schema.d.ts` — the drift gate from spec §3. Write job (a) fully now; leave job (b) commented with a `# enabled in Task 18` note.

- [ ] **Step 3: Commit**

```bash
git add -A && git commit -m "chore: import-linter module boundaries + CI workflow"
```

---

### Task 5: users — models + migration

**Files:**
- Modify: `src/recipe_normalizer/users/models.py`
- Test: `tests/users/test_models.py`

- [ ] **Step 1: Write the failing test**

```python
import uuid

from recipe_normalizer.users.models import Session as DbSession
from recipe_normalizer.users.models import User


def test_user_and_session_roundtrip(db_session):
    user = User(email="a@b.c", password_hash="x", display_name="A")
    db_session.add(user)
    db_session.flush()
    sess = DbSession(user_id=user.id, token_hash="t" * 64)
    db_session.add(sess)
    db_session.flush()
    assert isinstance(user.id, uuid.UUID)
    assert sess.user_id == user.id
```

Run: `uv run pytest tests/users -v` — Expected: FAIL (models don't exist).

- [ ] **Step 2: Write `users/models.py`**

```python
import uuid
from datetime import datetime, timedelta

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from recipe_normalizer.config import settings
from recipe_normalizer.db import Base, TimestampMixin, new_uuid


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str] = mapped_column(String(100))


def _default_expiry() -> datetime:
    return datetime.utcnow() + timedelta(hours=settings.session_ttl_hours)


class Session(TimestampMixin, Base):
    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(default=_default_expiry)
```

- [ ] **Step 3: Run test → PASS.** `uv run pytest tests/users -v`

- [ ] **Step 4: Generate migration** — start a throwaway Postgres (`docker compose up -d postgres`), then `uv run alembic revision --autogenerate -m "users tables"` and `uv run alembic upgrade head`. Inspect the generated file for sanity.

- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat(users): user + session models, first migration"`

---

### Task 6: users — AuthProvider + service (register/login/logout, TDD)

**Files:**
- Create: `src/recipe_normalizer/users/auth.py`, `src/recipe_normalizer/users/service.py`, `src/recipe_normalizer/users/schemas.py`
- Test: `tests/users/test_service.py`

- [ ] **Step 1: Write the failing tests**

```python
import pytest

from recipe_normalizer.users import service
from recipe_normalizer.users.service import AuthError


def test_register_and_login(db_session):
    user = service.register(db_session, email="ido@x.com", password="hunter22", display_name="Ido")
    assert user.email == "ido@x.com"

    token = service.login(db_session, email="ido@x.com", password="hunter22")
    assert isinstance(token, str) and len(token) >= 32

    current = service.get_user_by_token(db_session, token)
    assert current is not None and current.id == user.id


def test_wrong_password_rejected(db_session):
    service.register(db_session, email="a@x.com", password="rightpass", display_name="A")
    with pytest.raises(AuthError):
        service.login(db_session, email="a@x.com", password="wrongpass")


def test_duplicate_email_rejected(db_session):
    service.register(db_session, email="a@x.com", password="p1234567", display_name="A")
    with pytest.raises(AuthError):
        service.register(db_session, email="a@x.com", password="p7654321", display_name="B")


def test_logout_invalidates_token(db_session):
    service.register(db_session, email="a@x.com", password="p1234567", display_name="A")
    token = service.login(db_session, email="a@x.com", password="p1234567")
    service.logout(db_session, token)
    assert service.get_user_by_token(db_session, token) is None
```

Run: `uv run pytest tests/users/test_service.py -v` — Expected: FAIL (no service module).

- [ ] **Step 2: Write `users/auth.py`** — the `AuthProvider` seam from spec §4/§10:

```python
import hashlib
import secrets
from typing import Protocol

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

_hasher = PasswordHasher()


class AuthProvider(Protocol):
    """v1: email+password. SaaS swaps in OAuth behind this seam."""

    def hash_password(self, password: str) -> str: ...
    def verify_password(self, password: str, password_hash: str) -> bool: ...


class PasswordAuthProvider:
    def hash_password(self, password: str) -> str:
        return _hasher.hash(password)

    def verify_password(self, password: str, password_hash: str) -> bool:
        try:
            return _hasher.verify(password_hash, password)
        except VerifyMismatchError:
            return False


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
```

- [ ] **Step 3: Write `users/service.py`** — `register`, `login` (returns plaintext token, stores sha256), `logout`, `get_user_by_token` (checks expiry). `AuthError(Exception)` with a message; uses `PasswordAuthProvider()` as module default. Also write `users/schemas.py` with Pydantic `UserOut {id, email, display_name}`, `RegisterIn {email: EmailStr, password: str (min 8), display_name}`, `LoginIn`.

- [ ] **Step 4: Run tests → PASS.** `uv run pytest tests/users -v`

- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat(users): auth provider, register/login/logout service"`

---

### Task 7: users — FastAPI router + auth dependency

**Files:**
- Create: `src/recipe_normalizer/users/router.py`, `src/recipe_normalizer/api_deps.py`, `src/recipe_normalizer/errors.py`
- Test: `tests/users/test_router.py`

- [ ] **Step 1: Write `errors.py`** — the consistent error envelope (spec §8): exception handler turning `AuthError` → 401, `LookupError` → 404, validation → 422, everything else → 500, all as `{"error": {"code": str, "message": str}}`.

- [ ] **Step 2: Write `api_deps.py`** — `get_current_user(request, db) -> User`: read `session` HttpOnly cookie, `service.get_user_by_token`, raise 401 envelope if missing/invalid.

- [ ] **Step 3: Write `users/router.py`** — `POST /api/auth/register`, `POST /api/auth/login` (sets `session` cookie: HttpOnly, SameSite=Lax), `POST /api/auth/logout` (clears cookie + invalidates), `GET /api/auth/me` → `UserOut`.

- [ ] **Step 4: Write the failing test `tests/users/test_router.py`** using `fastapi.testclient.TestClient` against a minimal app factory (create `app_for_tests(db_session)` helper in `tests/api_helpers.py` that builds a FastAPI app with the users router and overrides `get_db`): register → login → `/me` returns the user → logout → `/me` returns 401 with the error envelope shape.

- [ ] **Step 5: Run → PASS → Commit** — `git add -A && git commit -m "feat(users): auth routes + cookie session dependency"`

---

### Task 8: catalog — models + migration

**Files:**
- Modify: `src/recipe_normalizer/catalog/models.py`
- Test: `tests/catalog/test_models.py`

- [ ] **Step 1: Write the failing test** — create a `CanonicalIngredient(name="all-purpose flour", category="baking", preferred_measure="mass", status="seeded", dietary_flags=["contains-gluten"], density_g_per_ml=0.53, gram_weights={"cup": 120, "tbsp": 8})` plus two `IngredientAlias` rows (`"AP flour"`/en, `"קמח לבן"`/he); flush; assert alias lookup `ingredient.aliases` has 2 and JSONB fields round-trip.

- [ ] **Step 2: Write `catalog/models.py`**

```python
import enum
import uuid

from sqlalchemy import Enum, Float, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from recipe_normalizer.db import Base, TimestampMixin, new_uuid


class PreferredMeasure(enum.StrEnum):
    mass = "mass"
    volume = "volume"


class IngredientStatus(enum.StrEnum):
    seeded = "seeded"
    unreviewed = "unreviewed"
    reviewed = "reviewed"


class CanonicalIngredient(TimestampMixin, Base):
    __tablename__ = "canonical_ingredients"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    category: Mapped[str] = mapped_column(String(50))
    preferred_measure: Mapped[PreferredMeasure] = mapped_column(Enum(PreferredMeasure))
    status: Mapped[IngredientStatus] = mapped_column(Enum(IngredientStatus))
    dietary_flags: Mapped[list[str]] = mapped_column(JSONB, default=list)
    density_g_per_ml: Mapped[float | None] = mapped_column(Float, nullable=True)
    gram_weights: Mapped[dict[str, float]] = mapped_column(JSONB, default=dict)
    merged_into_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("canonical_ingredients.id"), nullable=True
    )

    aliases: Mapped[list["IngredientAlias"]] = relationship(
        back_populates="ingredient", cascade="all, delete-orphan"
    )


class IngredientAlias(TimestampMixin, Base):
    __tablename__ = "ingredient_aliases"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    ingredient_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("canonical_ingredients.id", ondelete="CASCADE"), index=True
    )
    alias: Mapped[str] = mapped_column(String(200), index=True)
    language: Mapped[str] = mapped_column(String(10))  # BCP-47

    ingredient: Mapped[CanonicalIngredient] = relationship(back_populates="aliases")
```

(`gram_weights` keys are unit tokens from Task 9 — `"cup"`, `"tbsp"`, `"tsp"`, `"unit"` (one whole item, e.g. egg ≈ 50), or count-unit names like `"clove"`.)

- [ ] **Step 3: Run → PASS.** Then autogenerate + apply migration (`alembic revision --autogenerate -m "catalog tables"`, `alembic upgrade head`).

- [ ] **Step 4: Commit** — `git add -A && git commit -m "feat(catalog): canonical ingredient + alias models"`

---

### Task 9: catalog — unit registry and parsing (deterministic, TDD)

**Files:**
- Create: `src/recipe_normalizer/catalog/units.py`
- Test: `tests/catalog/test_units.py`

Spec §5 "Units": deterministic code, never LLM math. Metric, US/imperial, count, bar units, "parts".

- [ ] **Step 1: Write the failing tests**

```python
import pytest

from recipe_normalizer.catalog.units import Unit, UnitKind, parse_unit


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("g", "g"), ("grams", "g"), ("gram", "g"),
        ("kg", "kg"), ("ml", "ml"), ("milliliters", "ml"), ("l", "l"), ("liter", "l"),
        ("cup", "cup"), ("cups", "cup"), ("tbsp", "tbsp"), ("tablespoon", "tbsp"),
        ("tsp", "tsp"), ("teaspoons", "tsp"),
        ("oz", "oz"), ("ounce", "oz"), ("fl oz", "fl_oz"), ("fluid ounce", "fl_oz"),
        ("lb", "lb"), ("pound", "lb"),
        ("jigger", "jigger"), ("dash", "dash"), ("splash", "splash"),
        ("shot", "shot"), ("part", "part"), ("parts", "part"),
        ("clove", "clove"), ("cloves", "clove"), ("slice", "slice"), ("pinch", "pinch"),
    ],
)
def test_parse_unit(text, expected):
    assert parse_unit(text).token == expected


def test_unknown_unit_returns_none():
    assert parse_unit("flibbertigibbets") is None


def test_unit_kinds_and_factors():
    assert parse_unit("kg") == Unit("kg", UnitKind.mass, 1000.0)
    assert parse_unit("cup").kind is UnitKind.volume
    assert abs(parse_unit("cup").base_factor - 236.588) < 0.001
    assert abs(parse_unit("fl oz").base_factor - 29.5735) < 0.001
    assert parse_unit("part").kind is UnitKind.ratio
    assert parse_unit("clove").kind is UnitKind.count
    assert parse_unit("dash").is_inherently_approx
```

Run: `uv run pytest tests/catalog/test_units.py -v` — FAIL.

- [ ] **Step 2: Write `catalog/units.py`**

```python
import enum
from dataclasses import dataclass, field


class UnitKind(enum.StrEnum):
    mass = "mass"      # base: gram
    volume = "volume"  # base: ml
    count = "count"    # needs per-ingredient gram_weights
    ratio = "ratio"    # "parts" — scales natively, never converts


@dataclass(frozen=True)
class Unit:
    token: str
    kind: UnitKind
    base_factor: float = 0.0  # → g (mass) or → ml (volume); 0 for count/ratio
    is_inherently_approx: bool = field(default=False, compare=False)


_UNITS: list[tuple[Unit, list[str]]] = [
    (Unit("g", UnitKind.mass, 1.0), ["g", "gram", "grams", "gr"]),
    (Unit("kg", UnitKind.mass, 1000.0), ["kg", "kilogram", "kilograms"]),
    (Unit("oz", UnitKind.mass, 28.3495), ["oz", "ounce", "ounces"]),
    (Unit("lb", UnitKind.mass, 453.592), ["lb", "lbs", "pound", "pounds"]),
    (Unit("ml", UnitKind.volume, 1.0), ["ml", "milliliter", "milliliters", "millilitre", "millilitres"]),
    (Unit("l", UnitKind.volume, 1000.0), ["l", "liter", "liters", "litre", "litres"]),
    (Unit("tsp", UnitKind.volume, 4.92892), ["tsp", "teaspoon", "teaspoons"]),
    (Unit("tbsp", UnitKind.volume, 14.7868), ["tbsp", "tablespoon", "tablespoons", "tbs"]),
    (Unit("cup", UnitKind.volume, 236.588), ["cup", "cups"]),
    (Unit("fl_oz", UnitKind.volume, 29.5735), ["fl oz", "fluid ounce", "fluid ounces", "fl. oz."]),
    (Unit("jigger", UnitKind.volume, 44.36), ["jigger", "jiggers"]),
    (Unit("shot", UnitKind.volume, 44.36), ["shot", "shots"]),
    (Unit("dash", UnitKind.volume, 0.92, is_inherently_approx=True), ["dash", "dashes"]),
    (Unit("splash", UnitKind.volume, 5.0, is_inherently_approx=True), ["splash", "splashes"]),
    (Unit("pinch", UnitKind.mass, 0.36, is_inherently_approx=True), ["pinch", "pinches"]),
    (Unit("part", UnitKind.ratio), ["part", "parts"]),
    (Unit("clove", UnitKind.count), ["clove", "cloves"]),
    (Unit("slice", UnitKind.count), ["slice", "slices"]),
    (Unit("unit", UnitKind.count), ["unit", "units", "piece", "pieces", "whole"]),
]

_LOOKUP: dict[str, Unit] = {
    alias: unit for unit, aliases in _UNITS for alias in aliases
}


def parse_unit(text: str) -> Unit | None:
    return _LOOKUP.get(text.strip().lower().rstrip("."))
```

- [ ] **Step 3: Run → PASS → Commit** — `git commit -am "feat(catalog): deterministic unit registry + parsing"`

---

### Task 10: catalog — normalization conversion + is_approx (TDD)

**Files:**
- Create: `src/recipe_normalizer/catalog/conversion.py`
- Test: `tests/catalog/test_conversion.py`

Spec §5 rules: exact when source gave weight (solids) / volume (liquids) directly; approximate (flagged) when converted via densities/gram-weights; `None` when unconvertible.

- [ ] **Step 1: Write the failing tests**

```python
from recipe_normalizer.catalog.conversion import Converted, convert_to_normalized


def ing(preferred="mass", density=None, gram_weights=None):
    """Minimal stand-in matching the IngredientConversionData protocol."""
    class _I:
        preferred_measure = preferred
        density_g_per_ml = density
        gram_weights_ = gram_weights or {}
        @property
        def gram_weights(self): return self.gram_weights_
    return _I()


def test_solid_given_in_grams_is_exact():
    assert convert_to_normalized(500, "g", ing("mass")) == Converted(500, "g", is_approx=False)


def test_liquid_given_in_volume_is_exact():
    # 1 oz gin → 30 ml (spec display example rounds 29.57 → 30)
    assert convert_to_normalized(1, "fl_oz", ing("volume")) == Converted(29.57, "ml", is_approx=False)


def test_solid_from_cups_uses_gram_weights_and_flags_approx():
    flour = ing("mass", density=0.53, gram_weights={"cup": 120})
    assert convert_to_normalized(1, "cup", flour) == Converted(120, "g", is_approx=True)


def test_solid_from_volume_falls_back_to_density():
    sugar = ing("mass", density=0.85)
    out = convert_to_normalized(2, "tbsp", sugar)
    assert out.unit == "g" and out.is_approx is True
    assert abs(out.amount - 2 * 14.7868 * 0.85) < 0.01


def test_count_unit_uses_gram_weights():
    egg = ing("mass", gram_weights={"unit": 50})
    assert convert_to_normalized(3, "unit", egg) == Converted(150, "g", is_approx=True)


def test_liquid_given_in_mass_converts_via_density():
    honey = ing("volume", density=1.42)
    out = convert_to_normalized(100, "g", honey)
    assert out.unit == "ml" and out.is_approx is True


def test_unconvertible_returns_none():
    assert convert_to_normalized(None, None, ing("mass")) is None          # "to taste"
    assert convert_to_normalized(1, "part", ing("volume")) is None         # ratio recipes
    assert convert_to_normalized(2, "clove", ing("mass")) is None          # no gram weight known


def test_inherently_approx_units_flagged():
    bitters = ing("volume")
    assert convert_to_normalized(2, "dash", bitters) == Converted(1.84, "ml", is_approx=True)
```

Run: FAIL.

- [ ] **Step 2: Write `catalog/conversion.py`**

```python
from dataclasses import dataclass
from typing import Protocol

from recipe_normalizer.catalog.units import UnitKind, parse_unit


class IngredientConversionData(Protocol):
    preferred_measure: str          # "mass" | "volume"
    density_g_per_ml: float | None
    @property
    def gram_weights(self) -> dict[str, float]: ...


@dataclass(frozen=True)
class Converted:
    amount: float
    unit: str  # "g" | "ml"
    is_approx: bool


def _round2(x: float) -> float:
    return round(x, 2)


def convert_to_normalized(
    quantity: float | None, unit_token: str | None, ingredient: IngredientConversionData
) -> Converted | None:
    if quantity is None or unit_token is None:
        return None
    unit = parse_unit(unit_token)
    if unit is None or unit.kind is UnitKind.ratio:
        return None

    target_mass = ingredient.preferred_measure == "mass"
    density = ingredient.density_g_per_ml

    if unit.kind is UnitKind.count:
        grams_each = ingredient.gram_weights.get(unit.token)
        if grams_each is None:
            return None
        grams = quantity * grams_each
        if target_mass:
            return Converted(_round2(grams), "g", is_approx=True)
        return None if density is None else Converted(_round2(grams / density), "ml", is_approx=True)

    if unit.kind is UnitKind.mass:
        grams = quantity * unit.base_factor
        if target_mass:
            return Converted(_round2(grams), "g", is_approx=unit.is_inherently_approx)
        return None if density is None else Converted(_round2(grams / density), "ml", is_approx=True)

    # volume
    ml = quantity * unit.base_factor
    if not target_mass:
        return Converted(_round2(ml), "ml", is_approx=unit.is_inherently_approx)
    grams_per_unit = ingredient.gram_weights.get(unit.token)
    if grams_per_unit is not None:
        return Converted(_round2(quantity * grams_per_unit), "g", is_approx=True)
    if density is not None:
        return Converted(_round2(ml * density), "g", is_approx=True)
    return None
```

- [ ] **Step 3: Run → PASS → Commit** — `git commit -am "feat(catalog): dual-quantity normalization conversion with is_approx rules"`

---

### Task 11: catalog — seed data + loader

**Files:**
- Create: `src/recipe_normalizer/catalog/seed/ingredients.json`, `src/recipe_normalizer/catalog/seed/__init__.py`, `src/recipe_normalizer/catalog/seed_loader.py`
- Test: `tests/catalog/test_seed.py`

- [ ] **Step 1: Write the seed file schema + entries.** JSON array; entry shape:

```json
{
  "name": "all-purpose flour",
  "aliases": [{"alias": "AP flour", "language": "en"}, {"alias": "plain flour", "language": "en"}, {"alias": "קמח לבן", "language": "he"}, {"alias": "קמח", "language": "he"}],
  "category": "baking",
  "preferred_measure": "mass",
  "dietary_flags": ["contains-gluten"],
  "density_g_per_ml": 0.53,
  "gram_weights": {"cup": 120, "tbsp": 8, "tsp": 2.6}
}
```

Author **≥200 entries** covering: baking (flours, sugars, butter, eggs `{"unit": 50}`, yeast, baking soda/powder, cocoa, chocolate), produce (onion `{"unit": 110}`, garlic `{"clove": 5}`, tomatoes, common vegetables/fruits/herbs), dairy, meat/fish (incl. **ground beef** per the spec's granularity example), grains/pasta/legumes, oils/vinegars, spices, condiments, liquids (water, stock, milk, juices — `preferred_measure: volume`), and bar staples (gin, vodka, rum, tequila, whiskey, dry/sweet vermouth, Campari, triple sec, simple syrup, lime/lemon juice, Angostura bitters, club soda — all `volume`, density ≈ 0.94–1.3 for syrups/juices, ~0.95 for spirits). Hebrew aliases for the ~40 most common household ingredients. Every entry has at least one of `density_g_per_ml`/`gram_weights` unless genuinely senseless ("bay leaf" → `gram_weights: {"unit": 0.2}` is fine; sparse is allowed per spec).

- [ ] **Step 2: Write `seed_loader.py`** — `load_seed(session) -> int`: read the JSON (via `importlib.resources`), upsert by `name` (skip existing), insert aliases, `status="seeded"`; return count inserted. Also register as a CLI: `uv run python -m recipe_normalizer.catalog.seed_loader`.

- [ ] **Step 3: Write the failing test** — `load_seed` inserts ≥200 ingredients; loading twice doesn't duplicate; "AP flour" alias resolves; "ground beef" exists with `preferred_measure == "mass"`; "gin" exists with `"volume"` and `"alcohol"` in dietary_flags; every entry passes `parse_unit` validation on its `gram_weights` keys (i.e. keys are known unit tokens or `"unit"`).

- [ ] **Step 4: Run → PASS → Commit** — `git add -A && git commit -m "feat(catalog): 200-ingredient seed data + loader"`

---

### Task 12: catalog — service (alias matching, create-unreviewed, merge) + router

**Files:**
- Create: `src/recipe_normalizer/catalog/service.py`, `src/recipe_normalizer/catalog/schemas.py`, `src/recipe_normalizer/catalog/router.py`
- Test: `tests/catalog/test_service.py`

- [ ] **Step 1: Write the failing tests** — using seeded data via `load_seed(db_session)`:
  - `match("AP flour")` → all-purpose flour (case-insensitive exact alias)
  - `match("קמח")` → all-purpose flour (multilingual)
  - `match("zzgremlin dust")` → `None`
  - `create_unreviewed(db, name="gremlin dust")` → status `unreviewed`, name becomes its own alias
  - `merge(db, source_id, target_id)` — source's aliases move to target, source gets `merged_into_id` set and is excluded from future matches; (re-pointing cookbook ingredient lines is wired in Task 15's service via `catalog.service.merge` hook — for now merge takes an optional `on_merge` callback; Task 15 registers it)
  - `search(db, "flou")` → prefix/substring match over names+aliases for the admin UI
- [ ] **Step 2: Implement `service.py` + `schemas.py`** (`IngredientOut`, `IngredientDetail`, `MergeIn`). Matching = lowercase/strip exact alias or name lookup; substring search via `ILIKE`. (Fuzzy/LLM matching is Plan 2 — extraction needs it; YAGNI here.)
- [ ] **Step 3: Write `router.py`** — `GET /api/catalog/ingredients?q=&status=`, `GET /api/catalog/ingredients/{id}`, `POST /api/catalog/ingredients/{id}/merge` (body: `target_id`), `PATCH /api/catalog/ingredients/{id}` (name/category/flags/density/gram_weights/status → `reviewed`). All require auth (global catalog — any user, per spec §5).
- [ ] **Step 4: Run → PASS → Commit** — `git commit -am "feat(catalog): matching service, merge, admin routes"`

---

### Task 13: cookbook — models + migration

**Files:**
- Modify: `src/recipe_normalizer/cookbook/models.py`
- Test: `tests/cookbook/test_models.py`

- [ ] **Step 1: Write the failing test** — build a full recipe aggregate (recipe → 2 groups → 3 lines, 2 steps, one cuisine + one dish_type + one tag from vocab tables), flush, reload, assert ordering preserved (`order_index`) and cascade delete removes children.

- [ ] **Step 2: Write `cookbook/models.py`** per spec §5. Tables:
  - `recipes`: id, owner_id (FK users.id), schema_version (int, default 1), title, description, image_ref (str|None), source (str|None), source_type (enum `web|pdf|image|text|manual`), language (str, BCP-47), servings_amount (float|None), servings_unit_text (str|None), prep_min/cook_min/total_min (int|None), extraction_meta (JSONB|None), is_verified (bool, default False), provenance (JSONB|None), derived_from (FK recipes.id|None), last_edited_by (FK users.id|None), last_edited_at (datetime|None), **`source_fingerprint` (str|None, indexed)** — normalized-URL or file content hash for the §6.4 duplicate check (unique per `(owner_id, source_fingerprint)` partial index where not null)
  - `ingredient_groups`: id, recipe_id FK cascade, name (str|None), order_index (int)
  - `ingredient_lines`: id, group_id FK cascade, order_index, original_text, quantity (Numeric|None), unit (str|None), canonical_ingredient_id (FK catalog, nullable, `ondelete="SET NULL"`), normalized_amount (float|None), normalized_unit (str|None), is_approx (bool, default False), note (str|None), is_optional (bool, default False)
  - `steps`: id, recipe_id FK cascade, order_index, original_text, ingredient_line_refs (JSONB list of line ids, default list)
  - Vocab tables `cuisines`, `dish_types`, `tags` (id, name unique) + association tables `recipe_cuisines`, `recipe_dish_types`, `recipe_tags`. Seed `dish_types` with `cocktail, drink, smoothie, main, dessert, side, breakfast, soup, salad, bread, sauce, snack` in the migration (spec §5: seedable vocab tables, includes drinks).

- [ ] **Step 3: Run → PASS.** Autogenerate + apply migration. Inspect the partial unique index made it in (add manually via `op.create_index(..., postgresql_where=...)` if autogenerate missed it).

- [ ] **Step 4: Commit** — `git add -A && git commit -m "feat(cookbook): recipe aggregate models + vocab tables"`

---

### Task 14: cookbook — schemas (the API/normalize-shared recipe shape)

**Files:**
- Create: `src/recipe_normalizer/cookbook/schemas.py`
- Test: `tests/cookbook/test_schemas.py`

- [ ] **Step 1: Write Pydantic DTOs** mirroring spec §5: `IngredientLineIn/Out` (Out adds `display: str` — see below), `IngredientGroupIn/Out`, `StepIn/Out`, `RecipeIn` (manual entry / extraction both use this), `RecipeOut`, `RecipeSummary` (id, title, image_ref, dish_types, total_min, is_verified — for the browse grid). `RecipeOut.servings` as `{"amount": float|None, "unit_text": str|None}`.

  `IngredientLineOut.display` implements the spec's hard display rule, built server-side so every client renders identically:

```python
def build_display(line) -> str:
    """'1 cup flour → ~120 g (approx.)' | original-only when no normalization."""
    if line.normalized_amount is None:
        return line.original_text
    amount = format_amount(line.normalized_amount)  # trims trailing zeros
    prefix = "~" if line.is_approx else ""
    suffix = " (approx.)" if line.is_approx else ""
    return f"{line.original_text} → {prefix}{amount} {line.normalized_unit}{suffix}"
```

- [ ] **Step 2: Test** — `build_display` for: approx (`"1 cup flour → ~120 g (approx.)"`), exact (`"1 oz gin → 29.57 ml"`), unconvertible (`"salt to taste"` unchanged). Round-trip `RecipeIn` → model fields.
- [ ] **Step 3: Run → PASS → Commit** — `git commit -am "feat(cookbook): recipe DTOs + dual-quantity display rule"`

---

### Task 15: cookbook — service CRUD + catalog wiring (TDD)

**Files:**
- Create: `src/recipe_normalizer/cookbook/service.py`
- Test: `tests/cookbook/test_service.py`

- [ ] **Step 1: Write the failing tests**
  - `create_recipe(db, owner_id, RecipeIn)` — manual entry: every line gets catalog-matched (`catalog.service.match`) and normalized (`convert_to_normalized`); unmatched names create `unreviewed` canonical ingredients (spec §6.3.3 applies to manual entry too); returns `RecipeOut` with `display` strings correct
  - `get_recipe` enforces `owner_id` (LookupError for someone else's recipe)
  - `list_recipes(db, owner_id)` returns summaries, newest first
  - `update_recipe` sets `last_edited_by/last_edited_at`, re-runs match+normalize on changed lines
  - `delete_recipe` cascades
  - duplicate `source_fingerprint` for same owner raises `DuplicateRecipeError(existing_id)` — different owner is fine
  - catalog merge re-points lines: create recipe with "gremlin dust" (auto-created unreviewed), merge it into "all-purpose flour", reload recipe → line's `canonical_ingredient_id` is flour's id (uses the `on_merge` callback registered by cookbook at import/app-startup time: `catalog.service.register_merge_hook(cookbook.service.repoint_ingredient_lines)`)
- [ ] **Step 2: Implement `service.py`.** Keep it the only writer of cookbook tables. `repoint_ingredient_lines(db, source_id, target_id)` is a single UPDATE.
- [ ] **Step 3: Run → PASS → Commit** — `git commit -am "feat(cookbook): recipe CRUD service, catalog matching, merge re-pointing, dedupe"`

---

### Task 16: cookbook — scaling (deterministic, TDD)

**Files:**
- Create: `src/recipe_normalizer/cookbook/scaling.py`
- Test: `tests/cookbook/test_scaling.py`

Spec §7 Scaling: view-only, by factor or target servings; normalized amounts scale exactly; original quantities re-render as sensible fractions; unconvertible lines pass through flagged; "parts" scale natively.

- [ ] **Step 1: Write the failing tests**

```python
from fractions import Fraction

from recipe_normalizer.cookbook.scaling import format_quantity, scale_factor_for, scale_line


def test_scale_factor_by_target_servings():
    assert scale_factor_for(base_servings=4, target_servings=6) == 1.5


@pytest.mark.parametrize(
    ("value", "expected"),
    [(2.25, "2¼"), (0.5, "½"), (0.333, "⅓"), (1.0, "1"), (2.6666, "2⅔"), (3.875, "3⅞"), (0.125, "⅛")],
)
def test_format_quantity_fractions(value, expected):
    assert format_quantity(value) == expected


def test_scaled_line_doubles_normalized_and_reformats_original_quantity():
    line = make_line(quantity=1.125, unit="cup", normalized_amount=135, is_approx=True)
    out = scale_line(line, 2.0)
    assert out.quantity_display == "2¼"          # 1.125 * 2, as fraction
    assert out.normalized_amount == 270
    assert out.passes_through is False


def test_unconvertible_line_passes_through_flagged():
    line = make_line(quantity=None, unit=None, normalized_amount=None, original_text="salt to taste")
    out = scale_line(line, 3.0)
    assert out.passes_through is True


def test_parts_scale_natively():
    line = make_line(quantity=2, unit="part", normalized_amount=None)
    out = scale_line(line, 1.5)
    assert out.quantity_display == "3" and out.passes_through is False
```

(`make_line` = small test helper constructing an `IngredientLineOut`.)

- [ ] **Step 2: Implement `scaling.py`** — `format_quantity` snaps to nearest of denominators {2,3,4,8} when within 0.03, else one decimal; unicode vulgar fractions (⅛¼⅓⅜½⅝⅔¾⅞). `scale_recipe(recipe_out, factor) -> ScaledRecipeOut` maps every line through `scale_line`, scales `servings.amount`, carries a `step_text_disclaimer: True` field (spec §7 known limitation — UI shows it). Service/router expose `GET /api/recipes/{id}/scaled?factor=` or `?target_servings=` (added in Task 17's router).
- [ ] **Step 3: Run → PASS → Commit** — `git commit -am "feat(cookbook): deterministic scaling with fraction formatting"`

---

### Task 17: API assembly — app factory, cookbook router, OpenAPI export

**Files:**
- Create: `src/recipe_normalizer/main.py`, `src/recipe_normalizer/cookbook/router.py`, `scripts/export_openapi.py`
- Test: `tests/test_app.py`

- [ ] **Step 1: Write `cookbook/router.py`** — auth-required: `POST /api/recipes` (manual entry; 409 + existing_id on `DuplicateRecipeError`), `GET /api/recipes`, `GET /api/recipes/{id}`, `PATCH /api/recipes/{id}`, `DELETE /api/recipes/{id}`, `GET /api/recipes/{id}/scaled`, `GET /api/vocab` (cuisines/dish_types/tags for form dropdowns).
- [ ] **Step 2: Write `main.py`** — `create_app()`: include users/catalog/cookbook routers, install the Task 7 error handlers, run `catalog.service.register_merge_hook(cookbook.service.repoint_ingredient_lines)`, CORS for `localhost:5173` (dev), `/api/health`. Module-level `app = create_app()`.
- [ ] **Step 3: Write `scripts/export_openapi.py`** — dumps `app.openapi()` JSON to `web/openapi.json` (input for client generation).
- [ ] **Step 4: Test `tests/test_app.py`** — TestClient full flow: register → login → create recipe (flour + gin lines) → list → get (assert `display` strings) → scaled?factor=2 (assert doubled) → duplicate create returns 409 envelope.
- [ ] **Step 5: Run all backend gates** — `uv run pytest && uv run mypy && uv run lint-imports && uv run ruff check .` → all green → Commit — `git commit -am "feat(api): app factory, recipe routes, openapi export"`

---

### Task 18: web — scaffold + typed client + CI drift gate

**Files:**
- Create: `web/` (Vite React-TS scaffold), `web/src/api/client.ts`, `web/package.json` scripts
- Modify: `.github/workflows/ci.yml` (enable job b)

- [ ] **Step 1: Scaffold** — `npm create vite@latest web -- --template react-ts`; add deps: `@tanstack/react-query`, `react-router-dom`, `openapi-fetch`; dev deps: `openapi-typescript`, `prettier`. Vite dev proxy `/api` → `http://localhost:8000`.
- [ ] **Step 2: Client generation** — scripts: `"generate:client": "openapi-typescript ../web/openapi.json -o src/api/schema.d.ts"` (preceded by `uv run python scripts/export_openapi.py` via a root `Makefile` target `make client`). `web/src/api/client.ts`:

```ts
import createClient from "openapi-fetch";
import type { paths } from "./schema";

export const api = createClient<paths>({ baseUrl: "/", credentials: "include" });
```

- [ ] **Step 3: Enable CI job (b)** from Task 4 (typecheck + regenerate + `git diff --exit-code web/src/api/schema.d.ts`).
- [ ] **Step 4: Verify** — `npm run typecheck` green; commit — `git commit -am "feat(web): vite scaffold + generated typed api client + drift gate"`

---

### Task 19: web — design system + app shell + auth pages

**REQUIRED SUB-SKILL: invoke `frontend-design:frontend-design` before writing any UI code in Tasks 19–22.** Apply the locked design direction from the plan header (editorial cookbook: paper `#FAF7F2`, ink `#1C1917`, terracotta `#C2410C`, Fraunces display + Inter body, hairline rules, no gradients/emoji).

**Files:**
- Create: `web/src/styles/tokens.css` (CSS custom properties for the palette/type/spacing scale), `web/src/components/` (Button, Input, Field, PageHeader, EmptyState), `web/src/layouts/AppShell.tsx` (top nav: Cookbook / Inbox / Catalog / Shares — Inbox/Shares as disabled "soon" entries until Plans 2–3), `web/src/pages/{LoginPage,RegisterPage}.tsx`, router + QueryClient setup in `main.tsx`, auth context hook `useUser` (`/api/auth/me`, redirect to login on 401)

- [ ] **Step 1: Invoke frontend-design skill; build tokens + primitives.** Fonts self-hosted via `@fontsource/fraunces` + `@fontsource/inter` (no runtime Google Fonts).
- [ ] **Step 2: Build AppShell + auth pages** wired to the typed client (`api.POST("/api/auth/login", ...)`).
- [ ] **Step 3: Verify in browser** — `uv run uvicorn recipe_normalizer.main:app` + `npm run dev`; register, log in, see the shell. Screenshot to confirm the design bar (no default-Vite look).
- [ ] **Step 4: Commit** — `git commit -am "feat(web): design tokens, app shell, auth pages"`

---

### Task 20: web — manual recipe entry

**Files:**
- Create: `web/src/pages/RecipeEditorPage.tsx`, `web/src/components/recipe/{IngredientGroupEditor,IngredientLineRow,StepListEditor,ServingsInput,VocabMultiSelect}.tsx`

- [ ] **Step 1: Build the form** — title/description; servings (amount + free-text unit); times; cuisines/dish-types/tags multi-selects fed by `GET /api/vocab`; ingredient groups (add/remove/reorder; each line = single free-text input `original_text` plus optional structured quantity/unit/note/is_optional fields); steps list (add/remove/reorder); optional image upload deferred to Plan 2's FileStore — omit for now, `image_ref` stays null for manual recipes (spec: manual entry *may* attach one; acceptable to defer alongside FileStore).
- [ ] **Step 2: Submit → `POST /api/recipes`** → navigate to detail. Surface 409 duplicate as a link to the existing recipe.
- [ ] **Step 3: Manual browser verification** with a real recipe (use one with flour-by-cups and "salt to taste" to exercise approx + pass-through), then commit — `git commit -am "feat(web): manual recipe entry"`

---

### Task 21: web — cookbook browse page

**Files:**
- Create: `web/src/pages/CookbookPage.tsx`, `web/src/components/recipe/RecipeCard.tsx`

- [ ] **Step 1: Build the grid** — `GET /api/recipes` summaries; editorial cards (image when present, else a typographic placeholder with the recipe's initial — design it, don't default it); dish-type label, total time, unverified badge for `is_verified=false`. Search/filters land in Plan 3; v1 of this page is the grid + "Add recipe" action.
- [ ] **Step 2: Verify, commit** — `git commit -am "feat(web): cookbook browse grid"`

---

### Task 22: web — recipe detail with dual quantities + scaler

**Files:**
- Create: `web/src/pages/RecipeDetailPage.tsx`, `web/src/components/recipe/{IngredientList,ScaleControl,StepList}.tsx`

- [ ] **Step 1: Build the page** — header (title, servings, times, tags); ingredient groups rendering the server-built `display` string per line (the hard display rule — original always visible); steps; scale control (×½ ×1 ×2 ×3 presets + free factor + "for N servings") calling `GET /api/recipes/{id}/scaled` and swapping in scaled lines; when scaled ≠ 1: show the step-text disclaimer banner (spec §7 known limitation) and flag pass-through lines ("doesn't scale").
- [ ] **Step 2: Verify in browser** with the Task 20 recipe at ×2 and "for 6 servings"; commit — `git commit -am "feat(web): recipe detail, dual quantities, scaling view"`

---

### Task 23: E2E happy path (Playwright)

**Files:**
- Create: `e2e/` (Playwright TS project), `e2e/happy-path.spec.ts`

- [ ] **Step 1: Set up Playwright** (`npm init playwright@latest e2e`), `webServer` config booting api (uvicorn against compose Postgres with a fresh schema + seed) and `npm run dev`.
- [ ] **Step 2: Write the spec** — register → log in → add manual recipe (2 groups, flour by cups, salt to taste) → cookbook grid shows it → open detail → assert a line matching `→ ~\d+ g \(approx\.\)` → scale ×2 → assert doubled quantity and disclaimer visible → log out.
- [ ] **Step 3: Run green, add `e2e` job to CI, commit** — `git commit -am "test: e2e happy path (register→entry→browse→scale)"`

---

### Task 24: compose-up verification + README

- [ ] **Step 1:** `docker compose up --build` from scratch (fresh volumes); run migrations + seed (`docker compose exec api uv run python -m recipe_normalizer.catalog.seed_loader`); walk the happy path manually at `localhost:5173`.
- [ ] **Step 2:** Complete `README.md`: what it is, architecture sketch (module table from spec §4), quickstart (`docker compose up`, seed command), dev setup (uv, npm, `make client`), test commands, link to spec + this plan, roadmap section listing the spec's verbatim non-goals (spec §2 requires this).
- [ ] **Step 3:** Final gate: `uv run pytest && uv run mypy && uv run lint-imports && uv run ruff check . && (cd web && npm run typecheck)` all green. Commit — `git commit -am "docs: readme, compose-up verified"`

---

## Self-review notes (kept for the executor)

- **Spec coverage for phases 1–2:** users/auth (T5–7), catalog + seed + units + densities (T8–12), recipe model incl. image_ref/last_edited_by/source_fingerprint (T13), dual-quantity display rule (T14), manual entry + auto catalog-matching (T15, T20), scaling incl. parts/fractions/disclaimer (T16, T22), module boundaries in CI (T4), OpenAPI drift gate (T18), UI quality bar (T19–22), E2E (T23). Deferred intentionally: FileStore + image upload (Plan 2, with extraction which needs it anyway), fuzzy/LLM ingredient matching (Plan 2), search/filters/collections (Plan 3), worker/job queue (Plan 2).
- **Type consistency:** `convert_to_normalized(quantity, unit_token, ingredient)` (T10) is what `cookbook.service` calls (T15); `Converted.amount/unit/is_approx` map to `normalized_amount/normalized_unit/is_approx` columns (T13); `catalog.service.match/register_merge_hook` names used in T12, T15, T17.
