# Recipe Normalizer — Product & Design Spec

**Date:** 2026-06-12
**Status:** Approved design, pre-implementation
**Audience:** Implementer agent. This document is the source of truth for what to build and why. Where it is silent, prefer the simplest option consistent with the module boundaries below.

---

## 1. Product summary

A web application that ingests recipes from anywhere — URLs, PDFs, images, pasted text — and normalizes them into a single unified structure. Users build a personal, filterable cookbook; share recipes and cookbooks with each other; scale recipes deterministically; and ask AI questions grounded in their recipes. **Scope includes all recipes: food, baking, cocktails/drinks, smoothies — not food only.**

- **Users:** up to 10 (friends/family scale). Deployed locally first (docker-compose on one machine); the end goal is SaaS, so the architecture must make that a deployment change, not a rewrite.
- **Quality bar:** exceptional, easily-extended code. Module boundaries are enforced, every input type is a plugin, every external dependency sits behind an interface.

## 2. Goals and non-goals

### Goals (v1)
1. Ingest recipes from: URL, PDF, image (photos of cookbook pages / handwritten cards), TXT/pasted text, and manual entry.
2. Normalize every recipe into one schema with **dual quantities**: original text always preserved and displayed, plus normalized weight/volume (grams for solids, ml for liquids), with approximations flagged.
3. Tiered, partly **agentic** web extraction that survives ads, popups, cookie banners, and JS-only sites.
4. Filterable cookbook: by ingredient (canonical, cross-language), cuisine, dish type, tags, dietary flags, time, source.
5. Deterministic recipe scaling (by factor or target servings).
6. Sharing: copy-on-share between users, shared (co-owned) cookbooks, public read-only links.
7. AI features: per-recipe chat, cookbook-wide Q&A, AI recipe transformations, content-based recommendations.
8. Per-user persistent data in Postgres (row-level ownership; the multi-tenant model that carries into SaaS).
9. Multilingual input (e.g., Hebrew family recipes), English-normalized canonical layer, originals preserved.

### Non-goals (v1) — list verbatim in any roadmap
Nutrition data; meal planning; cook-mode (step-by-step cooking screen); user photo uploads of finished dishes; video/social inputs (YouTube, Instagram); email-in ingestion; voice-memo ingestion; collaborative-filtering recommendations; mobile apps; billing/multi-tenant SaaS infra (architecture must allow it; do not build it).

## 3. Tech stack

- **Backend:** Python 3.12+, FastAPI, SQLAlchemy 2.x + Alembic, Pydantic v2, Postgres 16 (the only datastore).
- **Worker:** same codebase, separate process; Postgres-backed job queue (`FOR UPDATE SKIP LOCKED`), retries with exponential backoff.
- **LLM:** Claude API via a single `LLMClient` wrapper (model ids, retries, timeouts, structured output, vision, token/cost logging all live here). Default to the latest capable Claude model; cheap/fast model for classification-grade calls.
- **Agentic browser:** Playwright (Chromium) driven by a tool-using Claude loop with screenshots.
- **Frontend:** React + TypeScript, Vite, TanStack Query, typed API client generated from the FastAPI OpenAPI schema (regenerated in CI; drift fails the build).
- **Deploy:** docker-compose with services `api`, `worker`, `web`, `postgres`. One command up.

## 4. Architecture — modular monolith

One repo. Backend `src/` is split into modules. **Each module owns its tables and exposes a typed service interface; cross-module access happens only through those interfaces.** Enforce with import-linter (or equivalent) in CI — a module importing another module's internals fails the build.

| Module | Owns | Responsibility |
|---|---|---|
| `users` | users, sessions, preferences | Accounts. Auth behind an `AuthProvider` interface: v1 = email+password; SaaS swaps in OAuth without touching other modules. |
| `ingestion` | inputs, jobs | Accept any input (URL / file upload / pasted text), create extraction jobs, job lifecycle: `queued → running → needs_review → done | failed | not_a_recipe`. Stores raw acquired artifacts. |
| `extraction` | (stateless) | The acquire+normalize pipeline (§6). Pluggable `Extractor` interface per source type. |
| `catalog` | canonical ingredients, aliases, densities, units | Canonical ingredient entities, multilingual alias matching, unit conversion math, merge/dedupe of near-duplicate entities. |
| `cookbook` | recipes, ingredient lines, steps, collections, variants | Recipe CRUD, search/filter, scaling, variant links (`derived_from`). |
| `sharing` | shares, shared cookbooks, public links | Copy-on-share, co-owned cookbooks, tokenized revocable public links. |
| `ai` | conversations, messages, recommendations cache | Per-recipe chat, cookbook Q&A (tool-use over cookbook search), transformations, content-based recommendations. Per-feature prompt modules. |

**Frontend pages:** Inbox (submit inputs + review queue), Cookbook (browse/filter), Recipe detail (dual quantities, scaler, chat panel, variants), Catalog admin (review `unreviewed` ingredients, merge duplicates), Shares.

## 5. Domain model

`schema_version` on every recipe; all changes via Alembic migrations.

