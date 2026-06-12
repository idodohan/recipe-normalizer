# Plan 2: Extraction Pipeline — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship spec §6 + §11.3: ingest recipes from URL / PDF / image / pasted text via a job queue + worker, a tiered extraction pipeline (JSON-LD → readable-HTML+LLM → agentic browser), one shared LLM normalize stage producing 1..N draft recipes, guardrails (not-a-recipe, cost caps, dedupe), the Inbox + review screen, and recipe images.

**Architecture:** Two new modules per spec §4 — `ingestion` (owns inputs/jobs tables, job lifecycle, review acceptance) and `extraction` (stateless acquire+normalize pipeline; every input type is an `Extractor` plugin). Two new shared-infra seams per spec §10 — `FileStore` (local disk now, S3 later) and `LLMClient` (single wrapper owning model ids, retries, structured output, vision, tool loops, token/cost logging). The worker is the same codebase run as a separate process, claiming jobs via Postgres `FOR UPDATE SKIP LOCKED`.

**Tech stack additions:** `anthropic` (official SDK), `httpx`, `trafilatura` (readable-text extraction), `pypdf` + `pypdfium2` (PDF text layer + page rendering), `playwright` (agentic browser, chromium), `rapidfuzz` (fuzzy catalog matching).

**LLM facts (verified against current API docs — do not "correct" these):** default capable model id is `claude-opus-4-8`; cheap/fast classifier is `claude-haiku-4-5`. Pricing per MTok: opus 4.8 $5 in / $25 out; haiku 4.5 $1 in / $5 out. Use `client.messages.parse(..., output_format=PydanticModel)` → `.parsed_output` for structured output. Opus 4.8 rejects `temperature`/`top_p`/`top_k` and `budget_tokens`; use `thinking={"type": "adaptive"}` when thinking is wanted (omit for extraction calls). Vision = content blocks `{"type": "image", "source": {"type": "base64", "media_type": ..., "data": ...}}`. Tool loop = manual loop on `stop_reason == "tool_use"`, appending assistant content + tool_result blocks. SDK retries 429/5xx automatically (`max_retries`).

**Spec:** `docs/superpowers/specs/2026-06-12-recipe-normalizer-design.md` (§6 is the heart of this plan; also §5 image_ref/dedupe, §7 review, §8 errors). Where plan and spec disagree, spec wins.

**Conventions:** same as Plan 1 (uuid PKs, tz-aware DateTime(timezone=True), services flush / get_db commits, owner-scoped queries, ApiError envelope, import-linter: cross-module via service/schemas only, TDD with the real-Postgres `db_session` fixture, mypy strict, ruff 100). ALL LLM calls in tests are mocked at the `LLMClient` seam (never at the anthropic SDK level outside `tests/llm/`); a small live-API smoke suite is opt-in behind `RN_LIVE_LLM_TESTS=1`.

**UI quality bar:** unchanged from Plan 1 (editorial cookbook; tokens in `web/src/styles/tokens.css`). Frontend tasks MUST invoke `frontend-design:frontend-design`.

---

### Task 1: FileStore seam

**Files:** Create `src/recipe_normalizer/filestore.py`; test `tests/test_filestore.py`.

- [ ] Failing tests first: `LocalFileStore(root)` — `save(data: bytes, *, suffix: str) -> str` returns an opaque ref (`sha256[:16]/sha256.suffix` path layout, content-addressed: saving identical bytes twice returns the same ref); `open(ref) -> bytes`; `exists(ref)`; `url_path(ref) -> str` (the API route path `/api/files/{ref}`); rejects path-traversal refs (`..`) with ValueError; `delete(ref)` idempotent.
- [ ] Implement: `class FileStore(Protocol)` with those methods + `class LocalFileStore` reading root from `settings.file_store_root`; module-level `get_file_store() -> FileStore` factory (env-driven later for S3 per spec §10). Refs are relative POSIX paths, validated against the root on open.
- [ ] Serve files: add to `main.py` an auth-required `GET /api/files/{ref:path}` returning `FileResponse` with content-type guessed from suffix; 404 ApiError on missing/invalid ref. Test via TestClient.
- [ ] Gates green; commit `feat: FileStore seam (local disk) + file serving route`.

