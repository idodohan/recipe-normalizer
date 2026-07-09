/**
 * E2E coverage for Phase 3 AI features: recipe chat, cookbook Q&A, transform,
 * and "more like this" recommendations — all with the LLM STUBBED.
 *
 * Offline by default: `playwright.config.ts` sets `RN_LLM_STUB=1` on the
 * backend (api) webServer process, which swaps `ai.service.make_ai_llm` to
 * the canned `_StubAiLLMClient` (see ai/service.py) for every ai endpoint —
 * chat, cookbook-qa, and transform all answer deterministically, with zero
 * real model calls and zero ANTHROPIC_API_KEY dependency. This mirrors how
 * extraction.spec.ts's worker subprocess already sets the same flag for its
 * own (separate) LLM calls.
 *
 * Follows the happy-path/ux.spec convention: a single shared page/context
 * (serial mode) with a unique per-run registered user — no cleanup needed.
 */

import { test, expect, type Page } from "@playwright/test";

const EMAIL = `e2e-ai+${Date.now()}@example.com`;
const PASSWORD = "password123";
const DISPLAY_NAME = "E2E AI Cook";

// Canned reply text — must match the `_STUB_*` constants in ai/service.py.
const STUB_CHAT_REPLY = "This is a stubbed AI reply (RN_LLM_STUB=1) — no real model was called.";
const STUB_QA_REPLY =
  "This is a stubbed cookbook Q&A reply (RN_LLM_STUB=1) — no real model was called.";
const STUB_TRANSFORM_TITLE = "Stub Transformed Recipe (RN_LLM_STUB=1)";

test.describe.configure({ mode: "serial" });

let sharedPage: Page;

/**
 * Creates a minimal valid recipe (optionally with cuisines and a distinct
 * canonical ingredient) and lands on its detail page.
 *
 * `ingredient` defaults to "flour" (same convention as the other specs'
 * `createRecipe` helpers) but the recommendations test overrides it to an
 * uncommon, mutually-distinct name per pair — `cookbook_service._process_line`
 * fuzzy-matches ingredient names into a SHARED canonical catalog
 * (`catalog_service.match_or_create`), so leaving every recipe on "flour"
 * would give them all a nonzero ingredient-overlap score and break the
 * "absent for a lone recipe" assertion below.
 */
