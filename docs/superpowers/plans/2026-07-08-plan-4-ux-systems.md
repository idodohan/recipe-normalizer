# Phase 1: UX Systems & Product Gaps Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the UX-system gaps found in the 2026-07-08 audit — feedback layer, loading/error affordances, edit flow, favorites/notes, images, catalog surface, dark mode, mobile/fluid type, a11y — while keeping the editorial design language exactly as it is.

**Architecture:** Frontend work in `web/` (React 19 + TS + Vite + TanStack Query 5, plain CSS with tokens in `web/src/styles/tokens.css`). Two small backend additions (favorites/notes columns; recipe image upload endpoint) follow the existing module conventions in `src/recipe_normalizer/cookbook/`. Every visual addition uses existing tokens — hairlines not shadows, Fraunces/Inter, terracotta accent.

**Tech Stack:** React 19, TypeScript strict, TanStack Query 5, openapi-fetch generated client, plain CSS custom properties. Backend: FastAPI/SQLAlchemy/Alembic as in Phase 0.

## Global Constraints

- Keep the editorial design language: tokens from `web/src/styles/tokens.css` only; hairline borders (`--hairline`), small radii (`--radius-sm/md`), NO box-shadows, Fraunces display / Inter body. New CSS follows the existing per-feature-file + BEM-ish convention.
- All CI gates green per task: backend `uv run pytest -q && uv run ruff check && uv run mypy src && uv run lint-imports`; frontend `cd web && npm run typecheck`; after any backend API change run `make client` and commit `web/openapi.json` + `web/src/api/schema.d.ts`.
- A11y baseline (from ui-ux-pro-max): toasts use `aria-live="polite"` and never steal focus, auto-dismiss 4s; body text ≥16px on mobile; touch targets ≥44px; visible focus states (the existing `--focus-ring` var); `prefers-reduced-motion` disables transitions/animations; color never the sole indicator.
- Dark mode: desaturated warm tonal variants (never pure inversion); every text/background pair ≥4.5:1 (verify with a contrast computation in a unit test or documented manual check).
- Deterministic core untouched: no changes to conversion/scaling logic.
- E2E must stay green; new flows (edit, favorite) get E2E coverage in the final task.

---

### Task 1: Frontend infrastructure — QueryClient defaults, shared hooks, one error parser

**Files:**
- Modify: `web/src/main.tsx` (QueryClient config), `web/src/api/errors.ts` (extend), `web/src/pages/RecipeEditorPage.tsx`, `web/src/review/DraftEditor.tsx`, `web/src/inbox/SubmitPanel.tsx`, `web/src/pages/InboxPage.tsx`, `web/src/inbox/JobList.tsx`
- Create: `web/src/hooks/useVocab.ts`, `web/src/hooks/useJobs.ts`

