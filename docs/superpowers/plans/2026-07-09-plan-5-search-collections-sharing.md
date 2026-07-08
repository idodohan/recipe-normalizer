# Phase 2: Search, Collections & Sharing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement spec §7's search/filters, collections, and all three sharing mechanisms — copy-on-share, public links, shared cookbooks (owner decision: last-write-wins accepted) — completing product goal B ("our house recipes").

**Architecture:** Search + collections live in the existing `cookbook` module (collections were always spec'd as cookbook-owned). Sharing is a NEW `sharing` module (`src/recipe_normalizer/sharing/`) owning shares, shared cookbooks, memberships, and public links, talking to cookbook only via `cookbook.service`. Postgres FTS (`tsvector` on title/description) paired with `pg_trgm` similarity so non-English text (Hebrew later) searches well. Public pages are unauthenticated read-only React routes reusing the dual-quantity renderer.

**Tech Stack:** as Phases 0–1. New Postgres extensions: `pg_trgm` (enable in migration), `unaccent` optional (skip — YAGNI).

## Global Constraints

- All CI gates green per task (backend gates, web typecheck, client drift via `make client`, E2E stays green).
- Module boundaries: `sharing` imports `cookbook.service`/`schemas` only; `cookbook` must NOT import `sharing`. Update `.importlinter` contracts to cover the new module.
- **Authorization is the critical surface of this phase.** Current invariant "recipe visible ⇔ owner" becomes "owner OR member of a shared cookbook containing it (read/edit) OR valid public token (read-only)". Every widening must be explicit, tested (positive AND negative), and centralized in one service function — no scattered ad-hoc checks.
- Public endpoints (token routes) are unauthenticated: rate-limit them per-IP (Phase 0 `limit_by_ip`), and they must leak nothing beyond the shared recipe/cookbook (no owner email, no job/meta internals).
- Sharing semantics per spec §7 + owner decisions: copy-on-share = independent snapshot with `provenance={shared_by, shared_at, origin_recipe_id}`; shared cookbooks = live co-owned, LWW, surface `last_edited_by/at`; public links = tokenized (unguessable ≥128-bit), revocable, read-only, no account needed.
- Keep the editorial design language; new UI reuses Phase 1 systems (toasts, skeletons, ErrorState, Tabs, dark tokens).
- Deterministic core untouched.

---

### Task 1: Search & filter backend

**Files:** `cookbook/service.py` (`list_recipes` gains params), `cookbook/router.py` (query params), `cookbook/schemas.py` (paginated response `RecipePage {items, total, limit, offset}`), new migration (FTS + trigram indexes), tests.

**Interfaces:** `GET /api/recipes?q=&cuisine=&dish_type=&tag=&dietary=&max_total_min=&source_type=&favorites=&limit=&offset=` → `RecipePage`. `q` matches title/description via `websearch_to_tsquery('simple', q)` OR trigram `similarity(title, q) > 0.25` OR ingredient-line `original_text ILIKE`-trigram; filters ANDed; `dietary` computed by joining ingredient dietary_flags (vegan/vegetarian/gluten_free: recipe qualifies when ALL matched canonical ingredients carry the flag — read how dietary_flags are stored in catalog first and document the chosen rule in a docstring). Migration: `CREATE EXTENSION IF NOT EXISTS pg_trgm`, GIN trigram index on `recipes.title`, GIN `to_tsvector('simple', title || ' ' || coalesce(description,''))` expression index. Frontend keeps working: default params preserve current behavior (all, newest-first). `make client`.

### Task 2: Search & filter UI

**Files:** `web/src/pages/CookbookPage.tsx` + css, small `useSearchParamsState` helper.

Search input (debounced 300ms, like catalog) + filter row (cuisine/dish-type/tag selects fed by `useVocab`, dietary chips, favorites chip from Phase 1 folds in). State mirrored to URL search params (shareable/back-button friendly). Server-side search replaces the client-side favorites filter. Skeletons while fetching, count line, EmptyState for no matches. Pagination: "Load more" button (offset += limit), not infinite scroll.

### Task 3: Collections backend

**Files:** `cookbook/models.py` (`Collection {id, owner_id, name(120), created_at}` + `collection_recipes` m2m), service CRUD (`create/rename/delete_collection`, `set_recipe_collections(recipe_id, collection_ids)` owner-scoped), router (`GET/POST /api/collections`, `PATCH/DELETE /api/collections/{id}`, `PUT /api/recipes/{id}/collections`), `collection` filter on `list_recipes`, migration, tests (ownership 404s, delete leaves recipes intact), `make client`.

### Task 4: Collections UI

**Files:** CookbookPage (collection chips/select in the filter row), RecipeDetailPage ("Collections" popover — checkbox list + create-new inline), toasts.

### Task 5: `sharing` module scaffold + copy-on-share backend

**Files:** new `src/recipe_normalizer/sharing/{__init__,models,schemas,service,router}.py`, `.importlinter` update, `main.py` router include, migration (`shares {id, origin_recipe_id, from_user_id, to_user_id, copied_recipe_id, created_at}`), tests.

**Interfaces:** `POST /api/share/recipe {recipe_id, to_email}` → looks up recipient by email (404 "No account with that email"), deep-copies the recipe via a new `cookbook.service.copy_recipe(db, recipe_id, *, new_owner_id, provenance)` (full deep copy: groups/lines/steps/vocab links/image_ref shared by ref; `is_verified` preserved; favorites/notes NOT copied; `source_fingerprint=None` so recipient can also import the original later), sets `provenance={"shared_by": from_email, "shared_at": iso, "origin_recipe_id": str}`. Rate-limit per-user (reuse `limit_by_user`, 30/hour). Recipient sees it in their cookbook immediately (provenance renders as a small "Shared by X" line in detail — Task 8 UI).

### Task 6: Public links backend

**Files:** `sharing/models.py` (`public_links {id, token(unique, secrets.token_urlsafe(24)), recipe_id?, revoked_at?, created_by, created_at}`), service + router: `POST /api/share/public {recipe_id}` → creates/returns link; `GET /api/share/public` (mine); `DELETE /api/share/public/{id}` (revoke); **unauthenticated** `GET /api/public/{token}` → recipe payload (RecipeOut minus notes/is_favorite/extraction_meta — define a `PublicRecipeOut`) with `limit_by_ip` 60/min; `GET /api/public/{token}/scaled?...` reusing the scaling service. 404 for revoked/unknown (indistinguishable). Tests incl. revocation and no-leak assertions. `make client`.

### Task 7: Shared cookbooks backend

**Files:** `sharing/models.py` (`shared_cookbooks {id, name, created_by}`, `shared_cookbook_members {cookbook_id, user_id, added_by}` PK(cookbook_id,user_id), `shared_cookbook_recipes {cookbook_id, recipe_id, added_by}`), service + router, migration, tests.

**Interfaces:** `POST /api/shared-cookbooks {name}`; `GET /api/shared-cookbooks` (mine=member); `GET /api/shared-cookbooks/{id}` (recipes list w/ RecipeSummary + last_edited_by display name); `POST .../members {email}` (member-invites-member allowed — friends scale); `DELETE .../members/{user_id}` (self-leave, or creator removes); `POST .../recipes {recipe_id}` (must own or co-own the recipe); `DELETE .../recipes/{recipe_id}`. **Access widening (the critical bit):** ONE new function `cookbook.service.user_can_access_recipe(db, user_id, recipe_id) -> "owner"|"member"|None` — implemented in cookbook via a REGISTERED HOOK from sharing (sharing registers a membership-checker callback at startup, mirroring the existing `register_hooks()` merge-hook pattern, so cookbook never imports sharing). `get_recipe`/`get_scaled`/`update_recipe` accept member access; DELETE and personal (favorites/notes/collections/image) stay owner-only. `update_recipe` sets `last_edited_by/at` (already does — verify). Negative tests: non-member 404, member cannot delete, member CAN edit, removed member loses access.

### Task 8: Sharing UI — share dialog + provenance + Shares nav

**Files:** RecipeDetailPage ("Share" button → dialog with three sections: Send a copy (email input), Public link (create/copy-to-clipboard/revoke), Add to shared cookbook (list + create)), provenance line on detail ("Shared by X on date"), AppShell: "Shares" nav item becomes live → new `SharesPage` listing shared cookbooks (create form) — route `/shares`; shared cookbook page `/shares/:id` (recipe grid + members panel + invite input + leave). Editorial styling; dialog is an accessible modal (focus trap, Escape, `aria-modal`) — build a small `Dialog.tsx` on `<dialog>` element.

### Task 9: Public recipe page (unauthenticated)

**Files:** router entry OUTSIDE the authenticated shell: `/p/:token` → `PublicRecipePage` — reuses IngredientList/StepList/ScaleControl against the public endpoints, both themes, no nav (minimal header with wordmark + "Recipe Normalizer" footer link), 404 state for revoked. E2E-friendly.

### Task 10: E2E + phase gate

New `e2e/tests/sharing.spec.ts`: search finds a recipe by title fragment; collection create/assign/filter; share-copy between two registered users (recipient sees provenance); public link → logged-out context renders the recipe + revoke → 404; shared cookbook: user A creates + invites B (by email), B adds a recipe, A edits it, B sees `last_edited_by` A... (keep to the valuable spine; ~6 tests). Full gates; final whole-branch review (opus) incl. an authorization-focused adversarial pass (member/non-member/revoked-token matrices); fix wave; roadmap update; merge.