### Recipe
- `id`, `owner_id`, `schema_version`
- `title`, `description`
- `source` (URL / file reference / `manual`), `source_type` (`web | pdf | image | text | manual`)
- `language` (BCP-47 of the original)
- `servings`: base yield — structured as `{amount, unit_text}` (e.g. `{4, "servings"}`, `{1, "loaf (~900g)"}`, `{1, "cocktail"}`)
- `times`: `{prep_min, cook_min, total_min}` (nullable)
- `cuisines[]`, `dish_types[]`, `tags[]` — controlled vocabularies stored as DB tables (seedable, growable), not enums in code. `dish_types` includes drinks: `cocktail`, `drink`, `smoothie`, etc.
- `ingredient_groups[]` — ordered; each has optional `name` ("For the dough") and ordered `ingredient_lines[]`
- `steps[]` — ordered; `original_text` preserved verbatim; optional `ingredient_line_refs[]`
- `extraction_meta`: `{tier_used, confidence, model, extracted_at, actions_log}` (e.g. "tier 3, dismissed 2 popups")
- `is_verified` — true once a human accepted it in the review screen
- `provenance` — when copied via sharing: `{shared_by, shared_at, origin_recipe_id}`
- `derived_from` — nullable recipe id; AI transformations are full recipes linked to their parent

### IngredientLine — the dual-quantity heart
- `original_text` — verbatim, always displayed ("1 heaping cup of flour", "כוס קמח")
- `quantity` (decimal, nullable), `unit` (as parsed from original, nullable)
- `canonical_ingredient_id` → catalog (nullable only when matching genuinely fails)
- `normalized_amount` + `normalized_unit` (`g` for solids, `ml` for liquids) + `is_approx` flag
  - exact when the source gave weight/volume directly; **approximate** (flagged, shown as "~") when converted from volume/count via catalog densities; `null` when unconvertible ("salt to taste") — displayed as original only
- `note` ("finely chopped"), `is_optional`

**Display rule (hard requirement):** every ingredient line renders original **and** normalized, e.g. `1 cup flour → ~120 g (approx.)`, `1 oz gin → 30 ml`. Never hide the original.

### CanonicalIngredient (catalog)
- `name` (English canonical), `aliases[]` (multilingual, e.g. "AP flour", "קמח לבן")
- `category` (produce, dairy, spirits, …), `dietary_flags[]` (contains-gluten, dairy, animal-product, alcohol, …)
- `preferred_measure`: `mass | volume` — solids normalize to grams, liquids to ml
- `density_data`: g/ml, g per cup/tbsp/tsp, g per unit (e.g. 1 medium egg ≈ 50 g) — whatever is known; sparse is fine
- `status`: `seeded | unreviewed | reviewed` — extraction auto-creates `unreviewed` entries; Catalog admin page supports review and **merge** (merging re-points all ingredient lines)
- Seed with ~200 common ingredients (incl. bar staples: gin, vodka, simple syrup, bitters…) with density data. Seed file lives in the repo as data, not code.

### Units
Unit parsing/conversion is deterministic code in `catalog`, **never** LLM math. Support: metric (g, kg, ml, l), US/imperial (cup, tbsp, tsp, oz, fl oz, lb), count units (clove, egg, slice), and bar units (jigger, dash, splash, shot, **parts**). "Parts" recipes scale natively by ratio.

## 6. Extraction pipeline

Two phases for every input: **acquire** (source → clean recipe text/artifacts) and **normalize** (text → schema). Normalize is one shared stage; acquire is per-source-type plugins implementing a common `Extractor` interface. New input types = new plugins.

### 6.1 Acquire — URL (tiered escalation)
Each tier runs only if the previous one failed; the tier used and actions taken are recorded in `extraction_meta`.

- **Tier 1 — structured data.** Fetch HTML; parse `schema.org/Recipe` JSON-LD/microdata. Present and complete → done (<1 s, no LLM). Expected to cover the majority of recipe sites.
- **Tier 2 — readable HTML.** Strip nav/ads/scripts to readable text; a cheap LLM call judges "is the full recipe present in this text?" Yes → done.
- **Tier 3 — agentic browser.** Playwright session driven by a tool-using Claude loop with screenshots. Available actions: dismiss cookie banners/popups/overlay ads, click "jump to recipe", scroll, expand collapsed sections, wait for JS render, capture content. **Budgets:** max 15 actions and 90 s hard timeout; on budget exhaustion fail gracefully with the final screenshot attached so the user sees why.

### 6.2 Acquire — other inputs
- **PDF:** extract text layer; if empty/garbled (scanned cookbook) → render pages to images → Claude vision.
- **Image:** Claude vision directly (same code path as scanned PDFs — handwritten cards included).
- **TXT / pasted text:** pass-through.
- **Manual entry:** structured form, skips extraction entirely.
- Future plugins (out of v1): YouTube transcript, Instagram, email-in, voice memo — same interface, listed in roadmap only.

