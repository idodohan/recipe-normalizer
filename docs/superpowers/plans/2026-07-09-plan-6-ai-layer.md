# Phase 3: AI Layer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement spec §7's AI features — per-recipe chat, cookbook Q&A, transformations, content-based recommendations — completing product goal C (the "modification/Q&A LLM layer").

**Architecture:** New `ai` module (`src/recipe_normalizer/ai/`) owning conversations + messages, talking to cookbook only via `cookbook.service`. All LLM calls go through the existing `LLMClient` (structured/classify_bool/tool_loop already exist) with per-user/per-feature cost logging via the existing `LlmUsage` ledger. Chat is grounded strictly on recipe JSON the user can access; cookbook Q&A uses `tool_loop` over the Plan-2 search API (never dumps the cookbook into context); transformations produce a new draft recipe routed through the EXISTING ingestion review gate via `derived_from`. This runs in the API process (not the worker) since these are interactive request/response — but the shared per-request cost cap and rate limits apply.

**Tech Stack:** as prior phases. LLM: `settings.llm_model` (opus) for chat/transform/Q&A, `llm_fast_model` where cheap. SSE optional — v1 uses request/response (spec allows either; polling/blocking is simpler and fine at friends-scale).

## Global Constraints

- All CI gates green per task (backend gates, web typecheck, client drift via `make client`, E2E stays green). LLM-touching tests use a stub/fake LLMClient (mirror the worker's `_StubLLMClient` and the golden-corpus `GoldenStubLLM` pattern — NO real API calls in CI); one opt-in live smoke test gated on an env flag is allowed, never in CI.
- Module boundaries: `ai` imports `cookbook.service`/`schemas`, `sharing.service` (for access checks), `llm.client`, `users.service`; NOTHING imports `ai`. Update `.importlinter` with new contracts mirroring `sharing`'s.
- **Grounding & no-fabrication is the core quality bar.** Chat/Q&A answer ONLY from the provided recipe(s); when the recipe doesn't contain the answer, the model must say so, not invent. Every AI feature's system prompt states this explicitly. Transformations may propose changes but must not silently drop the "never fabricate quantities" rule — the produced draft goes through human review exactly like an extraction.
- **Access control**: chat/Q&A/transform on a recipe require the SAME access as reading it — reuse `cookbook.service.user_recipe_access` (owner or shared-cookbook member). A public-token viewer gets NO AI (out of scope). Conversations are per-(user, recipe); a user never sees another user's conversation.
- **Cost & abuse**: every AI endpoint is rate-limited (`limit_by_user`); each call is bounded by a per-request `LLMClient` cost cap (reuse `settings` — add `ai_request_cost_cap_usd: float = 0.50`); log every call to `LlmUsage` with the feature name. A conversation has a max message count (e.g. 50) to bound context growth.
- Deterministic core untouched: transformations that change quantities still run through `normalize` + the review gate; scaling/conversion stay code.
- Editorial design language for all new UI; reuse Phase 1 systems (toasts, skeletons, Dialog, dark tokens, Tabs).

## Data model (ai/models.py)

- `Conversation {id, user_id FK, recipe_id FK recipes CASCADE (nullable — null = cookbook-wide Q&A), kind: Enum(recipe_chat, cookbook_qa), title (nullable), created_at, updated_at}`.
- `Message {id, conversation_id FK CASCADE, role: Enum(user, assistant), content: Text, created_at, token cost fields optional (or rely on LlmUsage)}`.
- Migration; index `(user_id, recipe_id, kind)`.

---

### Task 1: `ai` module scaffold + conversation storage

**Files:** create `src/recipe_normalizer/ai/{__init__,models,schemas,service,router}.py`; `.importlinter` contracts; `main.py` include; migration; tests.

Service: `create_conversation`, `get_conversation` (owner-scoped 404), `list_conversations(user, recipe_id?)`, `append_message`, `enforce_message_cap`. No LLM yet — just storage + access. Router: `GET/POST /api/ai/conversations`, `GET /api/ai/conversations/{id}`. Tests: ownership 404, cap enforcement, cascade on recipe delete. `make client`.

### Task 2: Per-recipe chat

**Files:** ai/service.py (`chat_turn`), ai/router.py (`POST /api/ai/conversations/{id}/messages {content}` → assistant Message), ai/prompts.py (RECIPE_CHAT_SYSTEM), config (`ai_request_cost_cap_usd`), tests.

**Interfaces:** `chat_turn(db, *, user_id, conversation_id, content, llm)` — verifies access to the conversation's recipe via `user_recipe_access` (404 if lost), loads the recipe JSON (the FULL owner view for grounding — but this is the user's own or a shared recipe they can read; personal fields irrelevant to cooking Q&A, include ingredients/steps/times), builds a grounded prompt (system = "You answer questions about THIS recipe only, grounded in the JSON below. If the recipe doesn't contain the answer, say so. Never invent quantities or steps."), appends prior messages (bounded), calls `llm.structured` or a plain message create, persists both user + assistant messages, returns the assistant Message. Rate-limit `limit_by_user("ai_chat", 60, 3600)`; per-request cost cap. Fake-LLM tests: grounded answer, refusal-to-fabricate when asked something absent, access-lost 404, cost-cap abort, message-cap. The LLM seam must be injectable (dependency) so tests never hit the network — follow how the worker injects/ stubs LLMClient.

### Task 3: Chat UI (recipe detail panel)