### Task 2: LLMClient wrapper + usage logging

**Files:** Create `src/recipe_normalizer/llm/__init__.py`, `llm/client.py`, `llm/models.py` (usage table), `llm/pricing.py`; migration; tests `tests/llm/`.

- [ ] Deps: `uv add anthropic httpx`.
- [ ] Config additions (config.py): `llm_model: str = "claude-opus-4-8"`, `llm_fast_model: str = "claude-haiku-4-5"`, `llm_max_retries: int = 3`, `llm_timeout_s: float = 120.0`, `job_cost_cap_usd: float = 1.50`, `anthropic_api_key: str = ""` (SDK also reads env ANTHROPIC_API_KEY; empty means SDK default resolution).
- [ ] `llm/models.py`: `LlmUsage` table — id, created_at, feature (str, e.g. "extract.normalize", "extract.tier2_judge", "extract.browser"), user_id (uuid nullable), job_id (uuid nullable), model, input_tokens, output_tokens, cost_usd Numeric(10,6). Migration + register in alembic env/conftest imports.
- [ ] `llm/pricing.py`: `PRICES_PER_MTOK = {"claude-opus-4-8": (5.0, 25.0), "claude-haiku-4-5": (1.0, 5.0)}`; `cost_usd(model, in_tok, out_tok)`; unknown model → conservative opus pricing + warning log.
- [ ] `llm/client.py` — the single seam (spec §3). Public surface (keep EXACTLY this; all extraction code depends on it):

```python
class UsageRecorder(Protocol):
    def record(self, *, feature: str, model: str, input_tokens: int, output_tokens: int, cost: float) -> None: ...

class CostCapExceeded(Exception): ...

class LLMClient:
    def __init__(self, *, recorder: UsageRecorder | None = None, cost_cap_usd: float | None = None) -> None: ...
    @property
    def spent_usd(self) -> float: ...
    def structured(self, *, feature: str, output_model: type[T], system: str | None = None,
                   content: str | list[dict[str, Any]], model: str | None = None,
                   max_tokens: int = 8000, fast: bool = False) -> T: ...
    def classify_bool(self, *, feature: str, question: str, content: str, max_chars: int = 30000) -> bool: ...
    def tool_loop(self, *, feature: str, system: str, tools: list[dict[str, Any]],
                  initial_content: list[dict[str, Any]],
                  execute: Callable[[str, dict[str, Any]], list[dict[str, Any]] | str],
                  max_iterations: int = 15, max_tokens: int = 8000) -> Message: ...

def image_block(data: bytes, media_type: str) -> dict[str, Any]: ...   # base64 content block helper
```

  - `structured` uses `self._client.messages.parse(model=..., max_tokens=..., system=..., messages=[{"role": "user", "content": content}], output_format=output_model)` and returns `.parsed_output`; on pydantic `ValidationError`/parse failure, ONE repair retry appending the error text; record usage after every call (including failures with usage available); raise `CostCapExceeded` when cumulative `spent_usd` would exceed the cap BEFORE issuing the next call.
  - `fast=True` (or classify_bool) routes to `settings.llm_fast_model`; classify_bool truncates content to max_chars and parses a one-field pydantic model `{answer: bool}`.
  - `tool_loop` is the manual loop from the SDK docs: while `stop_reason == "tool_use"`, append assistant content, run `execute(name, input)` per tool_use block, append tool_result blocks (`tool_use_id` matched, `is_error` on exception with message), re-call; stops at `end_turn` or max_iterations (raise `BudgetExceeded(last_response)` carrying the final Message); records usage each round.
  - anthropic client constructed with `max_retries=settings.llm_max_retries, timeout=settings.llm_timeout_s`. No temperature/top_p anywhere (rejected on opus 4.8). No `thinking` param for extraction calls.
- [ ] Tests (mock `anthropic.Anthropic` with a stub returning canned Message objects — only THIS test dir mocks the SDK): structured happy path + repair retry on invalid JSON; cost accumulation + CostCapExceeded; classify_bool truncation + routing to fast model; tool_loop executes tools, matches tool_use_id, surfaces is_error, stops on end_turn, raises after max_iterations; usage recorder called with correct token math. Opt-in live smoke `tests/llm/test_live_smoke.py` (skipif not RN_LIVE_LLM_TESTS): one tiny haiku classify_bool round-trip.
- [ ] import-linter: `recipe_normalizer.llm` is shared infra (like errors/db) — modules may import `llm.client`. Add contracts only if needed to keep existing ones green.
- [ ] Gates; commit `feat(llm): LLMClient seam with structured output, vision, tool loop, cost tracking`.