### 6.3 Normalize (shared)
1. One structured-output LLM pass (Pydantic-validated) produces the §5 schema from acquired text/images: ingredient lines parsed into quantity/unit/name/note, groups detected, steps split, cuisine/dish-type/tags assigned, language detected, `servings` extracted.
2. **Deterministic post-processing (code, not LLM):** unit→g/ml conversion via catalog densities; `is_approx` set per the rules in §5.
3. **Catalog matching** per ingredient line: exact/alias match → link; fuzzy+LLM match above threshold → link; else create `unreviewed` canonical ingredient.

### 6.4 Guardrails
- **Not-a-recipe:** the normalize pass returns `is_recipe` + confidence. Below threshold → job ends `not_a_recipe` with a human-readable reason (news article, menu photo, …). Never fabricate a recipe from a non-recipe.
- **Review gate:** every successful extraction lands in `needs_review`. The review screen shows extracted fields side-by-side with the source artifact (page snapshot / image), low-confidence fields highlighted; every field editable. Accept → cookbook with `is_verified = true`.
- **Raw artifact retention:** acquired raw text/screenshots are stored with the job; retry after a fix never re-scrapes.

## 7. Features

### Cookbook & search
Filters: canonical ingredient (matches all aliases and languages), cuisine, dish type, tags, dietary flags, max total time, source type. Dietary flags are **computed** from ingredient `dietary_flags` (vegan/vegetarian/GF/contains-alcohol), not hand-tagged. Full-text search over title/description/ingredients. "What can I make with X, Y" = ingredient-overlap query. Collections (user-defined folders).

### Scaling
Deterministic; by factor or by target servings. Normalized amounts scale exactly; original-quantity display recomputes into sensible fractions ("2¼ cups"). Unconvertible lines ("salt to taste") pass through unchanged and are visibly flagged. Scaling is a **view**, never a mutation; original recipe is immutable. A scaled view can be saved as a variant (`derived_from`).

### Sharing
- **Copy-on-share:** recipient gets an independent snapshot copy with `provenance` set. No live propagation, no permission system.
- **Shared cookbooks:** a cookbook co-owned by invited members; recipes added to it are co-owned by the cookbook (members can add/edit); members' personal cookbooks are unaffected.
- **Public links:** tokenized read-only URL per recipe or cookbook; revocable; no account required to view; shows the same dual-quantity rendering.

### AI features (all through `ai` module / `LLMClient`)
- **Per-recipe chat:** grounded on the recipe JSON ("substitute butter with oil?", "why rest the dough?"). Conversation persisted per recipe per user.
- **Cookbook Q&A:** tool-use — the LLM calls the cookbook search/filter API to answer ("what can I make with chicken and rice?", "quick weeknight dessert"). Never dump the whole cookbook into context.
- **Transformations:** "make it vegan / halve it / convert to air fryer" → output is a **new draft recipe** that goes through the same review screen, linked via `derived_from`.
- **Recommendations (v1):** content-based "more like this" using cuisine/ingredient/dish-type similarity. No collaborative filtering.
- Every LLM call logs tokens and cost per user per feature; per-job cost caps abort runaway extractions.

## 8. Error handling

- Jobs never vanish: every terminal state (`failed`, `not_a_recipe`) is user-visible with a reason and a retry button.
- LLM calls: timeouts, bounded retries, structured-output validation with one repair attempt.
- Agentic tier: action/time budgets (§6.1); failure attaches the last screenshot.
- API: consistent error envelope; all user input validated at the boundary (Pydantic).
- The frontend Inbox shows live job progress (poll or SSE) including current tier/action.

## 9. Testing

- **Module unit tests** against real Postgres (testcontainers); each module's service interface is the test surface.
- **Extraction golden corpus:** committed fixtures (saved HTML pages incl. JSON-LD and JS-rendered cases, PDFs, images, Hebrew samples, cocktail recipes) with expected normalized JSON. Extractor changes are regression-tested offline — no network in tests.
- **LLM seam:** all LLM calls mocked at `LLMClient` in tests; small opt-in live-API smoke suite behind an env flag.
- **Conversion math:** exhaustive unit tests for unit conversion, density math, approximation flagging, and scaling (including "parts" and non-scalable lines).
- **API contract:** OpenAPI-generated client regenerated in CI; drift fails the build.
- **E2E happy path:** docker-compose up → submit fixture URL → review → accept → filter → scale → share link, via Playwright test.

## 10. SaaS path (design constraint, not v1 work)

The following must hold so SaaS is a deployment change: all data row-owned by `user_id` (already multi-tenant); auth behind `AuthProvider`; job queue behind a `JobQueue` interface (Postgres now, swappable later); no local-filesystem state outside an object-storage-style `FileStore` interface (local disk now, S3 later); config via environment.

## 11. Build order (suggested phasing for the implementation plan)

1. Skeleton: repo, docker-compose, CI, module layout, users/auth, empty UI shell.
2. Domain core: catalog (+seed data), recipe model, manual entry, cookbook CRUD + dual-quantity rendering + scaling.
3. Extraction: ingestion jobs + worker; text/paste plugin → normalize stage → review screen; then URL tiers 1–2; then PDF; then images; then tier 3 agentic browser.
4. Search & filters; collections.
5. Sharing (copy-on-share → public links → shared cookbooks).
6. AI (recipe chat → cookbook Q&A → transformations → recommendations).