**Interfaces:**
- Produces: `useVocab()` (5-min staleTime, replaces the duplicated vocab queries in RecipeEditorPage + DraftEditor); `useJobs({poll}: {poll?: boolean})` single owner of the `["jobs"]` key (poll=true adds the 2500ms refetchInterval logic; InboxPage uses poll:false or shares JobList's); `apiErrorMessage(body, fallback)` unchanged plus new `apiErrorEnvelope(body): {code?: string, message?: string, existingId?: string, jobId?: string}` consolidating the ad-hoc parsers in SubmitPanel (`readEnvelope`) and RecipeEditorPage (`extractExistingId`).
- QueryClient: `new QueryClient({defaultOptions: {queries: {staleTime: 30_000, retry: 1}}})`.

Steps: (1) typecheck-driven refactor — no behavior change intended; move duplicated code, update imports; (2) `npm run typecheck` green; (3) manually verify dev server still renders cookbook + inbox (`npm run dev` smoke or rely on E2E in final task); (4) commit `refactor(web): shared query hooks, client defaults, one error parser`.

---

### Task 2: Toast layer

**Files:**
- Create: `web/src/components/Toaster.tsx`, `web/src/components/toaster.css`, `web/src/hooks/useToast.ts`
- Modify: `web/src/main.tsx` (mount `<Toaster/>` inside the QueryClientProvider), wire into: `web/src/inbox/SubmitPanel.tsx` (submit success: "Added to the inbox"), `web/src/review/DraftEditor.tsx` (saved / accepted / rejected — replace the never-resetting "Saved ✓" label state), `web/src/pages/RecipeDetailPage.tsx` (deleted), `web/src/pages/InboxPage.tsx` (accept-all result), `web/src/pages/RecipeEditorPage.tsx` (created).

**Interfaces:**
- `useToast()` returns `{toast: (opts: {title: string; description?: string; variant?: "success" | "error"}) => void}` via a module-level store (subscribe pattern; no context re-render storm). `<Toaster/>` renders a fixed bottom-right stack (bottom-center on mobile), `role="status"` container with `aria-live="polite"`, max 3 visible, auto-dismiss 4000ms with a pause-on-hover, manual dismiss button (44px target, `aria-label="Dismiss"`).
- Styling: paper surface (`--color-surface-raised`), `--hairline` border with a 2px ink top rule (matches the recipe-card motif); success variant gets a terracotta top rule, error gets `--color-error`. Enter/exit: translateY+opacity 200ms ease-out, disabled under `prefers-reduced-motion`. No shadows.

Steps: TDD-lite for the store (a small vitest is NOT set up in this repo — instead assert via typecheck + an E2E assertion in the final task; keep the store logic trivially simple); implement; wire the six call sites (remove the `savedAt` label hack in DraftEditor); typecheck; commit `feat(web): toast layer with aria-live, wired into all mutations`.

---

### Task 3: Loading skeletons, retry affordances, auth-flash fix

**Files:**
- Create: `web/src/components/Skeleton.tsx`, `web/src/components/skeleton.css`, `web/src/components/ErrorState.tsx`
- Modify: `web/src/pages/CookbookPage.tsx`, `web/src/pages/RecipeDetailPage.tsx`, `web/src/pages/ReviewPage.tsx`, `web/src/inbox/JobList.tsx`, `web/src/layouts/AuthenticatedApp.tsx`

**Interfaces:**
- `<Skeleton variant="card-grid" | "detail" | "rows" count?>` — paper-tone shimmer blocks (`background: linear-gradient` sweep animation, disabled under reduced-motion → static block), matching the real layouts' dimensions to avoid CLS (card grid mirrors `.cookbook-grid` cell heights).
- `<ErrorState message onRetry?>` — EmptyState-style block with the error message and a secondary Button "Try again" calling the query's `refetch`. Replace every dead-end error block (CookbookPage:44-52, JobList, ReviewPage, RecipeDetailPage) with it.
- `AuthenticatedApp` loading: render a full-viewport paper screen with the wordmark (small, centered, Fraunces) instead of `null` — visible to AT via `role="status"` "Signing you in…".

Steps: implement; replace text-only loading states in the five files; typecheck; commit `feat(web): skeletons, retry affordances, auth loading screen`.

---

### Task 4: Edit-saved-recipe flow

**Files:**
- Modify: `web/src/main.tsx` (route `/recipes/:id/edit`), `web/src/pages/RecipeDetailPage.tsx` (Edit button in the header actions, next to Delete), `web/src/pages/RecipeEditorPage.tsx` (accept an edit mode) OR Create: `web/src/pages/RecipeEditPage.tsx` if cleaner — decide by reading how `useRecipeForm`/`draft.ts` initialize state; the review `DraftEditor` already maps a recipe → form state (`web/src/review/DraftEditor.tsx` + `web/src/recipe/draft.ts`) — REUSE that mapping (export it from draft.ts as `draftFromRecipe(recipe)`), do not reimplement.

**Interfaces:**
- Route `/recipes/:id/edit` loads the recipe (`["recipe", id]`), initializes the shared `RecipeFormFields` via `draftFromRecipe`, PATCHes `/api/recipes/{recipe_id}` on save, invalidates `["recipe", id]` + `["recipes"]`, toasts "Recipe updated", navigates to `/recipes/:id`.
- Cancel returns to detail without saving. Unsaved-changes guard: `onBeforeUnload` is out of scope; a simple confirm on Cancel when dirty is enough (`draft !== initial` shallow compare provided by `useRecipeForm` if present, else skip — do not overbuild).

Steps: read draft.ts/DraftEditor first; implement; typecheck; manual smoke via dev server; commit `feat(web): edit flow for saved recipes`.

---

### Task 5: Backend — favorites + personal notes

**Files:**
- Modify: `src/recipe_normalizer/cookbook/models.py` (Recipe: `is_favorite: Mapped[bool]` default False server_default "false"; `notes: Mapped[str | None]` Text nullable), `src/recipe_normalizer/cookbook/schemas.py` (RecipeOut + a new `RecipePersonalPatch` or extend the existing patch schema — read how PATCH /api/recipes works first; favorites/notes must be PATCHable WITHOUT sending the full destructive-replace payload — add a lightweight `PATCH /api/recipes/{id}/personal` endpoint `{is_favorite?: bool, notes?: str|null}` to avoid touching the full-replace semantics), `src/recipe_normalizer/cookbook/router.py`, `src/recipe_normalizer/cookbook/service.py` (`set_personal(db, user_id, recipe_id, *, is_favorite=UNSET, notes=UNSET)`)
- Create: alembic migration (autogenerate)
- Test: `tests/cookbook/test_service.py`, `tests/cookbook/test_router.py` (TDD: favorite toggle persists; notes set/clear; 404 on other owner's recipe; full PATCH replace does NOT clear favorites/notes — they are personal metadata outside the replace)

Steps: TDD; migration; `make client` + commit regenerated files; full gates; commit `feat(cookbook): favorites and personal notes`.

---

### Task 6: Favorites + notes UI

**Files:**
- Modify: `web/src/recipe/RecipeCard.tsx` (heart toggle top-right, optimistic), `web/src/pages/RecipeDetailPage.tsx` (heart in header + Notes section), `web/src/pages/CookbookPage.tsx` (filter chip row: All | Favorites), relevant CSS files.

**Interfaces:**
- Heart: an SVG (inline, stroke style matching hairline aesthetic — outline default, filled terracotta when favorited), button ≥44px hit area, `aria-pressed`, `aria-label="Add to favorites"/"Remove from favorites"`. Optimistic update: `onMutate` patches the `["recipes"]` and `["recipe", id]` caches, rolls back on error with an error toast.
- Notes on detail: a "Kitchen notes" section (Fraunces heading, hairline-top) with an auto-growing textarea, saved on blur via the personal PATCH, "Saved" toast suppressed (silent save; show only errors) — notes editing should feel like marginalia, not a form.
- Cookbook filter: client-side filter chips (no backend filter yet — Plan 5 adds real search); chip = existing `.chip` style with `aria-pressed`.

Steps: implement; typecheck; commit `feat(web): favorites and kitchen notes`.

---

### Task 7: Recipe images — display + manual upload

**Files:**
- Backend: `src/recipe_normalizer/cookbook/router.py` + `service.py`: `PUT /api/recipes/{id}/image` (multipart, reuse `ingestion/sniff.py` allowlist minus pdf — images only, 10MB cap, store via FileStore, set `image_ref`; `DELETE /api/recipes/{id}/image` clears it). Tests in `tests/cookbook/test_router.py` (sniff rejection, ownership 404, happy path). NOTE import-linter: cookbook may not import ingestion — if `sniff.py` placement blocks reuse, move `sniff.py` to top-level `src/recipe_normalizer/sniff.py` (it is dependency-free) exactly like netguard, updating ingestion imports.
- Frontend: `web/src/pages/RecipeDetailPage.tsx` — render the image (when `image_url`) as a full-bleed banner above the title (lazy, `aspect-ratio` reserved to avoid CLS, `alt=""` decorative) with hover/focus controls "Replace photo" / "Remove"; `web/src/pages/RecipeEditorPage.tsx` + edit flow — image picker in the form's Essentials section (file input styled as a secondary button, preview thumbnail).
- `make client` after backend change.

Steps: backend TDD → client regen → frontend; full gates; commit(s) `feat(cookbook): recipe image upload endpoint` / `feat(web): recipe images in detail and editor`.

---

### Task 8: Catalog page

**Files:**
- Modify: `web/src/pages/CatalogPage.tsx` (replace stub), new `web/src/pages/catalog.css`
- Uses existing endpoints: `GET /api/catalog/ingredients?q=&status=&limit=`, `PATCH .../{id}` + `/merge` (admin-gated as of Phase 0; `useUser()` now has `is_admin`).

**Interfaces:**
- Browse: search input (debounced 300ms) → `["catalog", q]` query; results as a table-like list (name, category, density g/ml, status chip, dietary flags) in the editorial style (2px ink header rule, hairline rows, `tabular-nums` for densities).
- Detail/edit: clicking a row expands an inline panel (aliases, gram weights). If `user.is_admin`, show editable density/status controls (PATCH on save + toast); non-admins see read-only with a muted "Curated by admins" note.
- Empty query shows a short explainer of what the catalog is ("The shared ingredient library that powers unit conversion") + the seeded count.

Steps: implement; typecheck; commit `feat(web): catalog browser with admin editing`.

---

### Task 9: Dark mode

**Files:**
- Modify: `web/src/styles/tokens.css` (dark token set), `web/src/styles/base.css` (color-scheme), `web/src/layouts/AppShell.tsx` (+css: theme toggle button in the top bar, sun/moon inline SVG, 44px, `aria-label`), Create: `web/src/hooks/useTheme.ts`
- Audit sweep: grep all `web/src/**/*.css` for raw hex values outside tokens.css — any found must be replaced with a token first (the audit says components use tokens, but verify; known suspects: recipe-card alternating ink/terracotta, error washes).

**Interfaces:**
- Token architecture: `:root { …existing light tokens… }` + `:root[data-theme="dark"] { …overrides… }` + `@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { …same overrides via a shared definition… } }` — implement by defining the dark values once in a `@` block comment-documented section; acceptable duplication if a CSS var indirection would be convoluted, but prefer defining dark overrides in ONE place and applying via both selectors (e.g. duplicate the block with a build-free copy — keep the two literal blocks adjacent with a "keep in sync" comment).
- Dark palette (warm, desaturated, editorial — NOT inverted): paper `#181410`, surface-raised `#201b16`, ink/text `#ece5da`, text-muted `#b3a89a`, text-faint `#8c8174`, hairline `#3a332b`, accent `#e0764a` (terracotta lightened for ≥4.5:1 on paper), accent-deep `#e98d5f`, accent-wash `#31221a`, error `#e57368`, error-wash `#2e1b18`, neutral scale flipped to warm darks. Verify contrast: body text pairs and accent-on-paper ≥4.5:1 (document computed ratios in the commit message or a comment).
- `useTheme()`: `{theme: "light"|"dark"|"system", setTheme}`, persists to localStorage `rn-theme`, stamps `data-theme` on `document.documentElement` ("system" removes the attribute). Toggle cycles light→dark→system? NO — keep binary light/dark with system as the default until the user first toggles (YAGNI).
- `base.css`: `color-scheme: light dark;` on root so form controls/scrollbars adapt.

Steps: hex-audit sweep first; tokens; hook+toggle; visually smoke both themes on cookbook/detail/inbox/review via dev server; typecheck; commit `feat(web): dark mode — warm editorial dark token set + toggle`.

---

### Task 10: Mobile/fluid type, reduced-motion, a11y hardening

**Files:**
- Modify: `web/src/styles/tokens.css` + `base.css` (fluid type), all CSS files with transitions (reduced-motion), `web/src/pages/ReviewPage.tsx` + `web/src/review/SourcePanel.tsx` (tabs), `web/src/recipe/VocabMultiSelect.tsx` (combobox), `web/src/layouts/AppShell.tsx` (skip link)

**Interfaces:**
- Fluid type: `--text-display: clamp(2rem, 5vw + 1rem, 2.75rem)`; `--text-title: clamp(1.5rem, 3vw + 0.75rem, 1.875rem)`; body bumps to `1rem` (16px) at `max-width: 640px` (audit: current base 15px; keep 15px desktop, 16px mobile via media query on `html`).
- Reduced motion: one global block in `base.css`: `@media (prefers-reduced-motion: reduce) { *, *::before, *::after { animation-duration: 0.01ms !important; animation-iteration-count: 1 !important; transition-duration: 0.01ms !important; } }`.
- Tabs (ReviewPage draft tabs + SourcePanel source tabs): proper `tablist/tab/tabpanel` triples (`aria-controls`, `id`s, `aria-selected`), roving tabindex (selected tab tabIndex=0, others -1), ArrowLeft/ArrowRight/Home/End handling. Extract a tiny shared `useTabs(count)` hook or a `<Tabs>` component in `web/src/components/Tabs.tsx` used by both.
- Combobox (VocabMultiSelect): `role="combobox"` + `aria-expanded` + `aria-activedescendant`; ArrowDown/ArrowUp move the active option (visual highlight via existing hover style), Enter selects active, Escape closes (already), options get `id`s + `role="option"` + `aria-selected`. Keep existing Enter-to-add-free-text and Backspace-chip behaviors.
- Skip link: visually-hidden-until-focused "Skip to content" as first element in AppShell targeting `#main`; add `id="main"` + `tabIndex={-1}` to the main content wrapper; on route change, focus `#main` (small effect in AppShell watching location).

Steps: implement; keyboard-walk every widget manually via dev server (document what was walked in the report); typecheck; commit `feat(web): fluid type, reduced motion, keyboard-complete tabs and combobox, skip link`.

---

### Task 11: Phase gate — E2E coverage + full verification + merge

- Extend `e2e/tests/happy-path.spec.ts` (or a new `ux.spec.ts` following its serial style): edit a saved recipe (change title, save, assert detail shows it), favorite it (assert `aria-pressed`), add a kitchen note (blur, reload, assert persisted), toggle dark mode (assert `data-theme="dark"` on html), toast visible after an action.
- Run everything: backend gates, `npm run typecheck`, client-drift check, full E2E.
- Final whole-branch review (most capable model) incl. dark-mode contrast + a11y spot checks; one fix wave; re-verify.
- Update roadmap Phase 1 → done; merge branch `ux-phase-1` to main.