### Task 3: catalog — fuzzy + LLM-assisted matching (spec §6.3.3)

**Files:** Modify `catalog/service.py`; tests.

- [ ] `uv add rapidfuzz`. New `match_or_create(db, name: str, *, llm: LLMClient | None = None) -> CanonicalIngredient`:
  1. exact `match()` (existing) → hit.
  2. fuzzy: `rapidfuzz.process.extractOne(normalized, all alias/name strings, score_cutoff=90)` → link.
  3. if `llm` provided and fuzzy 70–90 band has candidates: ONE `structured(fast=True)` call ("which of these catalog ingredients, if any, is '<name>'? answer index or none") → link on confident answer.
  4. else `create_unreviewed`.
- [ ] TDD with seeded catalog: "all purpose flour" (missing hyphen) fuzzy-matches; "AP fluor" (typo) fuzzy-matches; "dragon fruit syrup" creates unreviewed; LLM band test with a stub LLMClient. `cookbook.service.create_recipe` switches from `match`→`create_unreviewed` to `match_or_create` (llm=None there — manual entry stays LLM-free; extraction passes a client).
- [ ] Gates; commit `feat(catalog): fuzzy + LLM-assisted ingredient matching`.

### Task 4: ingestion — models + migration

**Files:** Create `src/recipe_normalizer/ingestion/models.py` (+ empty service/router stubs), conftest/alembic imports, migration; tests.

- [ ] `Job`: id, user_id FK users CASCADE indexed; input_type native enum InputType(url|pdf|image|text); status native enum JobStatus(queued|running|needs_review|done|failed|not_a_recipe); payload JSONB (`{"url": ...}` or `{"file_ref": ..., "filename": ..., "media_type": ...}` or `{"text": ...}`); source_fingerprint String(128) nullable indexed; attempts int default 0; next_attempt_at DateTime(tz) nullable; locked_at DateTime(tz) nullable; locked_by String(100) nullable; error String(2000) nullable; reason String(1000) nullable (human-readable not_a_recipe/failure reason, spec §6.4/§8); artifacts JSONB default dict (`{"raw_text_ref": ..., "screenshot_refs": [...], "image_ref": ...}`); extraction_meta JSONB nullable (`{tier_used, confidence, model, extracted_at, actions_log}` — spec §5); produced_recipe_ids JSONB default list (uuid strings); cost_usd Numeric(10,6) default 0; TimestampMixin.
- [ ] TDD roundtrip test incl. JSONB fields + enum transitions; migration autogen + apply; partial index on (status, next_attempt_at) for queue polling.
- [ ] Gates; commit `feat(ingestion): job model + migration`.

### Task 5: extraction — plugin interface + normalize stage (the shared LLM pass)

**Files:** Create `src/recipe_normalizer/extraction/__init__.py`, `extraction/base.py`, `extraction/normalize.py`, `extraction/text_plugin.py`; tests `tests/extraction/`.

- [ ] `extraction/base.py`:

```python
@dataclass
class Acquired:
    text: str | None = None
    images: list[tuple[bytes, str]] = field(default_factory=list)  # (data, media_type)
    source_image: tuple[bytes, str] | None = None                  # hero image when the source provides one
    artifacts: dict[str, str] = field(default_factory=dict)        # filestore refs for retention (spec §6.4)
    meta: dict[str, Any] = field(default_factory=dict)             # tier_used, actions_log, ...

class Extractor(Protocol):
    input_type: ClassVar[InputType]
    def acquire(self, payload: dict[str, Any], *, llm: LLMClient, store: FileStore) -> Acquired: ...
```

- [ ] `extraction/normalize.py` — the §6.3 shared stage. Pydantic schema for the structured pass:

