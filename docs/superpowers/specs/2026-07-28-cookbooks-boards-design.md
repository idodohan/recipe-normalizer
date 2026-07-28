# Cookbooks as Boards — recipe ↔ cookbook many-to-many (Design Spec)

**Date:** 2026-07-28
**Status:** approved (product direction: social network of recipes & cookbooks; controller decided the boards model)

## Why

The app is heading toward a social network of recipes and cookbooks. The
genre-defining structure is Pinterest-style **boards**: a recipe can live in
several cookbooks, and cookbooks are the shareable/followable objects. This
supersedes two earlier decisions that no longer fit:

- **"A recipe lives in exactly one cookbook"** (Phase 1) → a recipe may live in
  **many** cookbooks.
- **Collections as a separate concept** → collections **are** cookbooks; the
  standalone concept is retired and existing collections migrate to cookbooks.

It also dissolves the parked Phase-1 issue (a removed member losing access to a
recipe they contributed): with many-to-many, that recipe stays in the
contributor's own cookbook while also being in the shared one.

## Model

- **Join table `cookbook_recipes`**: `(cookbook_id, recipe_id)` composite PK,
  `added_by` (FK users, SET NULL), `added_at`. Replaces `recipes.cookbook_id`.
- `recipes.owner_id` **stays** — the creator/origin, authoritative for
  owner-only operations. A recipe is created into one cookbook (its owner's, by
  default) and may then be added to others.
- Every recipe is in **≥1** cookbook at all times (removing the last placement
  is not allowed via "remove from cookbook"; deleting the recipe is a separate
  owner action). Enforced in the service, not the DB.

## Access model (the crux — derived over ALL a recipe's cookbooks)

Let `cookbooks(R)` = the set of cookbooks a recipe R is in.

- **Read R**: allowed if the user can read *any* cookbook in `cookbooks(R)`
  (owner/member/public-or-unlisted-viewer of at least one), OR `R.owner_id ==
  user` (the creator can always read their own).
- **Edit R** (update fields): allowed if the user is editor+ (`editor`/`owner`
  role) of *any* cookbook in `cookbooks(R)`, OR `R.owner_id == user`.
- **Owner-only on R** (delete the recipe entirely; mint a public link; be the
  copy-on-share source): `R.owner_id == user` only — unchanged from Phase 1's
  `is_recipe_owner`.
- **Add R to a cookbook C** / **remove R from C**: the user must be editor+ of
  C. Adding requires read access to R (you can add a recipe you can see).
  Removing R from C never deletes R; it just drops the join row (blocked if C is
  R's last placement — offer "delete recipe" instead, owner-only).
- **Cookbook membership/visibility management**: owner-only (unchanged).

This replaces Phase-1's single-cookbook `_recipe_access`/`_require_recipe_access`
with a set-based derivation. `recipe_access(user, recipe)` returns the *highest*
role the user holds across `cookbooks(R)` (plus owner via owner_id).

## Save / pin semantics (social)

- **Your own recipe → your cookbook(s)**: add a join row (reference, no copy).
  A recipe can be in many of your cookbooks at once.
- **Someone else's public/shared recipe → your cookbook**: **copy-on-save**
  (reuse the existing copy-on-share machinery) — you get an independent copy you
  own, placed in your chosen cookbook. (Reference across owners is a later
  call; copy keeps ownership/edit-rights clean for v1 of the social model.)

## Migration (from the current one-cookbook + collections state)

1. Create `cookbook_recipes`; backfill one join row per recipe from its current
   `recipes.cookbook_id` (`added_by = recipe.owner_id`).
2. **Fold collections into cookbooks**: for each `collections` row, create a
   private cookbook (`owner_id = collection.owner_id`, `is_default = false`,
   name = collection name); for each `collection_recipes` row, add a
   `cookbook_recipes` join (recipe → that new cookbook) IF the user can edit the
   recipe (owner). A recipe already in its default cookbook is now *also* in the
   collection-derived cookbook — exactly the many-to-many win.
3. Drop `recipes.cookbook_id`; drop `collections` + `collection_recipes`.
4. Reversible downgrade recreates the column (assigning each recipe its first
   join-row cookbook) and empty collections tables.

Same additive-then-finalize split discipline as the Phase-1 migration to keep
intermediate deploys runnable, if the implementer finds it necessary; otherwise
one migration is acceptable since this is pre-launch.

## API / services

- `cookbook_recipes` operations: `add_recipe_to_cookbook(recipe_id,
  cookbook_id)`, `remove_recipe_from_cookbook(...)` (blocked on last placement),
  `list_cookbook_recipes(cookbook_id)` (unchanged shape).
- `create_recipe(cookbook_id=None)` → creates the recipe (owner) and one join
  row (default cookbook when omitted).
- `save_recipe_to_cookbook` (social): own recipe → join row; other's → copy.
- `recipe_access` / `require_recipe_access` reworked to the set-based rule.
- `GET /api/recipes` (global quick-find) spans recipes in any cookbook the user
  can read, de-duplicated (a recipe in several readable cookbooks appears once).
- `GET /api/cookbooks/{id}` unchanged (its recipes via the join).
- New: `PUT /api/recipes/{id}/cookbooks` or add/remove endpoints for placements;
  the recipe detail page gets a "Save to cookbook(s)" control (multi-select).
- Retire `/api/collections*`.

## IA / frontend (follow-on, after the model lands)

- **In-cookbook search primary**: full-text within a cookbook (not just title).
- **Global search demoted** to a light quick-find (top bar / home), personal.
- **Collections UI retired**; the home's collection chips go away (they're
  cookbooks now). Favorites stays as a personal lens.
- Recipe detail: "Save to cookbooks" multi-select (which of my cookbooks this
  recipe is in) replacing the collections control.

## Non-goals (this phase)

Public discovery feed, follow, profiles (the next social phase — this is the
data foundation they need). Cross-owner *reference* (we copy-on-save for now).

## Testing

Migration (backfill join rows; collections→cookbooks; drop; round-trip);
the set-based access matrix (read/edit/owner across 0/1/many cookbooks,
public/unlisted/private mixes, owner-not-a-member); last-placement removal
block; copy-on-save vs reference; global-list de-dup; import-linter + mypy +
ruff green; OpenAPI client regen.
