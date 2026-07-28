# Recipe Normalizer

Recipe Normalizer ingests recipes from anywhere — URLs, PDFs, images, pasted text, manual entry — and normalizes them into one unified structure with **dual quantities**: the original text is always preserved and displayed alongside a normalized weight/volume (grams for solids, ml for liquids), with approximations flagged. Users build a personal, filterable cookbook; scale recipes deterministically; share recipes and cookbooks; and ask AI questions grounded in their recipes. Scope covers all recipes: food, baking, cocktails, smoothies.

**v1 status:** all six phases have shipped — accounts, the global ingredient catalog with deterministic unit conversion, ingestion from URL/PDF/image/text/manual with a review gate, cookbooks with dual-quantity rendering and scaling, search/favorites, sharing (copy-on-share, public links), and the AI layer (recipe chat, cookbook Q&A, transformations, recommendations). Cookbooks are boards: a recipe can live in several at once, and the standalone "collections" feature has been folded into them. See [Roadmap](#roadmap) for non-goals.

## Quickstart

Requires Docker.

```sh
docker compose up -d --build
```

Open http://localhost:5173 — register an account, add a recipe, scale it. Migrations run and the ingredient catalog (~240 canonical ingredients with density data) is seeded automatically when the `api` container starts. For AI features and URL/PDF/image extraction, set an LLM key first (see [LLM providers](#llm-providers)): `OPENROUTER_API_KEY=sk-or-... docker compose up -d --build`.

## Architecture

Modular monolith: one repo, one FastAPI app, backend modules under `src/recipe_normalizer/`. Each module owns its tables and exposes a typed service interface; cross-module access happens only through `service.py`/`schemas.py`, enforced by import-linter in CI.

| Module | Owns | Responsibility | Status |
|---|---|---|---|
| `users` | users, sessions | Accounts. Auth behind an `AuthProvider` interface: v1 = email+password (cookie sessions); SaaS swaps in OAuth without touching other modules. | shipped |
| `catalog` | canonical ingredients, aliases, densities, units | Canonical ingredient entities, multilingual alias matching, deterministic unit conversion math. | shipped |
| `cookbook` | cookbooks, memberships, recipe↔cookbook placements, recipes, ingredient lines, steps | Cookbooks (private/unlisted/public, owner + editor/viewer members) holding recipes; recipe CRUD, dual-quantity rendering, deterministic scaling. A recipe may be placed in **many** cookbooks at once, and access is the highest level the caller holds across all of them. | shipped |
| `ingestion` | inputs, jobs | Accept any input (URL / file / pasted text), extraction job lifecycle + review gate. | shipped |
| `extraction` | (stateless) | The acquire+normalize pipeline; pluggable `Extractor` per source type, incl. tiered agentic web extraction. | shipped |
| `sharing` | shares, public links | Copy-on-share, tokenized public links to a single recipe. (Co-owned cookbooks moved to `cookbook` in the cookbooks pivot.) | shipped |
| `ai` | conversations, recommendations | Per-recipe chat, cookbook Q&A, transformations, recommendations. | shipped |

Key invariants:

- **Dual quantities (display rule):** every ingredient line renders original **and** normalized, e.g. `1 cup flour → ~120 g (approx.)`, `1 oz gin → 30 ml`; the original is never hidden, and unconvertible lines ("salt to taste") display as original only.
- **Deterministic conversion & scaling:** unit parsing, conversion, and scaling are code in `catalog`/`cookbook` — never LLM math; scaling is a view, never a mutation.
- **Cookbooks as boards:** the `cookbook_recipes` join is the only record of containment. A recipe is always in **at least one** cookbook (enforced in the service — removing the last placement is refused), and access derives from the whole set, plus an always-owner path through `recipes.owner_id`. Deleting a cookbook removes *placements*, never recipes: one placed solely there is re-filed into its owner's default cookbook first. Deleting the recipe is a separate, owner-only action.
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
export OPENROUTER_API_KEY=sk-or-...   # LLM normalize / tier-2 judge / tier-3 browser
uv run python -m recipe_normalizer.worker
```

### LLM providers

Every LLM call flows through `src/recipe_normalizer/llm/` and supports two providers,
selected by `RN_LLM_PROVIDER` (`auto` by default — picks whichever API key is present,
preferring OpenRouter):

- **OpenRouter** (default, free-tier friendly): set `OPENROUTER_API_KEY`
  (free keys at <https://openrouter.ai/keys>). Default model is `openrouter/free`,
  OpenRouter's auto-router over currently-free models (vision + tools + JSON schema),
  so it keeps working as the free lineup rotates. Pin a specific model with
  `RN_LLM_MODEL` / `RN_LLM_FAST_MODEL` (e.g. `google/gemma-4-31b-it:free`; note the
  main model must support **image input** or scanned-PDF/photo extraction breaks —
  text-only picks like `nvidia/nemotron-3-super-120b-a12b:free` only suit
  `RN_LLM_FAST_MODEL`). Free-tier limits: ~50 requests/day (1000/day after a
  one-time $10 credit purchase); expect occasional 429s — the client retries with backoff.
  Cost tracking uses OpenRouter's own accounting ($0 for `:free` models).
- **Anthropic**: set `ANTHROPIC_API_KEY` (and optionally `RN_LLM_PROVIDER=anthropic`).
  Defaults to `claude-opus-4-8` with `claude-haiku-4-5` as the fast model — highest
  extraction quality, pay-per-token.

The worker runs as its own process (see the `worker` service in `docker-compose.yml`). Tier 3 drives a headless Chromium via Playwright; without `playwright install chromium` it is unavailable and URL jobs that need it fail gracefully with the tier-2 reason. The job-level LLM spend is bounded by `RN_JOB_COST_CAP_USD` (default $1.50).

Ingestion quickstart: with the api and worker both running, open the **Inbox** in the web app and paste a recipe URL, drop a PDF/image, or paste text. Each submission becomes a job that the worker extracts in the background; finished jobs land in the inbox as `NEEDS REVIEW`, where the **Review** screen shows the source beside the editable draft. Accept moves the recipe into your cookbook. No API key? Run the worker with `RN_LLM_STUB=1` to exercise the full pipeline with a canned extraction (test/e2e only).

Configuration is via `RN_`-prefixed env vars (`src/recipe_normalizer/config.py`); the defaults match the compose Postgres (`postgresql+psycopg://rn:rn@localhost:5432/rn`).

Deployment notes:

- **`RN_COOKIE_SECURE` must be `true` in any TLS deployment** — it defaults to `false` so plain-http local dev works, and compose passes it through (`RN_COOKIE_SECURE=true docker compose up -d`); leaving it false ships the 30-day session cookie without the `Secure` flag.
- The api container runs uvicorn with `--proxy-headers` so the per-IP rate limits see the real client IP behind the nginx proxy. It relies on nginx being the only ingress — do not publish the api port publicly without re-scoping `--forwarded-allow-ips`.
- **Admin is granted out-of-band only**, never at registration: `docker compose exec api uv run python -m recipe_normalizer.users.make_admin <email>` (the user must already exist).

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
- **Plan 3 — search, organization, sharing (shipped):** filters and full-text search, copy-on-share, public links, and co-owned cookbooks (originally `sharing`'s own shared-cookbook model; superseded by the first-class `Cookbook` entity in the cookbooks pivot). Organization shipped as "collections", a second axis alongside one-cookbook-per-recipe; the boards pivot made cookbooks themselves many-to-many — which is what a collection was — so collections were folded into cookbooks and retired.
- **Plan 4 — AI features (shipped):** per-recipe chat, cookbook Q&A, transformations, content-based recommendations.

## Documents

- Spec: [`docs/superpowers/specs/2026-06-12-recipe-normalizer-design.md`](docs/superpowers/specs/2026-06-12-recipe-normalizer-design.md)
- Plan 1 (this codebase): [`docs/superpowers/plans/2026-06-12-plan-1-foundation-domain-core.md`](docs/superpowers/plans/2026-06-12-plan-1-foundation-domain-core.md)
