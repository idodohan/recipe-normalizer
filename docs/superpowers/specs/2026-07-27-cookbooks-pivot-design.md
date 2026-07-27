# Recipe Normalizer — Cookbooks Pivot (Design Spec)

**Date:** 2026-07-27
**Status:** approved (vision + Phase-1 data-model call: recipe belongs to exactly one cookbook)

## Why

The app today is "one filterable pile of my recipes" plus bolt-on Shares. Users
told us: the look is too boxy, the filter bar is noise, Catalog is a DB view they
shouldn't see, Shares should not be a separate place, and "Inbox" is a misleading
name for adding a recipe. The pivot reframes the product around **cookbooks you
own, organize, and share** — a social-cookbook shell over the unchanged
normalization engine (dual quantities + deterministic scaling stay exactly as-is;
that is the moat, not in scope for change).

## Vision

Primary object is a **Cookbook**. A user owns many. Each cookbook has a visibility
(`private` / `unlisted` / `public`) and members with roles. Recipes live inside a
cookbook — **exactly one** (Phase 1 decision; multi-cookbook "boards" is a later,
additive change, not now). The home screen is your cookbooks (plus ones shared with
you) as a warm, photo-forward magazine grid — no hairline boxes, no nine-control
filter bar. Adding a recipe is a guided wizard, not an "Inbox."

## Non-goals (this pivot)

- No change to normalization / conversion / scaling logic.
- No public discovery feed, profiles, or follow (foundation-first; a clean
  fast-follow that depends on this data model).
- No recipe-in-multiple-cookbooks (exactly one for now).
- Collections stay in the schema but drop out of the primary UI (they become an
  optional within-cookbook grouping; not migrated away, not destroyed).

---

## Phase 1 — Cookbook data model + Catalog gating (backend)

The foundation. Ships behind the same gates (pytest, mypy --strict, ruff,
import-linter, OpenAPI client regen). The frontend keeps working through a
compatibility shim (see "Keeping the app alive") until Phases 2–3 rebuild the UI.

### New/changed tables (owned by the existing `cookbook` module)

> The Cookbook entity lives in the **existing `cookbook` module** (which already
> owns recipes/lines/steps) — not a new near-homonym module. The container and
> its recipes belong together, and it keeps the import-linter boundary simple.

- **`cookbooks`**: `id`, `owner_id` (FK users CASCADE, indexed), `name`
  (String(120)), `description` (nullable), `cover_image_ref` (nullable — reuses
  filestore), `visibility` (enum: `private` | `unlisted` | `public`, default
  `private`), `public_token` (nullable, unique — minted for unlisted/public,
  192-bit like the existing public-link tokens), `is_default` (bool — the
  auto-created "My Cookbook", not deletable), timestamps.
- **`cookbook_members`**: `(cookbook_id, user_id)` composite PK, `role` (enum:
  `editor` | `viewer`), `added_by`, `created_at`. The owner is authoritative via
  `cookbooks.owner_id` (no member row for the owner). This fixes the earlier
  audit's H9 (no role column → every member an editor).
- **`recipes.cookbook_id`**: new FK → `cookbooks.id` `ON DELETE CASCADE`, indexed,
  `NOT NULL` after backfill. Exactly-one containment.

### Migration (Alembic, data-preserving)

1. Create `cookbooks` + `cookbook_members`; add `recipes.cookbook_id` **nullable**.
2. For every user with recipes, create a default cookbook `"My Cookbook"`
   (`is_default=true`, `visibility=private`) and set `cookbook_id` on all their
   recipes to it.
3. Migrate the old `sharing` shared-cookbooks: each `shared_cookbooks` row →
   a `cookbooks` row (`owner_id = created_by`, `visibility=private`); each
   `shared_cookbook_members` → `cookbook_members` (`role=editor`, preserving
   today's all-editors behavior); each `shared_cookbook_recipes.recipe_id` →
   set that recipe's `cookbook_id` to the new cookbook (if a recipe is in more
   than one shared cookbook — rare in alpha — assign the first and log the rest).
4. Any recipe still without a `cookbook_id` (defensive) → its owner's default.
5. `ALTER recipes.cookbook_id SET NOT NULL`.
6. Drop the old `shared_cookbooks*` tables (their data now lives in cookbooks).
   Keep `shares` (copy-on-share audit) and per-recipe `public_links` as-is.

### Services / access model (in `cookbook/service.py`)

- `list_my_cookbooks(user)` → owned + member-of, each with recipe count + role.
- `create_cookbook`, `rename`, `set_description`, `set_cover`, `delete`
  (default cookbook not deletable; deleting a cookbook cascades its recipes —
  confirm in UI).
