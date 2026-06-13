# Recipe Normalizer

Recipe Normalizer ingests recipes from anywhere — URLs, PDFs, images, pasted text, manual entry — and normalizes them into one unified structure with **dual quantities**: the original text is always preserved and displayed alongside a normalized weight/volume (grams for solids, ml for liquids), with approximations flagged. Users build a personal, filterable cookbook; scale recipes deterministically; share recipes and cookbooks; and ask AI questions grounded in their recipes. Scope covers all recipes: food, baking, cocktails, smoothies.

**v1 status:** this repo currently ships the foundation — accounts, the global ingredient catalog with deterministic unit conversion, manual recipe entry, the cookbook with dual-quantity rendering, and scaling. Extraction (URL/PDF/image), search/collections/sharing, and AI features are the next plans (see [Roadmap](#roadmap)).

## Quickstart

Requires Docker.

```sh
docker compose up -d --build
docker compose exec api uv run python -m recipe_normalizer.catalog.seed_loader
```

Open http://localhost:5173 — register an account, add a recipe, scale it. Migrations run automatically when the `api` container starts; the seed command loads ~240 canonical ingredients with density data.

## Architecture

Modular monolith: one repo, one FastAPI app, backend modules under `src/recipe_normalizer/`. Each module owns its tables and exposes a typed service interface; cross-module access happens only through `service.py`/`schemas.py`, enforced by import-linter in CI.

| Module | Owns | Responsibility | Status |
|---|---|---|---|
| `users` | users, sessions | Accounts. Auth behind an `AuthProvider` interface: v1 = email+password (cookie sessions); SaaS swaps in OAuth without touching other modules. | shipped |
| `catalog` | canonical ingredients, aliases, densities, units | Canonical ingredient entities, multilingual alias matching, deterministic unit conversion math. | shipped |
| `cookbook` | recipes, ingredient lines, steps | Recipe CRUD, dual-quantity rendering, deterministic scaling. | shipped |
| `ingestion` | inputs, jobs | Accept any input (URL / file / pasted text), extraction job lifecycle + review gate. | shipped |
| `extraction` | (stateless) | The acquire+normalize pipeline; pluggable `Extractor` per source type, incl. tiered agentic web extraction. | shipped |
| `sharing` | shares, shared cookbooks, public links | Copy-on-share, co-owned cookbooks, tokenized public links. | planned (Plan 3) |
| `ai` | conversations, recommendations | Per-recipe chat, cookbook Q&A, transformations, recommendations. | planned (Plan 4) |

Key invariants:

- **Dual quantities (display rule):** every ingredient line renders original **and** normalized, e.g. `1 cup flour → ~120 g (approx.)`, `1 oz gin → 30 ml`; the original is never hidden, and unconvertible lines ("salt to taste") display as original only.
- **Deterministic conversion & scaling:** unit parsing, conversion, and scaling are code in `catalog`/`cookbook` — never LLM math; scaling is a view, never a mutation.
- **Global catalog:** canonical ingredients, aliases, and densities are shared by all users; alias/density/review work benefits everyone.

Frontend is React + TypeScript (Vite, TanStack Query) in `web/`, talking to the API through a typed client generated from the OpenAPI schema (drift fails CI). The production deployment serves the built bundle via nginx, which proxies `/api/` to the FastAPI container (`docker/`).

## Development

Backend (Python 3.12+, [uv](https://docs.astral.sh/uv/), Postgres via Docker):

```sh
uv sync                                                      # install deps
docker compose up -d postgres                                # local Postgres on :5432
uv run alembic upgrade head                                  # migrations
uv run python -m recipe_normalizer.catalog.seed_loader       # seed the catalog
uv run uvicorn recipe_normalizer.main:app --reload --port 8000
```

Extraction worker (claims ingestion jobs from the Postgres queue and runs the
tiered acquire → normalize pipeline):

```sh
uv run playwright install chromium    # one-time: tier-3 agentic browser (URL extraction)
export ANTHROPIC_API_KEY=sk-ant-...   # LLM normalize / tier-2 judge / tier-3 browser
uv run python -m recipe_normalizer.worker
```

The worker runs as its own process (see the `worker` service in `docker-compose.yml`). Tier 3 drives a headless Chromium via Playwright; without `playwright install chromium` it is unavailable and URL jobs that need it fail gracefully with the tier-2 reason. The job-level LLM spend is bounded by `RN_JOB_COST_CAP_USD` (default $1.50).

Ingestion quickstart: with the api and worker both running, open the **Inbox** in the web app and paste a recipe URL, drop a PDF/image, or paste text. Each submission becomes a job that the worker extracts in the background; finished jobs land in the inbox as `NEEDS REVIEW`, where the **Review** screen shows the source beside the editable draft. Accept moves the recipe into your cookbook. No API key? Run the worker with `RN_LLM_STUB=1` to exercise the full pipeline with a canned extraction (test/e2e only).

Configuration is via `RN_`-prefixed env vars (`src/recipe_normalizer/config.py`); the defaults match the compose Postgres (`postgresql+psycopg://rn:rn@localhost:5432/rn`).

Frontend:

```sh
cd web
npm install
npm run dev        # Vite dev server on :5173, proxies /api to :8000
```

After any backend API change, regenerate the typed client and commit the result:

```sh
make client        # exports OpenAPI schema, regenerates web/src/api/schema.d.ts
```

Tests:

```sh
uv run pytest -q              # unit/integration tests — real Postgres via testcontainers (Docker required)
cd e2e && npx playwright test # E2E happy path (boots backend + frontend itself)
```

CI gates (all must pass): `ruff check`, `mypy`, `lint-imports` (module boundaries), `pytest`, frontend `typecheck`, generated-client drift check, and Playwright E2E.

## Roadmap

**Non-goals (v1), verbatim from the spec:** Nutrition data; meal planning; cook-mode (step-by-step cooking screen); user photo uploads of finished dishes; video/social inputs (YouTube, Instagram); email-in ingestion; voice-memo ingestion; collaborative-filtering recommendations; mobile apps; billing/multi-tenant SaaS infra (architecture must allow it; do not build it).

Planned phases:

- **Plan 2 — extraction pipeline (shipped):** ingestion jobs + worker; text/paste → normalize → review screen; URL tiers 1–2 (structured data, readable HTML) + tier 3 agentic browser; PDF (text layer + scanned vision); images.
- **Plan 3 — search, collections, sharing:** filters and full-text search, collections, copy-on-share, public links, shared cookbooks.
- **Plan 4 — AI features:** per-recipe chat, cookbook Q&A, transformations, content-based recommendations.

## Documents

- Spec: [`docs/superpowers/specs/2026-06-12-recipe-normalizer-design.md`](docs/superpowers/specs/2026-06-12-recipe-normalizer-design.md)
- Plan 1 (this codebase): [`docs/superpowers/plans/2026-06-12-plan-1-foundation-domain-core.md`](docs/superpowers/plans/2026-06-12-plan-1-foundation-domain-core.md)
