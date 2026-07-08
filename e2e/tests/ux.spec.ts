/**
 * E2E coverage for Phase 1 UX features: edit, favorite, kitchen notes, dark
 * mode, and the skip link.
 *
 * Follows the happy-path.spec.ts convention: a single shared page/context
 * (serial mode) with a unique per-run registered user — no cleanup needed.
 */

import { test, expect, type Page } from "@playwright/test";

// One unique user per run.
const EMAIL = `e2e-ux+${Date.now()}@example.com`;
const PASSWORD = "password123";
const DISPLAY_NAME = "E2E UX Cook";

test.describe.configure({ mode: "serial" });

// Shared page across all tests in the describe block.
let sharedPage: Page;

test.describe("phase 1 ux", () => {
  test.beforeAll(async ({ browser }) => {
    const context = await browser.newContext();
    sharedPage = await context.newPage();
  });

  test.afterAll(async () => {
    await sharedPage.context().close();
  });

  // -----------------------------------------------------------------------
  // 0. Register + create a recipe to exercise the rest of the suite against.
  // -----------------------------------------------------------------------
  test("register", async () => {
    const page = sharedPage;
    await page.goto("/register");
    await page.getByLabel("Display name").fill(DISPLAY_NAME);
    await page.getByLabel("Email").fill(EMAIL);
    await page.getByLabel("Password").fill(PASSWORD);
    await page.getByRole("button", { name: "Create account" }).click();
    await expect(page).toHaveURL("/");
  });

  test("add UX Test Recipe", async () => {
    const page = sharedPage;
    await page.goto("/recipes/new");

    await page.getByRole("textbox", { name: "Recipe title" }).fill("UX Test Recipe");

    const line1 = page.locator(".line").nth(0);
    await line1.getByRole("textbox", { name: "Ingredient line as written" }).fill("1 cup sugar");
    await line1.getByRole("textbox", { name: "Ingredient name for matching" }).fill("sugar");
    await line1.getByRole("textbox", { name: "Quantity" }).fill("1");
    await line1.getByRole("textbox", { name: "Unit" }).fill("cup");

    await page.getByLabel("Step 1").fill("Mix it.");

    await page.getByRole("button", { name: "Save recipe" }).click();

    await expect(page).toHaveURL(/\/recipes\//);
    await expect(page.getByRole("heading", { level: 1 })).toContainText("UX Test Recipe");
  });

  // -----------------------------------------------------------------------
  // 1. Edit flow: change title → Save → detail shows new title + toast.
  // -----------------------------------------------------------------------
  test("edit: change title → save → detail updated + toast", async () => {
    const page = sharedPage;

    await page.getByRole("button", { name: "Edit" }).click();
    await expect(page).toHaveURL(/\/edit$/);

    const titleField = page.getByRole("textbox", { name: "Recipe title" });
    await titleField.fill("UX Test Recipe (edited)");

    await page.getByRole("button", { name: "Save changes" }).click();

    // Back on the detail page with the new title.
    await expect(page).toHaveURL(/\/recipes\/[^/]+$/);
    await expect(page.getByRole("heading", { level: 1 })).toContainText(
      "UX Test Recipe (edited)",
    );

    // Toast announced via role="status".
    await expect(page.getByRole("status").getByText("Recipe updated")).toBeVisible();
  });

  // -----------------------------------------------------------------------
  // 2. Favorite: heart on detail → cookbook Favorites chip.
  // -----------------------------------------------------------------------
  test("favorite: heart toggles → shows under Favorites → still under All", async () => {
    const page = sharedPage;

    const heart = page.getByRole("button", { name: "Add to favorites" });
    await heart.click();
    await expect(page.getByRole("button", { name: "Remove from favorites" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );

    await page.goto("/");
    await page.getByRole("button", { name: "Favorites", exact: true }).click();
    await expect(
      page.locator(".recipe-card").filter({ hasText: "UX Test Recipe (edited)" }),
    ).toBeVisible();

    await page.getByRole("button", { name: "All", exact: true }).click();
    await expect(
      page.locator(".recipe-card").filter({ hasText: "UX Test Recipe (edited)" }),
    ).toBeVisible();
  });

  // -----------------------------------------------------------------------
  // 3. Kitchen notes: type → blur → reload → persisted.
  // -----------------------------------------------------------------------
  test("kitchen notes: persists across reload", async () => {
    const page = sharedPage;

    await page.getByRole("link", { name: /UX Test Recipe/i }).click();
    await expect(page.getByRole("heading", { level: 1 })).toContainText("UX Test Recipe");

    const notes = page.getByPlaceholder("Add a note — substitutions, tweaks, who loved it…");
    await notes.fill("Double the sugar next time.");
    await notes.blur();

    // Give the PATCH a moment to land before reloading.
    await expect(page.getByRole("heading", { level: 1 })).toContainText("UX Test Recipe");
    await page.waitForTimeout(500);

    await page.reload();
    await expect(
      page.getByPlaceholder("Add a note — substitutions, tweaks, who loved it…"),
    ).toHaveValue("Double the sugar next time.");
  });

  // -----------------------------------------------------------------------
  // 4. Dark mode: toggle → persists across reload → toggle back.
  // -----------------------------------------------------------------------
  test("dark mode: toggle → persists across reload → toggle back", async () => {
    const page = sharedPage;

    const toggle = page.getByRole("button", { name: "Switch to dark theme" });
    await toggle.click();
    await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");

    await page.reload();
    await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");

    await page.getByRole("button", { name: "Switch to light theme" }).click();
    await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  });

  // -----------------------------------------------------------------------
  // 5. Skip link: Tab on fresh load focuses it; Enter focuses #main.
  // -----------------------------------------------------------------------
  test("skip link: Tab focuses it, Enter jumps to #main", async () => {
    const page = sharedPage;
    await page.goto("/");
    // Wait for the authenticated shell to render (goto lands on a brief
    // "Signing you in…" loading state while the session check resolves) —
    // otherwise Tab lands before the skip link exists and focus goes nowhere.
    await expect(page.getByRole("navigation", { name: "Primary" })).toBeVisible();

    await page.keyboard.press("Tab");
    const skipLink = page.getByRole("link", { name: "Skip to content" });
    await expect(skipLink).toBeFocused();
    await expect(skipLink).toBeVisible();

    await page.keyboard.press("Enter");
    await expect(page.locator("#main")).toBeFocused();
  });
});