- `set_visibility(cookbook, visibility)` — mints/clears `public_token`.
- `invite_member(cookbook, email, role)`, `set_member_role`, `remove_member`,
  `leave_cookbook`. Only the owner manages membership and visibility.
- `access(user, cookbook) -> Role|None` where Role ∈ {owner, editor, viewer}.
  Recipe access derives from cookbook access: **viewer** = read; **editor** =
  read + add/edit recipes; **owner** = all + manage. This replaces the ad-hoc
  `user_recipe_access` widening and the transitive-escalation bug (audit H9).
- `get_public_cookbook(token)` — anonymous read of an unlisted/public cookbook +
  its recipes, through a strict output allowlist (mirror the existing
  `PublicRecipeOut` discipline).
- Recipe create/move: `create_recipe` takes a target `cookbook_id` (editor+),
  `move_recipe(recipe, to_cookbook)` (editor of both).

The `sharing` module is **absorbed into `cookbook`**: copy-on-share (`shares`)
and per-recipe public links stay as thin helpers (kept in `sharing` or moved into
`cookbook` — implementer's call, whichever keeps import-linter cleanest);
`SharedCookbook*` is gone. Import-linter contracts updated accordingly.

### Catalog → admin only

- Catalog **read/list/edit routes** (`catalog/router.py`) gain an
  `require_admin` dependency (reuse the users module's admin check). Canonical
  matching stays a server-internal call from the pipeline — unchanged and never
  user-facing.
- 403 for non-admins (not 404 — this is a known route, just privileged).

### Keeping the app alive during Phase 1

The existing frontend assumes a flat recipe list + separate Shares. To avoid a
broken window between phases:
- `GET /api/recipes` keeps returning the user's recipes across all cookbooks
  they can read (so today's Cookbook page still lists everything).
- New `GET/POST /api/cookbooks` endpoints are added alongside.
- The Shares endpoints return an empty/redirect-friendly shape (deprecated) so
  the old Shares page degrades gracefully rather than 500ing, until Phase 3
  removes it.

---

## Phase 2 — "Add a recipe" wizard (replaces Inbox)

Reconceive ingestion as one guided flow (`/add`):
1. **Source** — paste text / paste a link / upload PDF or photo / type it
   (the paste-first `/recipes/new` work already landed is the seed).
2. **Normalizing** — a progress state polling the job (the old inbox job-list
   becomes this transient status, plus a small "needs review" notification
   badge — not a top-level tab).
3. **Review** — the existing review gate, unchanged.
4. **Save to cookbook** — pick the destination cookbook (defaults to "My
   Cookbook"); editor access enforced.

"Inbox" disappears from nav. In-flight/failed jobs surface as a notification and
inside the wizard, not as a named section.

## Phase 3 — Visual redesign (warm, photo-forward magazine)

- Replace the hairline-box language: **soft shadows + rounded corners**, no
  bordered rectangles; generous whitespace; imagery leads.
- Keep the warm paper/terracotta palette + Fraunces display; retune tokens
  (add elevation scale, larger radii, remove the "hairlines not shadows" rule).
- **Cookbooks home**: photo-forward cards (cover image or generated cover),
  visibility badge, recipe count. Yours + shared-with-you on one page. No Shares
  tab.
- **Cookbook detail**: recipe photo grid, a single calm search, visibility +
  members controls; the nine-control filter bar is gone (optional quiet filter
  inside a cookbook only).
- **Generated covers**: text-only recipes/cookbooks get a deterministic warm
  gradient + dish initial / food glyph so the grid never looks broken.
- Nav becomes: **Cookbooks · Add a recipe · (Catalog — admins only)**.
- Apply via impeccable + ui-ux-pro-max during build; run the impeccable detector.

---

## Testing & rollout

- Phase 1: unit tests for the migration (default cookbook backfill; shared-
  cookbook migration; NOT NULL), the role/access matrix (viewer/editor/owner ×
  read/add/edit/manage), visibility + public-token, catalog admin-gate (403).
  Keep mypy --strict, ruff, import-linter green; regen OpenAPI client.
- Each phase is spec-referenced, plan-driven, delegated to agents, and verified
  before the next starts. The app stays runnable at every phase boundary.

## Open decisions deferred (not blocking)

- Public discovery / profiles / follow (fast-follow).
- Recipe in multiple cookbooks (additive later).
- Whether Collections survive long-term or fully fold into cookbooks.