```python
class NormalizedLine(BaseModel):
    original_text: str
    name: str | None = None          # ingredient name for catalog matching
    quantity: float | None = None
    unit: str | None = None
    note: str | None = None
    is_optional: bool = False

class NormalizedRecipe(BaseModel):
    title: str
    description: str | None = None
    language: str = "en"             # BCP-47 of the ORIGINAL text
    servings_amount: float | None = None
    servings_unit_text: str | None = None
    prep_min: int | None = None
    cook_min: int | None = None
    total_min: int | None = None
    cuisines: list[str] = []
    dish_types: list[str] = []
    tags: list[str] = []
    groups: list[NormalizedGroup]    # ≥1 group ≥1 line (mirror RecipeIn constraints)
    steps: list[NormalizedStep] = []

class NormalizeResult(BaseModel):
    is_recipe: bool
    confidence: float                # 0..1
    reason: str | None = None        # human-readable when not a recipe / low confidence
    recipes: list[NormalizedRecipe] = []   # 1..N (multi-recipe inputs per spec §6.3)
```

`normalize(acquired: Acquired, *, llm: LLMClient) -> NormalizeResult` builds content blocks (text and/or image blocks via `image_block`), one `llm.structured(feature="extract.normalize", output_model=NormalizeResult, ...)` call with a carefully written system prompt: preserve original_text VERBATIM (incl. Hebrew), detect groups ("For the dough"), split steps verbatim, classify dish_types from the seeded vocab list (pass the 12 names in the prompt), extract servings, NEVER invent a recipe from a non-recipe (is_recipe=false + reason), multi-recipe sources → one entry per recipe.
- [ ] `extraction/text_plugin.py`: passthrough — `Acquired(text=payload["text"])`, stores raw text artifact via FileStore.
- [ ] `persist_drafts(db, *, user_id, job, result: NormalizeResult, llm) -> list[UUID]` (in normalize.py or a small `extraction/persist.py`): converts each NormalizedRecipe → `RecipeIn` (+ per-line name) → `cookbook.service.create_recipe(..., source/source_type/source_fingerprint from job, extraction_meta, image_ref from job.artifacts, is_verified stays False)`. NOTE: create_recipe currently lacks `extraction_meta`/`image_ref` params — extend cookbook.service signature minimally (keyword-only, default None) here. Fingerprint: only the FIRST draft of a job carries the fingerprint (partial unique index is per-recipe; N drafts from one source would collide) — pass fingerprint to draft 0 only.
- [ ] TDD throughout with a stub LLMClient returning canned NormalizeResult objects (the seam!): single recipe persists with catalog-matched lines + display strings; multi-recipe input → 2 drafts, fingerprint only on first; not-a-recipe propagates is_recipe/reason; Hebrew original_text preserved.
- [ ] Gates; commit `feat(extraction): plugin interface, shared normalize stage, draft persistence`.

### Task 6: ingestion — service + router (submit, dedupe, jobs API, review actions)

**Files:** Create `ingestion/service.py`, `ingestion/schemas.py`, `ingestion/router.py`; wire in main.py; tests.

- [ ] Fingerprints: `fingerprint_url(url)` (lowercase scheme/host, strip default port, drop utm_*/fbclid/gclid query params, strip fragment, sha256) and `fingerprint_bytes(data)` (sha256). Unit-test normalization cases.
- [ ] `submit_url/submit_file/submit_text(db, *, user_id, ...) -> Job`: compute fingerprint (text inputs: fingerprint of normalized text); dedupe per spec §6.4 — if a recipe with (owner, fingerprint) exists → `DuplicateRecipeError` (existing envelope w/ existing_id); if an ACTIVE job (queued/running/needs_review) with same (user, fingerprint) exists → ApiError 409 "already being processed" with job_id in extra; else create queued Job. Files saved to FileStore first (validate media types: pdf|png|jpeg|webp|gif, size cap 30 MB).
- [ ] Jobs API: `GET /api/jobs` (own, newest first, optional status filter), `GET /api/jobs/{id}` (incl. produced draft summaries via cookbook service), `POST /api/jobs/{id}/retry` (failed/not_a_recipe → queued, attempts reset; reuses retained artifacts per spec §6.4 — no re-scrape: if artifacts.raw_text_ref exists, worker skips acquire). Submit endpoints: `POST /api/ingest/url {url}`, `POST /api/ingest/file` (multipart), `POST /api/ingest/text {text}` → 202 JobOut.
- [ ] Review actions (spec §6.4 gate): `POST /api/jobs/{id}/recipes/{recipe_id}/accept` → cookbook service sets is_verified=True (add `verify_recipe(db, owner_id, recipe_id)`); `.../reject` → delete draft + remove from produced_recipe_ids; when no unresolved drafts remain → job status done. `POST /api/jobs/accept-high-confidence` → accepts every needs_review job whose extraction_meta.confidence ≥ 0.9 (spec: "approve all high-confidence"); returns count.
- [ ] Router tests via make_client (users+ingestion routers): submit text 202; duplicate url → 409 envelope with existing/job id; file type rejection 422; jobs list owner-scoped; accept/reject flow drives job to done; accept-all endpoint.
- [ ] Gates; commit `feat(ingestion): submit/dedupe/jobs API + review actions`.

