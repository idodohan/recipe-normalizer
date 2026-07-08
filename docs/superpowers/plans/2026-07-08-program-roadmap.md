# "Make It Very Good" Program Roadmap

Owner decisions (2026-07-08): friends-and-family scale; Hebrew/RTL deferred to a later phase;
shared cookbooks use last-write-wins (spec §7 as written). Focus on UI/UX, but cover everything.

This roadmap is the durable memory spine for a long autonomous run (loop-engineering).
Each phase gets its own full plan document written when the phase starts.

## Phases

| Phase | Plan doc | Contents | Status |
|---|---|---|---|
| 0 | `2026-07-08-plan-3-hardening.md` | SSRF guard, catalog authz (admin role), per-job LLM cost cap, rate limiting, poison-job cap, 500-handler + request logging, config-driven CORS, upload magic-byte sniffing + image-bomb guards, correctness nits | done (merged 2026-07-08, 15 commits, final review clean) |
| 1 | `2026-07-08-plan-4-ux-systems.md` | UX systems + product gaps (THE FOCUS): edit-saved-recipe flow, toast layer, skeletons + retry affordances, auth-loading fix, recipe detail image + manual image upload, personal notes + favorites, Catalog page, mobile/fluid type + reduced-motion, dark mode token set, a11y (tabs/combobox/focus), QueryClient defaults + shared hooks + centralized error parsing | done (merged 2026-07-08, 17 commits, final review clean; follow-ups for later: X-Content-Type-Options nosniff header, color-scheme tracks manual theme, image blob GC + rate limit on image PUT) |
| 2 | `2026-07-09-plan-5-search-collections-sharing.md` | Cookbook entity, search/filters (FTS + pg_trgm so Hebrew works later), collections, copy-on-share, public links, shared cookbooks (LWW) | done (merged 2026-07-09, ~24 commits; adversarial authz review passed. NOTE for owner: transitive re-share accepted — a member of cookbook A can add the owner's recipe to a disjoint cookbook B without owner consent; close later by gating add-to-cookbook on owner-only if undesired. Follow-ups: nosniff header on file routes, whitespace-name validator, >200 select truncation in shared-cookbook add) |
| 3 | `2026-07-09-plan-6-ai-layer.md` | Per-recipe chat, cookbook Q&A (tool-use over search), transformations via review gate + derived_from, content-based recommendations | not started |
| ops | (when hosting) | TLS/domain, prod compose overlay, S3 filestore, backups, error tracking, CI security scanning | not started |

## Recipe-model completeness verdict (audit 2026-07-08)

Model already covers: title, description, image_ref, source + source_type, language,
servings (amount + free-text unit), prep/cook/total minutes, cuisines, dish types, tags,
dietary via ingredient flags, grouped ingredient lines with dual quantities + optional + note,
steps with `ingredient_line_refs`. Missing for "very good": **personal notes** on a recipe and
**favorites/pinning** (both added in Phase 1). Nutrition, meal planning, cook mode stay non-goals.

## Standing constraints

- Never break the deterministic core: unit parsing/conversion/scaling stay code, never LLM.
- Dual-quantity display rule is inviolable (original never hidden).
- All CI gates must stay green per task: ruff, mypy --strict, lint-imports, pytest,
  web typecheck, generated-client drift (`make client` after any API change), Playwright E2E.
- Keep the editorial design language (paper/ink/terracotta, hairlines, Fraunces) — evolve, don't replace.
- TDD per task; commit per task; merge to main only with all gates green.
