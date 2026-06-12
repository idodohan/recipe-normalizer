/**
 * E2E happy path: register → entry → browse → scale → delete.
 *
 * All steps run in a single browser context / page (shared via beforeAll) so
 * the session cookie persists across tests. A unique email is generated per
 * run — no cleanup required; the dev compose postgres (localhost:5432, db rn)
 * is used directly, which is acceptable for v1 (state is isolated by user).
 */

import { test, expect, type Page } from "@playwright/test";

// One unique user per run.
// example.com is an RFC 2606 reserved domain that passes email validation.
const EMAIL = `e2e+${Date.now()}@example.com`;
const PASSWORD = "password123";
const DISPLAY_NAME = "E2E Cook";

test.describe.configure({ mode: "serial" });

// Shared page across all tests in the describe block.
let sharedPage: Page;

test.describe("happy path", () => {
  test.beforeAll(async ({ browser }) => {
    const context = await browser.newContext();
    sharedPage = await context.newPage();
  });

  test.afterAll(async () => {
    await sharedPage.context().close();
  });

  // -----------------------------------------------------------------------
  // 1. Register
  // -----------------------------------------------------------------------
  test("register → empty cookbook", async () => {
    const page = sharedPage;
    await page.goto("/register");
    await page.getByLabel("Display name").fill(DISPLAY_NAME);
    await page.getByLabel("Email").fill(EMAIL);
    await page.getByLabel("Password").fill(PASSWORD);
    await page.getByRole("button", { name: "Create account" }).click();

    // Should land on the cookbook page (/).
    await expect(page).toHaveURL("/");
    // Empty state title is visible.
    await expect(
      page.getByRole("heading", { name: "Your cookbook is empty" }),
    ).toBeVisible();
    // "Add a recipe" button is present.
    await expect(page.getByRole("button", { name: "Add a recipe" })).toBeVisible();
  });

  // -----------------------------------------------------------------------
  // 2. Add recipe
  // -----------------------------------------------------------------------
  test("add E2E Brownies", async () => {
    const page = sharedPage;
    await page.goto("/recipes/new");

    // -- Title (section 01) --
    await page.getByRole("textbox", { name: "Recipe title" }).fill("E2E Brownies");

    // -- Servings --
    await page.getByLabel("Serves").fill("8");
    // "As" label is in the servings row — scope to .editor__servings to avoid
    // ambiguity with "Ingredient line as written" in the ingredients section.
    await page.locator(".editor__servings").getByLabel("As").fill("servings");

    // -- Dish type (section 02) --
    // Type "dessert" into the Dish types combobox, press Enter to add it.
    await page.getByRole("combobox", { name: /dish types/i }).fill("dessert");
    await page.getByRole("combobox", { name: /dish types/i }).press("Enter");

    // -- Ingredients (section 03) --
    // Line 1: 2 cups all-purpose flour
    const line1 = page.locator(".line").nth(0);
    await line1.getByRole("textbox", { name: "Ingredient line as written" }).fill("2 cups all-purpose flour");
    await line1.getByRole("textbox", { name: "Ingredient name for matching" }).fill("all-purpose flour");
    await line1.getByRole("textbox", { name: "Quantity" }).fill("2");
    await line1.getByRole("textbox", { name: "Unit" }).fill("cup");

    // Line 2: salt to taste (no qty/unit) — press Enter from line 1 to create it.
    await line1.getByRole("textbox", { name: "Ingredient line as written" }).press("Enter");
    const line2 = page.locator(".line").nth(1);
    await line2.getByRole("textbox", { name: "Ingredient line as written" }).fill("salt to taste");
    // name stays empty, no qty or unit

    // Line 3: 1/2 tsp vanilla extract — press Enter from line 2 to create it.
    await line2.getByRole("textbox", { name: "Ingredient line as written" }).press("Enter");
    const line3 = page.locator(".line").nth(2);
    await line3.getByRole("textbox", { name: "Ingredient line as written" }).fill("1/2 tsp vanilla extract");
    await line3.getByRole("textbox", { name: "Ingredient name for matching" }).fill("vanilla extract");
    // Type "1/2" to exercise fraction parsing.
    await line3.getByRole("textbox", { name: "Quantity" }).fill("1/2");
    await line3.getByRole("textbox", { name: "Unit" }).fill("tsp");

    // -- Steps (section 04) --
    await page.getByLabel("Step 1").fill("Mix everything.");
    // Add step 2.
    await page.getByRole("button", { name: "+ Add step" }).click();
    await page.getByLabel("Step 2").fill("Bake at 175C for 25 minutes.");

    // -- Save --
    await page.getByRole("button", { name: "Save recipe" }).click();

    // Should navigate to the recipe detail page.
    await expect(page).toHaveURL(/\/recipes\//);
    await expect(page.getByRole("heading", { level: 1 })).toContainText("E2E Brownies");
  });

  // -----------------------------------------------------------------------
  // 3. Detail page assertions
  // -----------------------------------------------------------------------
  test("detail page: ingredient display strings", async () => {
    const page = sharedPage;
    // Already on the detail page from the previous test — just assert.
    await expect(page.getByRole("heading", { level: 1 })).toContainText("E2E Brownies");

    // Flour line: "2 cups all-purpose flour → ~240 g (approx.)"
    // The display string is split across spans; assert on the whole list item's text.
    const flourLine = page.locator(".ing-line").filter({ hasText: "all-purpose flour" });
    await expect(flourLine).toContainText("2 cups all-purpose flour");
    await expect(flourLine).toContainText("~240 g");
    await expect(flourLine).toContainText("(approx.)");

    // Salt line: no arrow, no metric conversion (passthrough / no qty).
    const saltLine = page.locator(".ing-line").filter({ hasText: "salt to taste" });
    await expect(saltLine).toContainText("salt to taste");
    // There should be NO "→" arrow on the salt line.
    await expect(saltLine).not.toContainText("→");

    // Vanilla line: 0.5 tsp = ~2.46 ml (exact volume, no approx.).
    const vanillaLine = page.locator(".ing-line").filter({ hasText: "vanilla extract" });
    await expect(vanillaLine).toContainText("2.46 ml");
    // Vanilla conversion is exact — it should NOT say "(approx.)"
    await expect(vanillaLine).not.toContainText("(approx.)");

    // Unreviewed badge is visible (recipe is not verified).
    await expect(page.locator(".rd__flag")).toContainText(/unreviewed/i);
  });

  // -----------------------------------------------------------------------
  // 4. Cookbook grid
  // -----------------------------------------------------------------------
  test("cookbook grid: card present with UNREVIEWED and dessert kicker", async () => {
    const page = sharedPage;
    await page.goto("/");
    const card = page.locator(".recipe-card").filter({ hasText: "E2E Brownies" });
    await expect(card).toBeVisible();
    // "dessert" kicker.
    await expect(card.locator(".recipe-card__types")).toContainText("dessert");
    // UNREVIEWED flag.
    await expect(card.locator(".recipe-card__flag")).toContainText(/unreviewed/i);
  });

  // -----------------------------------------------------------------------
  // 5. Scale ×2
  // -----------------------------------------------------------------------
  test("scale ×2: flour ~480 g, original text, salt doesn't-scale, disclaimer, ×1 reset", async () => {
    const page = sharedPage;
    // Navigate to the recipe detail page via the card link.
    await page.getByRole("link", { name: /E2E Brownies/i }).click();
    await expect(page.getByRole("heading", { level: 1 })).toContainText("E2E Brownies");

    // Click the ×2 preset.
    await page.getByRole("button", { name: "×2" }).click();

    // Scaled note.
    await expect(page.locator(".rd__scaled-note")).toContainText(
      "Scaled ×2 — showing recalculated quantities",
    );

    // Flour: scaled quantity ~480 g.
    const flourLine = page.locator(".ing-line").filter({ hasText: "all-purpose flour" });
    await expect(flourLine).toContainText("~480 g");
    // "from 2 cups all-purpose flour" sub-line.
    await expect(flourLine.locator(".ing-line__from")).toContainText(
      "from 2 cups all-purpose flour",
    );

    // Salt: passthrough → "doesn't scale" tag.
    // The CSS (.ing-line__pass) applies text-transform: uppercase, so we check
    // the DOM text content case-insensitively.
    const saltLine = page.locator(".ing-line").filter({ hasText: "salt to taste" });
    const passEl = saltLine.locator(".ing-line__pass");
    await expect(passEl).toBeVisible();
    const passText = await passEl.textContent();
    // The source uses a Unicode right single quotation mark (U+2019): "doesn’t scale".
    // CSS text-transform: uppercase renders it as "DOESN’T SCALE".
    // We normalise to lowercase and match with the Unicode apostrophe.
    expect(passText?.toLowerCase()).toContain("doesn’t scale");

    // Disclaimer in method section.
    await expect(page.locator(".rd__note")).toContainText(
      "Step text shows original quantities",
    );

    // ×1 reset → back to ~240 g, note and disclaimer gone.
    await page.getByRole("button", { name: "×1" }).click();
    await expect(flourLine).toContainText("~240 g");
    await expect(page.locator(".rd__scaled-note")).not.toBeVisible();
    await expect(page.locator(".rd__note")).not.toBeVisible();
  });

  // -----------------------------------------------------------------------
  // 6. Scale for 12 servings (base 8 → factor 1.5)
  // -----------------------------------------------------------------------
  test("scale for 12 servings: flour 3 cup original side, ~360 g", async () => {
    const page = sharedPage;
    // Already on the detail page.
    await expect(page.getByRole("heading", { level: 1 })).toContainText("E2E Brownies");

    // Use the "Target servings" input; blur triggers the scale.
    const servingsInput = page.getByLabel("Target servings");
    await servingsInput.fill("12");
    await servingsInput.press("Enter");

    // Scaled note shows factor 1.5.
    await expect(page.locator(".rd__scaled-note")).toContainText("1.5");

    // Flour: original side shows "3 cup", normalized shows "~360 g".
    const flourLine = page.locator(".ing-line").filter({ hasText: "all-purpose flour" });
    // The display string on the scaled ingredient starts with the new qty ("3 cup …").
    await expect(flourLine.locator(".ing-line__orig")).toContainText("3 cup");
    await expect(flourLine).toContainText("~360 g");
  });

  // -----------------------------------------------------------------------
  // 7. Delete
  // -----------------------------------------------------------------------
  test("delete: inline confirm → empty cookbook", async () => {
    const page = sharedPage;
    // Already on the detail page.
    await expect(page.getByRole("heading", { level: 1 })).toContainText("E2E Brownies");

    // Click Delete to enter confirm state.
    await page.getByRole("button", { name: "Delete" }).click();
    // Confirm dialog appears.
    await expect(page.getByText("Delete recipe?")).toBeVisible();
    // Click Yes.
    await page.getByRole("button", { name: "Yes" }).click();

    // Should redirect to cookbook, which should now be empty for this user.
    await expect(page).toHaveURL("/");
    await expect(
      page.getByRole("heading", { name: "Your cookbook is empty" }),
    ).toBeVisible();
  });

  // -----------------------------------------------------------------------
  // 8. Sign out
  // -----------------------------------------------------------------------
  test("sign out → /login", async () => {
    const page = sharedPage;
    await page.getByRole("button", { name: "Sign out" }).click();
    await expect(page).toHaveURL("/login");
  });
});