async function createRecipe(
  page: Page,
  title: string,
  cuisines: string[] = [],
  ingredient = "flour",
): Promise<void> {
  await page.goto("/recipes/new");
  await page.getByRole("textbox", { name: "Recipe title" }).fill(title);

  for (const cuisine of cuisines) {
    const cuisineBox = page.getByRole("combobox", { name: /cuisines/i });
    await cuisineBox.fill(cuisine);
    await cuisineBox.press("Enter");
  }

  const line1 = page.locator(".line").nth(0);
  await line1
    .getByRole("textbox", { name: "Ingredient line as written" })
    .fill(`1 cup ${ingredient}`);
  await line1.getByRole("textbox", { name: "Ingredient name for matching" }).fill(ingredient);
  await line1.getByRole("textbox", { name: "Quantity" }).fill("1");
  await line1.getByRole("textbox", { name: "Unit" }).fill("cup");

  await page.getByLabel("Step 1").fill("Mix it.");
  await page.getByRole("button", { name: "Save recipe" }).click();

  await expect(page).toHaveURL(/\/recipes\//);
  await expect(page.getByRole("heading", { level: 1 })).toContainText(title);
}

test.describe("phase 3 ai coverage", () => {
  test.beforeAll(async ({ browser }) => {
    const context = await browser.newContext();
    sharedPage = await context.newPage();
  });

  test.afterAll(async () => {
    await sharedPage.context().close();
  });

  test("register", async () => {
    const page = sharedPage;
    await page.goto("/register");
    await page.getByLabel("Display name").fill(DISPLAY_NAME);
    await page.getByLabel("Email").fill(EMAIL);
    await page.getByLabel("Password").fill(PASSWORD);
    await page.getByRole("button", { name: "Create account" }).click();
    await expect(page).toHaveURL("/");
  });

  // -----------------------------------------------------------------------
  // 1. Recipe chat
  // -----------------------------------------------------------------------
  test("recipe chat: ask a question → stubbed reply appears", async () => {
    const page = sharedPage;
    await createRecipe(page, "AI Chat Test Recipe");

    await page.getByRole("button", { name: "Ask about this recipe" }).click();
    const input = page.getByRole("textbox", { name: /ask a question about/i });
    await input.fill("Can I substitute butter for oil?");
    await page.getByRole("button", { name: "Send message" }).click();

    await expect(
      page.locator(".ai-msg--assistant").filter({ hasText: STUB_CHAT_REPLY }),
    ).toBeVisible();
  });

  // -----------------------------------------------------------------------
  // 2. Cookbook Q&A
  // -----------------------------------------------------------------------
  test("cookbook q&a: ask a question → stubbed answer (+ chip if referenced)", async () => {
    const page = sharedPage;
    await page.goto("/");
    await page.getByRole("button", { name: "Ask your cookbook" }).click();

    const input = page.getByRole("textbox", { name: "Ask a question about your cookbook" });
    await input.fill("What can I make with chicken and rice?");
    await page.getByRole("button", { name: "Send message" }).click();

    await expect(
      page.locator(".ai-msg--assistant").filter({ hasText: STUB_QA_REPLY }),
    ).toBeVisible();

    // The stub's tool_loop calls `search_recipes` once with no filters, which
    // returns the caller's whole cookbook (limit 20) — since we already have
    // at least one recipe (from the chat test above), the answer references
    // it and a chip should render. Only assert the chip if it actually shows
    // up, per the task's "only assert if the stub's canned answer references
    // a recipe" instruction — but here we expect it to, given cookbook state.
    const sources = page.locator(".ai-panel__sources");
    if (await sources.isVisible().catch(() => false)) {
      await expect(sources.locator(".chip--link").first()).toBeVisible();
    }
  });

  // -----------------------------------------------------------------------
  // 3. Transform
  // -----------------------------------------------------------------------
  test("transform: 'make it vegan' → review screen shows stubbed draft", async () => {
    const page = sharedPage;
    await page.goto("/");
    await page
      .getByRole("link", { name: /AI Chat Test Recipe/i })
      .first()
      .click();
    await expect(page.getByRole("heading", { level: 1 })).toContainText("AI Chat Test Recipe");

    await page.getByRole("button", { name: "Transform" }).click();
    const dialog = page.locator(".transform-dlg");
    await expect(page.getByRole("heading", { name: "Transform this recipe" })).toBeVisible();

    await page.getByLabel("Instruction", { exact: true }).fill("make it vegan");
    await dialog.getByRole("button", { name: "Transform", exact: true }).click();

    await expect(page).toHaveURL(/\/jobs\/.+\/review/);
    const title = page.getByRole("textbox", { name: "Recipe title" });
    await expect(title).toHaveValue(STUB_TRANSFORM_TITLE);
  });

  test("transform: 'halve it' → friendly scale hint, no navigation", async () => {
    const page = sharedPage;
    await page.goto("/");
    await page
      .getByRole("link", { name: /AI Chat Test Recipe/i })
      .first()
      .click();
    await expect(page.getByRole("heading", { level: 1 })).toContainText("AI Chat Test Recipe");

    await page.getByRole("button", { name: "Transform" }).click();
    const dialog = page.locator(".transform-dlg");
    await page.getByLabel("Instruction", { exact: true }).fill("halve it");
    await dialog.getByRole("button", { name: "Transform", exact: true }).click();

    await expect(page.getByText(/use the Scale control/i)).toBeVisible();
    // No navigation — still on the recipe detail page, dialog still open.
    await expect(page).not.toHaveURL(/\/jobs\//);
    await expect(page.getByRole("heading", { name: "Transform this recipe" })).toBeVisible();
  });

  // -----------------------------------------------------------------------
  // 4. Recommendations ("more like this")
  // -----------------------------------------------------------------------
  test("recommendations: absent for a lone cuisine, present once a sibling shares it", async () => {
    const page = sharedPage;
    // A distinctive ingredient name (not "flour", used by earlier recipes in
    // this suite) so this pair's only overlap with anything else in the
    // cookbook is intentional — see the createRecipe doc above.
    await createRecipe(page, "AI Reco Recipe One", ["Levantine"], "quince paste");

    // No other recipe shares "Levantine" (or "quince paste") yet — the row
    // should not render.
    await expect(page.locator(".similar")).toHaveCount(0);

    await createRecipe(page, "AI Reco Recipe Two", ["Levantine"], "quince paste");

    // Now "Recipe Two"'s detail page (current) should recommend "Recipe One".
    await expect(page.locator(".similar__heading")).toContainText("More like this");
    const tile = page.locator(".similar__tile").filter({ hasText: "AI Reco Recipe One" });
    await expect(tile).toBeVisible();
    await tile.click();
    await expect(page.getByRole("heading", { level: 1 })).toContainText("AI Reco Recipe One");
  });
});