### Task 7: worker — Postgres job queue + runner

**Files:** Create `src/recipe_normalizer/worker.py`, `ingestion/queue.py`; compose `worker` service; tests.

- [ ] `ingestion/queue.py`: `claim_next(db, worker_id) -> Job | None` —

```sql
SELECT * FROM jobs
WHERE status = 'queued' AND (next_attempt_at IS NULL OR next_attempt_at <= now())
ORDER BY created_at
FOR UPDATE SKIP LOCKED
LIMIT 1
```

(via SQLAlchemy `.with_for_update(skip_locked=True)`), set status=running/locked_at/locked_by, flush. `complete(db, job, result)`, `fail(db, job, error)` — attempts+1; if attempts < 3 → status queued + `next_attempt_at = now() + 2^attempts * 30s` (exponential backoff per spec §3); else failed with reason. `release_stale(db, older_than_minutes=10)` — running jobs with stale locked_at → queued (crash recovery).
- [ ] `worker.py`: `run_worker(poll_interval=2.0, once=False)` loop — own SessionLocal sessions per job (commit/rollback explicitly; the worker is NOT behind get_db); builds `LLMClient(recorder=DbUsageRecorder(db, user_id, job_id), cost_cap_usd=settings.job_cost_cap_usd)` per job; dispatches to the Extractor registry by input_type; acquire (skipped when retained raw text exists) → normalize → persist drafts → status needs_review + extraction_meta + produced ids; `is_recipe=False` → not_a_recipe + reason; `CostCapExceeded` → failed with "cost cap exceeded" reason; any exception → fail() with backoff. Job.cost_usd updated from llm.spent_usd. Graceful SIGTERM (finish current job). CLI: `python -m recipe_normalizer.worker` (+ `--once` for tests).
- [ ] Queue TDD against real Postgres: claim skips locked rows (two sessions), backoff schedule, stale release, full pipeline run with text plugin + stub LLM (inject a stub LLMClient factory — make the worker's client construction injectable) drives job queued→needs_review and drafts exist; not-a-recipe path; cost-cap path.
- [ ] compose: add `worker` service (same image as api, `command: uv run python -m recipe_normalizer.worker`, same env, depends_on postgres healthy). The spec's §3 service list (api/worker/web/postgres) is now complete.
- [ ] Gates; commit `feat(worker): postgres job queue + extraction worker process`.

### Task 8: URL extractor — tiers 1+2

**Files:** Create `extraction/url_plugin.py`, `extraction/jsonld.py`; fixtures `tests/extraction/fixtures/html/`; tests.

- [ ] `uv add trafilatura`. Fixtures: commit 4+ saved HTML files — (a) full schema.org/Recipe JSON-LD (realistic, e.g. structure of a serious-eats-style page), (b) microdata-only variant, (c) readable recipe WITHOUT structured data, (d) a news article (negative), (e) Hebrew recipe page with JSON-LD. NO network in tests: the plugin takes a `fetch: Callable[[str], FetchResult]` injectable (default httpx impl with browser UA, 15s timeout, redirects, content-type guard).
- [ ] `extraction/jsonld.py`: parse `<script type="application/ld+json">` blocks (json + @graph arrays + list-roots), find `@type` Recipe (case-insensitive, array types); map to `NormalizeResult` DIRECTLY (NO LLM — tier 1 is deterministic, spec §6.1): name→title, recipeIngredient[] → one group with original_text lines (name/quantity/unit left None — catalog matching still runs on... wait: lines need `name` for matching; tier-1 lines get name=None → unlinked).
  DESIGN DECISION (follow): tier 1 maps structure deterministically, then ONE cheap LLM pass (`fast=True`, feature "extract.tier1_enrich") takes ONLY the ingredient strings and returns per-line {name, quantity, unit, note} — structure from JSON-LD stays authoritative, the LLM only parses lines (bounded, cheap, still no full-page LLM read). recipeInstructions (HowToStep/sections) → steps verbatim; recipeYield → servings; prep/cook/totalTime ISO-8601 durations → minutes (deterministic parser, unit-tested); recipeCuisine/category → cuisines/dish_types; image (str|list|ImageObject) → first url into `Acquired.source_image` via fetch; `inLanguage` → language.
  Completeness check (spec: "present and complete"): title + ≥3 ingredient lines + ≥1 instruction step else fall through to tier 2.
- [ ] Tier 2 in `url_plugin.py`: `trafilatura.extract` (favor_recall, include tables) → `llm.classify_bool(feature="extract.tier2_judge", question="Does this text contain at least one complete cooking/drink recipe (ingredients AND instructions)?")` (haiku, spec §6.1) → yes: `Acquired(text=readable_text)` flows to the shared normalize; no: raise `TierFailed` → tier 3 (Task 9; until then job fails gracefully with reason "could not extract from page"). Also capture og:image for tier-2 pages.
  Raw artifact retention: store fetched HTML + readable text via FileStore in `artifacts`.
- [ ] Tests: fixture (a) → tier 1, NO normalize-LLM call (assert stub not called for full normalize; enrich call allowed), correct title/steps/servings/ISO durations/image captured, meta.tier_used==1; (b) microdata → falls to tier 2 IF jsonld absent (microdata parsing is OPTIONAL — implement only `itemtype*=schema.org/Recipe` quick extraction if cheap, else document fallthrough; decide in-code, test what you implement); (c) → tier 2 judged yes (stub classify true) → normalize stub called; (d) → tier 2 judged no → TierFailed; (e) Hebrew JSON-LD → tier 1, Hebrew preserved.
- [ ] Gates; commit `feat(extraction): URL tiers 1-2 (JSON-LD + readable-text)`.

### Task 9: URL extractor — tier 3 agentic browser

**Files:** Create `extraction/browser.py`; integrate into url_plugin; tests (mocked playwright + mocked LLM); compose/worker deps.

- [ ] `uv add playwright` + `playwright install chromium` (document in README + Dockerfile for worker: `RUN uv run playwright install --with-deps chromium`).
- [ ] `browser.py`: `browse_for_recipe(url, *, llm, store) -> Acquired` — Playwright sync chromium (headless, 1280×2000, realistic UA, `wait_until="domcontentloaded"`), then an `llm.tool_loop` (model default = opus, feature "extract.browser") with system prompt: "You are operating a browser to reach the full text of a recipe... dismiss cookie banners/popups/overlay ads, click 'jump to recipe', scroll, expand collapsed sections, then capture." Tools (execute() implements via playwright):
  - `screenshot()` → returns image block of current viewport (this is how the model SEES — also append an initial screenshot to initial_content)
  - `click(text_or_selector)` (text first via get_by_text, fallback css), `press_escape()`, `scroll(pixels)`, `wait(ms ≤ 5000)`
  - `capture_content()` → trafilatura on `page.content()`; if the result contains ≥ 3 lines that look like ingredients (cheap regex heuristic), tool returns "CAPTURED" and the loop's execute records the text + final screenshot and signals done (raise a private `_Captured` carrying it; catch in browse_for_recipe).
  Budgets per spec §6.1: max 15 tool-loop iterations (LLMClient max_iterations=15) AND 90s wall clock (check between iterations; enforce via time check in execute). On exhaustion/failure: save final screenshot to FileStore, raise `TierFailed(reason, screenshot_ref)` — job fails gracefully with the screenshot attached (spec: "the user sees why"). Actions log (every tool call) → meta.actions_log.
- [ ] url_plugin: tier 2 TierFailed → tier 3 (only when fetch succeeded but content insufficient; network-level failures fail directly). meta.tier_used=3.
- [ ] Tests: fake playwright Page object (no real browser) + scripted LLM stub that emits tool_use sequences: dismiss → scroll → capture happy path produces Acquired with text + actions_log; budget exhaustion path raises TierFailed with screenshot_ref; wall-clock timeout path. One opt-in REAL browser smoke (skipif no RN_LIVE_BROWSER_TESTS): load a local fixture HTML via file:// and capture.
- [ ] Gates; commit `feat(extraction): tier-3 agentic browser with action/time budgets`.

### Task 10: PDF + image extractors

**Files:** Create `extraction/pdf_plugin.py`, `extraction/image_plugin.py`; fixtures (a 1-2 page text-layer recipe PDF generated in-repo via a tiny script + a scanned-style image-only PDF + 1 recipe photo jpg + 1 handwritten-card style image); tests.

- [ ] `uv add pypdf pypdfium2`. PDF plugin (spec §6.2): extract text layer per page (pypdf); if total meaningful text < 200 chars OR garbled ratio high (non-printable/replacement-char heuristic) → render pages to PNG at 144 dpi via pypdfium2 (cap 10 pages, fail with reason beyond) → `Acquired(images=[...])` (vision path); else `Acquired(text=...)`. Multi-recipe PDFs are handled naturally by normalize's recipes[].
- [ ] Image plugin: payload file_ref → bytes from FileStore; downscale to ≤2000px long edge via pypdfium2? no — `uv add pillow`; re-encode to JPEG when >3MB; `Acquired(images=[(data, media_type)], source_image=same)` (the photo doubles as the recipe image for handwritten cards? NO — spec image_ref is the dish hero image; a handwritten card photo is NOT a dish photo. Leave source_image None for image inputs; the artifact ref is retained for the review screen).
- [ ] Tests: text-layer PDF → text path (no images); scanned PDF → images path with page cap; image input → single image block + artifact retained; oversized image re-encoded. Normalize stub asserts image blocks reach the LLM content.
- [ ] Gates; commit `feat(extraction): PDF (text layer + vision fallback) and image extractors`.

### Task 11: golden corpus regression suite

**Files:** `tests/extraction/test_golden.py`, `tests/extraction/golden/` (fixture inputs + expected JSON).

- [ ] Spec §9: committed fixtures with expected normalized JSON, regression-tested offline. Corpus (reuse Task 8/10 fixtures + add): JSON-LD page, readable-HTML page, Hebrew page, cocktail recipe text (Negroni in "parts"), multi-recipe text (2 recipes), not-a-recipe article, text-layer PDF. For LLM-dependent stages the "expected JSON" is the canned NormalizeResult the stub returns — the golden tests verify the DETERMINISTIC envelope end-to-end: tier selection, jsonld mapping (true expected JSON, no stub), duration parsing, fingerprinting, persistence (drafts with correct catalog links/display strings against seeded Postgres), artifacts retention, meta. Structure: one parametrized test walking `golden/cases/*/` dirs (`input.*`, `expected.json`, optional `llm_stub.json`).
- [ ] Gates; commit `test(extraction): golden corpus regression suite`.

### Task 12: web — Inbox (submit + job progress)

**REQUIRED: invoke frontend-design skill.** **Files:** `web/src/pages/InboxPage.tsx` + components (`SubmitPanel`, `JobList`, `JobRow`), enable the Inbox nav item, regenerate typed client (`make client` — commit openapi.json + schema.d.ts).

- [ ] Submit panel: URL input (paste-and-go), file drop zone (pdf/images, shows queued file), text paste area — three editorial "slots" matching the design system (hairline-framed, Fraunces labels, no dropzone-library look). Submits via typed client; 409 duplicate envelope → inline notice linking existing recipe/job.
- [ ] Job list: react-query with `refetchInterval: 2500` while any job is queued/running (spec §8 live progress; polling per plan note); rows show input summary (favicon/domain for urls, filename for files), status as small-caps editorial tags (QUEUED/RUNNING/NEEDS REVIEW/DONE/FAILED/NOT A RECIPE), tier/action info from extraction_meta when present, reason text for failed/not_a_recipe (+ final screenshot link when present), retry button (ghost) for failed, "Review" primary action for needs_review → /jobs/{id}/review (Task 13). "Approve all high-confidence" header action when ≥1 needs_review.
- [ ] Visual verification (mandatory, screenshots → /tmp/rn-screens/p2-t12-*) with realistic seeded jobs in every status (insert via API/DB directly).
- [ ] typecheck/build/lint green; commit `feat(web): inbox — submit panel + live job list`.

### Task 13: web — review screen

**REQUIRED: invoke frontend-design skill.** **Files:** `web/src/pages/ReviewPage.tsx` (+ components), route `/jobs/:id/review`.

- [ ] Spec §6.4: side-by-side — LEFT: source artifact (rendered raw text, page screenshot image via /api/files/, or original image; tabs when multiple artifacts); RIGHT: the extracted draft(s) in an EDITABLE form — reuse the Task 20 editor components (groups/lines/steps/vocab/servings) initialized from the draft recipe, PATCH /api/recipes/{id} on save. Multi-draft jobs: numbered draft tabs ("Recipe 1 of 3"). Confidence: extraction_meta.confidence shown as an editorial annotation; low-confidence (<0.7) gets a terracotta "review carefully" note (per-field confidence isn't modeled — document that the spec's per-field highlighting is approximated at recipe level in v1).
- [ ] Actions: Accept (→ accept endpoint → next unresolved draft or back to inbox), Reject (inline confirm), Save edits. Accepted drafts show in cookbook (is_verified=true).
- [ ] Visual verification with a real extracted job (run the worker against a fixture); screenshots p2-t13-*.
- [ ] Gates; commit `feat(web): extraction review screen`.

### Task 14: recipe images end-to-end + E2E + compose verification

**Files:** cookbook (image_ref already exists), web RecipeCard/DetailPage (image branches exist — verify with real refs), e2e spec, README updates.

- [ ] Wire `Acquired.source_image` → FileStore → recipe.image_ref in persist_drafts (done in Task 5 — VERIFY end-to-end now): worker run against the JSON-LD fixture (fetch stub serving the fixture + a tiny PNG) produces a draft whose card and detail page render the image from /api/files/.
- [ ] E2E (`e2e/tests/extraction.spec.ts`): serve fixture HTML via a tiny static server on a known port; submit URL through the Inbox UI → poll until NEEDS REVIEW → open review → edit title → Accept → cookbook shows recipe (with image) → detail shows dual quantities from extraction. Stub strategy: E2E runs the REAL worker with the REAL LLM? NO — gate: if RN_LIVE_LLM_TESTS, run fully live; otherwise start the worker with `RN_LLM_STUB=1` — add a tiny env-gated stub LLMClient factory in worker.py (returns canned NormalizeResult for the fixture; ONLY active with the env flag; documented as test-only). Tier-1 fixtures need no LLM except line-enrichment — the stub covers that.
- [ ] Compose: full `docker compose up --build` with the worker service; submit a text recipe through the real UI (live LLM if key present, else stub env) and walk it to the cookbook. Update README (worker, ANTHROPIC_API_KEY env, ingestion quickstart, cost caps).
- [ ] ALL gates incl. e2e; commit `feat: extraction E2E + compose with worker; docs`.

---

## Self-review notes

- Spec §6 coverage: tiers 1/2/3 (T8/T9), PDF/image (T10), text/manual (T5/Plan 1), normalize multi-recipe + guardrails (T5), not-a-recipe (T5/T7), review gate + approve-all (T6/T13), artifact retention + no-re-scrape retry (T6/T7/T8), dedupe at submission (T6), budgets + final screenshot (T9), cost caps + usage logging (T2/T7), fuzzy+LLM matching (T3), images (T8/T14), FileStore + JobQueue seams for SaaS (T1/T7), worker compose service (T7).
- Deliberate v1 simplifications to carry into review screens: recipe-level (not per-field) confidence; microdata optional; polling (not SSE).
- Type consistency: LLMClient surface in T2 is consumed verbatim by T3/T5/T8/T9; Acquired/Extractor in T5 consumed by T7-T10; NormalizeResult in T5 consumed by T7/T8/T11/T14 stubs.
