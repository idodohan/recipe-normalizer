# Cookbooks Pivot — Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Make **Cookbook** the primary container — a user owns many cookbooks, each with visibility + member roles; every recipe belongs to exactly one cookbook — while the existing app keeps running.

**Architecture:** Grow the existing `cookbook` module with a `Cookbook` entity + `CookbookMember`; add `recipes.cookbook_id` (NOT NULL after backfill). Absorb `sharing`'s shared-cookbook concept into cookbook access (owner/editor/viewer). One data-preserving Alembic migration. Catalog routes gate on admin. `GET /api/recipes` stays flat (compat shim) so the current frontend survives until Phases 2–3.

**Tech Stack:** Python 3.12+, FastAPI, SQLAlchemy 2 (Mapped/mapped_column), Alembic, Postgres, pytest, mypy --strict, ruff, import-linter. Spec: `docs/superpowers/specs/2026-07-27-cookbooks-pivot-design.md`.

## Global Constraints

- mypy --strict, ruff, `lint-imports` (9 contracts) must stay green; regenerate `web/src/api/schema.d.ts` via `make client` after any API change (CI diffs it).
- No `text()` SQL interpolation — SQLAlchemy expression language only.
- Tokens/IDs ≥128-bit: cookbook `public_token` uses `secrets.token_urlsafe(24)` (~192-bit), matching existing public links.
- Access rule (authoritative): recipe access derives from cookbook access. `viewer`=read; `editor`=read+add/edit recipes; `owner`=all+manage(visibility/members/delete). Existence is hidden with 404, never 403 — EXCEPT catalog admin-gate which returns 403 (known route, privileged).
- Default cookbook (`is_default=true`) is never deletable and always `private` unless the owner changes it.
- Every task: TDD (failing test first), then commit. Real Postgres via the existing test fixtures.

---

### Task 1: Cookbook + CookbookMember models

**Files:**
- Modify: `src/recipe_normalizer/cookbook/models.py` (add entities)
- Test: `tests/cookbook/test_cookbook_models.py`

**Interfaces:**
- Produces: `Cookbook(id, owner_id, name, description|None, cover_image_ref|None, visibility: CookbookVisibility, public_token|None, is_default: bool, created_at, updated_at)`; `CookbookMember(cookbook_id, user_id, role: CookbookRole, added_by, created_at)`; enums `CookbookVisibility(private|unlisted|public)`, `CookbookRole(editor|viewer)`.

- [ ] **Step 1: Write failing test** — construct a `Cookbook` + `CookbookMember`, assert defaults (`visibility=private`, `is_default=False`, `public_token=None`) and that the enums serialize to their string values. Persist via a session fixture and read back.
- [ ] **Step 2: Run — fail** (`ImportError`/attribute).
- [ ] **Step 3: Implement** the two mapped classes + enums following the module's existing `StrEnum` + `Mapped`/`mapped_column` patterns. `owner_id` FK users CASCADE indexed; `public_token` unique nullable; composite PK on member `(cookbook_id, user_id)`; member `cookbook_id` FK cookbooks CASCADE.
- [ ] **Step 4: Run — pass.**
- [ ] **Step 5: Commit** `feat(cookbook): Cookbook + CookbookMember models`.

### Task 2: `recipes.cookbook_id` column (nullable first)

**Files:** Modify `src/recipe_normalizer/cookbook/models.py` (Recipe); Test `tests/cookbook/test_cookbook_models.py`.

**Interfaces:** Produces `Recipe.cookbook_id: Mapped[uuid.UUID]` (FK cookbooks CASCADE, indexed). Model declares NOT NULL; the migration backfills before enforcing (Task 8).

- [ ] **Step 1:** Failing test — create a cookbook, create a recipe with `cookbook_id=cb.id`, read back and assert linkage; assert a relationship/loader if one is added.
- [ ] **Step 2:** Run — fail.
- [ ] **Step 3:** Add the column (FK CASCADE, indexed). Keep it required at the model level.
- [ ] **Step 4:** Run — pass.
- [ ] **Step 5:** Commit `feat(cookbook): recipes belong to a cookbook`.

### Task 3: Access model — `access()` + role resolution

**Files:** Modify `src/recipe_normalizer/cookbook/service.py`; Test `tests/cookbook/test_cookbook_access.py`.

**Interfaces:**
- Produces: `cookbook_access(db, *, user_id, cookbook_id) -> CookbookRole | Literal["owner"] | None`; `require_cookbook_access(db, *, user_id, cookbook_id, need: Access) -> Cookbook` raising `ApiError(404)` when insufficient. `Access` = an ordered notion where owner ⊃ editor ⊃ viewer; helper `_rank(role)`.

- [ ] **Step 1:** Failing tests — the full matrix: owner→all; editor member→read+edit but not manage; viewer member→read only; non-member→None/404; owner_id always wins over any member row. Include: a `public`/`unlisted` cookbook grants anonymous **viewer** read (user_id=None) but never edit.
- [ ] **Step 2:** Run — fail.
- [ ] **Step 3:** Implement resolution: `owner_id==user` → owner; else member row role; else if visibility in {unlisted, public} → viewer; else None. `require_cookbook_access` compares `_rank`.
- [ ] **Step 4:** Run — pass.
- [ ] **Step 5:** Commit `feat(cookbook): cookbook access + role resolution`.