**Files:** web RecipeDetailPage — a "Ask about this recipe" collapsible chat panel (reuse Dialog or an inline panel); message list (user/assistant bubbles, editorial styling — NOT generic chat-app gradient; hairline-framed, Fraunces for the assistant's first line maybe), input + send, streaming NOT required (show a skeleton/typing indicator while awaiting), error toast, conversation persists (load latest conversation for this recipe on open). a11y: the log is `aria-live="polite"`, input labeled, ≥44px send. Tokens only. Typecheck + E2E stays green.

### Task 4: Cookbook Q&A (tool-use over search)

**Files:** ai/service.py (`cookbook_qa_turn` using `llm.tool_loop`), ai/router.py (`POST /api/ai/cookbook-qa {content, conversation_id?}`), ai/prompts.py (COOKBOOK_QA_SYSTEM + tool defs), tests.

**Interfaces:** the tool_loop exposes ONE tool `search_recipes(query?, cuisine?, dish_type?, tag?, dietary?, max_total_min?, favorites?)` whose `execute` callback calls `cookbook.service.list_recipes` scoped to THIS user (owner-only list — Q&A is over your own cookbook; note shared recipes aren't in the personal list, acceptable v1) and returns compact JSON (id, title, cuisines, dish_types, total_min, a few ingredient names — NOT full recipes, to bound tokens). System prompt: "Answer using ONLY search results; call the tool to find recipes; cite recipe titles; if nothing matches, say so." Bounded iterations (reuse tool_loop max_iterations, lower it — e.g. 6). Rate-limit + cost cap. Fake-LLM tests: a scripted tool_loop (fake LLMClient that returns a tool_use then an answer) proving the tool is called with parsed args and the answer only references returned recipes; zero-results path.

### Task 5: Cookbook Q&A UI

**Files:** a Q&A surface — either a section on CookbookPage ("Ask your cookbook") or a small route; message list + input reusing the Task-3 chat components (extract them to `web/src/ai/` shared components in Task 3 so both reuse). Show which recipes the answer referenced as clickable chips linking to detail. Skeletons/toasts/a11y. Typecheck + E2E.

### Task 6: Transformations

**Files:** ai/service.py (`transform_recipe`), ai/router.py (`POST /api/ai/recipes/{recipe_id}/transform {instruction}` e.g. "make it vegan", "halve it", "air-fryer version"), ai/prompts.py (TRANSFORM_SYSTEM), tests. Integration: reuse the extraction `normalize`/`RecipeIn` shape and the ingestion review gate.

**Interfaces:** `transform_recipe(db, *, user_id, recipe_id, instruction, llm)` — access check; loads the recipe; prompts the LLM to produce a transformed recipe as the SAME structured `RecipeIn`/normalize output the extraction pipeline uses (reuse the schema — DRY), with the no-fabrication rules; then routes it through the EXISTING review path so it lands as a draft the user reviews and accepts, `derived_from = recipe_id`, `provenance = {transformed_from: recipe_id, instruction, at}`. DECISION to make while implementing: does transform create an ingestion Job (so it appears in the Inbox review screen — most consistent) OR a direct unverified recipe with a review redirect? Prefer reusing the ingestion review gate (Job with a new input_type "transform" or a synthetic source) so the existing Review screen handles it with zero new UI — read ingestion/service.py + worker to see if a job can be seeded with a prebuilt NormalizeResult (tier-1 already does prebuilt) and pick the least-invasive path; document it. Rate-limit + cost cap (transforms are the most expensive — cap tighter or same). Fake-LLM tests: transform produces a valid draft through the gate with derived_from set; invalid LLM output rejected; access 404; scaling-type transform ("halve") — decide whether to LLM it or refuse in favor of deterministic scaling (PREFER: "halve it" should be answered by pointing to the deterministic scale feature, NOT the LLM — the transform LLM is for qualitative changes like substitutions; document this boundary and have the prompt/endpoint steer quantity-only scaling to the scale view).

### Task 7: Transformation UI

**Files:** RecipeDetailPage — a "Transform" control (button → Dialog: instruction input + examples "make it vegan", "gluten-free", "for an air fryer"); on submit, calls transform, then navigates to the review screen (or inbox) for the produced draft; toast. Make the deterministic-scaling boundary visible (if the user types "halve"/"double", nudge them to the existing scale control). a11y, tokens. Typecheck + E2E.

### Task 8: Content-based recommendations

**Files:** ai/service.py (`recommendations_for_recipe` / `for_user`), ai/router.py (`GET /api/ai/recipes/{id}/similar`), tests. This is NOT collaborative filtering (explicit non-goal) and does NOT need an LLM — it's content similarity over the user's OWN cookbook: score by shared cuisine + dish_type + tag + canonical-ingredient overlap (reuse the search/filter infra — a weighted overlap query, deterministic, cheap). Return top-N RecipeSummary "More like this". Optional LLM one-liner "why" is out of scope v1 (keep it deterministic + free). UI: a "More like this" row on RecipeDetailPage. Tests: overlap scoring ranks correctly, excludes the recipe itself, owner-scoped. `make client`.

### Task 9: E2E + phase gate

New `e2e/tests/ai.spec.ts` with the LLM STUBBED (set an env like the worker's RN_LLM_STUB, or a dedicated RN_AI_STUB, that makes ai.service use a canned LLMClient returning deterministic responses — build this stub in Task 2 and reuse): recipe chat returns a grounded answer; cookbook Q&A returns an answer referencing a seeded recipe; transform produces a reviewable draft with derived_from; recommendations row shows a related recipe. Full gates; final whole-branch review (opus) incl. a grounding/no-fabrication + cost-cap + access-control sweep; fix wave; roadmap update; merge. Then the program's four phases are complete — update the roadmap's ops-track section with the accumulated follow-ups (nosniff header, TLS/domain, S3 filestore, backups, Sentry, CI security scanning, transitive-reshare decision) as the remaining pre-public-launch checklist.