### Task 4: Cookbook CRUD + visibility + membership services

**Files:** Modify `src/recipe_normalizer/cookbook/service.py`, `cookbook/schemas.py`; Test `tests/cookbook/test_cookbook_service.py`.

**Interfaces (Produces):** `list_my_cookbooks(db, user_id) -> list[CookbookSummary]` (owned + member-of, each with `role`, `recipe_count`); `create_cookbook(db, owner_id, name, description=None) -> Cookbook`; `rename`, `set_description`, `set_cover`; `delete_cookbook` (raises if `is_default`); `set_visibility(db, cookbook_id, owner_id, visibility)` (mints `public_token` for unlisted/public, clears for private); `invite_member(db, cookbook_id, owner_id, email, role)`, `set_member_role`, `remove_member`, `leave_cookbook`. `ensure_default_cookbook(db, user_id) -> Cookbook`.

- [ ] **Step 1:** Failing tests, one behavior each: create→appears in list with role=owner+count; rename/description/cover persist; delete non-default ok, delete default raises `ApiError(409)`; set_visibility public mints a token, back to private clears it; invite by unknown email raises 404-style (reuse users lookup); invite existing user adds editor/viewer; only owner can manage (editor invite attempt → 404 via require access); `ensure_default_cookbook` idempotent (one default per user).
- [ ] **Step 2:** Run — fail.
- [ ] **Step 3:** Implement, threading `require_cookbook_access(..., need=owner)` on all manage ops. Reuse the users module's email→user lookup (respect the existing enumeration discipline — do not leak existence beyond current behavior).
- [ ] **Step 4:** Run — pass; also run `lint-imports` (cookbook must not import users' internals — go through its service).
- [ ] **Step 5:** Commit `feat(cookbook): cookbook CRUD, visibility, membership`.

### Task 5: Recipe create/move honor cookbook access

**Files:** Modify `cookbook/service.py` (create_recipe, new `move_recipe`), `cookbook/router.py`; Test `tests/cookbook/test_service.py` (extend).

**Interfaces:** `create_recipe(..., cookbook_id)` requires editor+ on that cookbook (default: caller's default cookbook when omitted, via `ensure_default_cookbook`). `move_recipe(db, recipe_id, user_id, to_cookbook_id)` requires editor on both source and destination. `get_recipe`/`update_recipe`/`delete_recipe` now derive access from the recipe's cookbook via Task 3 (replaces the old `user_recipe_access` widening).

- [ ] **Step 1:** Failing tests — create into a cookbook you edit (ok); into one you only view (404); into one you're not in (404); omitted cookbook_id → lands in caller's default; move between two editable cookbooks ok; move where you lack destination edit → 404; a viewer of a shared cookbook can GET a recipe but PATCH/DELETE 404.
- [ ] **Step 2:** Run — fail.
- [ ] **Step 3:** Implement; delete the now-dead `user_recipe_access` widening and any sharing-based access branch. Update `cookbook/router.py` request/response models (recipe carries `cookbook_id`).
- [ ] **Step 4:** Run — pass; `mypy --strict`.
- [ ] **Step 5:** Commit `feat(cookbook): recipe ops derive access from cookbook`.

### Task 6: Cookbook + public-cookbook routers

**Files:** Modify `cookbook/router.py` (or add `cookbook/cookbook_router.py` if the file is large — keep one router registered), `main.py` (public route); Test `tests/cookbook/test_cookbook_router.py`, extend `tests/test_app.py`.

**Interfaces (HTTP):** `GET /api/cookbooks` (my cookbooks + role + count), `POST /api/cookbooks`, `PATCH /api/cookbooks/{id}` (name/description/visibility), `DELETE /api/cookbooks/{id}`, `GET /api/cookbooks/{id}` (+ its recipes, access-checked), member routes `POST/PATCH/DELETE /api/cookbooks/{id}/members`, and anonymous `GET /api/public/cookbooks/{token}` returning a strict allowlist (mirror `PublicRecipeOut`: no owner_id/emails leak).

- [ ] **Step 1:** Failing API tests via the app test client — the happy paths + a viewer/editor/owner authorization sweep + anonymous token read of a public cookbook, and 404 on a private one's token.
- [ ] **Step 2:** Run — fail.
- [ ] **Step 3:** Implement routes delegating to Task 4/5 services; add a `PublicCookbookOut` allowlist schema.
- [ ] **Step 4:** Run — pass; `make client` to regen `schema.d.ts`; commit that too.
- [ ] **Step 5:** Commit `feat(api): cookbook + public-cookbook endpoints`.

### Task 7: Catalog admin-gate

**Files:** Modify `catalog/router.py`, reuse `users` admin dependency (find it: `api_deps` or `users`); Test `tests/catalog/test_router.py` (extend).

**Interfaces:** All user-facing catalog routes (list/get/edit/merge) depend on `require_admin` → 403 for non-admins. Internal matching path (called from the pipeline, not HTTP) unchanged.

- [ ] **Step 1:** Failing test — a non-admin session gets 403 on `GET /api/catalog/...`; an admin gets 200; unauthenticated gets 401.
- [ ] **Step 2:** Run — fail.
- [ ] **Step 3:** Add the dependency to the catalog router.
- [ ] **Step 4:** Run — pass.
- [ ] **Step 5:** Commit `feat(catalog): restrict catalog routes to admins`.

### Task 8: Data-preserving migration + drop shared_cookbooks

**Files:** Create `alembic/versions/<rev>_cookbooks_pivot.py`; Test `tests/test_cookbooks_migration.py`.

**Interfaces:** upgrade(): create `cookbooks`, `cookbook_members`; add `recipes.cookbook_id` nullable; backfill; enforce NOT NULL; drop `shared_cookbooks`, `shared_cookbook_members`, `shared_cookbook_recipes`. downgrade(): reverse (recreate shared tables empty, drop cookbook_id + tables).

Backfill logic (run via `op.get_bind()` + SQLAlchemy core, no ORM):
1. For each distinct `recipes.owner_id` (and each user in `users`), insert a default cookbook (`is_default=true`, `visibility='private'`, `name='My Cookbook'`).
2. `UPDATE recipes SET cookbook_id = <owner's default>`.
3. For each `shared_cookbooks` row: insert a cookbook (`owner_id=created_by`, `visibility='private'`, `name=<name>`, `is_default=false`); insert `cookbook_members` from `shared_cookbook_members` (`role='editor'`); for each `shared_cookbook_recipes.recipe_id`, `UPDATE recipes SET cookbook_id=<new>` where that recipe isn't already reassigned (first-wins; a query logs any recipe seen in >1 shared cookbook).
4. Any `recipes.cookbook_id IS NULL` → owner's default (defensive).
5. `ALTER COLUMN cookbook_id SET NOT NULL`.
6. Drop the three `shared_cookbook*` tables.

- [ ] **Step 1:** Write `tests/test_cookbooks_migration.py` — seed pre-migration state (users, recipes, a shared_cookbook with members+recipes) at the prior head, run `alembic upgrade head`, assert: every recipe has a non-null cookbook_id; each user has exactly one default cookbook; the shared cookbook became a cookbook with editor members and its recipes reassigned; shared_cookbook* tables are gone. (Use the existing migration-test harness if present — see `tests/test_db.py`/`test_get_db_integration.py`; otherwise drive alembic against the test database.)
- [ ] **Step 2:** Run — fail (no migration).
- [ ] **Step 3:** Autogenerate a skeleton (`alembic revision`), then hand-write upgrade/downgrade per the logic above. Verify `alembic upgrade head` then `alembic downgrade -1` then `upgrade head` round-trips on an empty DB.
- [ ] **Step 4:** Run — pass.
- [ ] **Step 5:** Commit `feat(db): cookbooks pivot migration (data-preserving)`.

### Task 9: Absorb sharing / compat shim / cleanup

**Files:** Modify `sharing/` (remove SharedCookbook service+router+schemas+models; keep `shares` + per-recipe `public_links`), `main.py` router registration, `.importlinter`, `cookbook/router.py` (`GET /api/recipes` stays flat = all recipes the user can read across cookbooks); Test: extend app tests; delete dead sharing tests.

- [ ] **Step 1:** Failing test — `GET /api/recipes` returns recipes from the caller's default AND from a cookbook shared to them as viewer/editor (flat compat list); the removed `/api/shared-cookbooks*` routes return 404/deprecated-empty and no longer 500.
- [ ] **Step 2:** Run — fail.
- [ ] **Step 3:** Delete SharedCookbook code paths; update `list_recipes` to span readable cookbooks; update import-linter contracts (drop the shared-cookbook boundary, keep the rest); remove the now-dead sharing tests referencing SharedCookbook.
- [ ] **Step 4:** Run full backend suite + `mypy --strict` + `ruff` + `lint-imports`; `make client`.
- [ ] **Step 5:** Commit `refactor(sharing): fold shared cookbooks into cookbook access`.

---

## Self-Review

- **Spec coverage:** Cookbook entity (T1), exactly-one containment (T2, T8), roles/visibility (T1,T3,T4), access derivation replacing H9 widening (T3,T5), public cookbook page (T6), catalog admin-gate (T7), data-preserving migration incl. shared-cookbook absorption (T8), compat shim keeping the app alive (T9). All spec Phase-1 bullets map to a task.
- **Placeholder scan:** none — each task names files, interfaces, and concrete test intents; the migration logic is spelled out step-by-step.
- **Type consistency:** `CookbookRole`/`CookbookVisibility`/`cookbook_access` names used identically across T1/T3/T4/T5/T6; `ensure_default_cookbook` defined in T4, used in T5/T8.
- **Scope:** backend-only, one migration, app stays runnable via T9's flat `GET /api/recipes`. Frontend rebuild is Phases 2–3 (separate plans).
