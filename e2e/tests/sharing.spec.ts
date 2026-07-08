/**
 * E2E coverage for Phase 2 sharing/search/collections: cookbook search,
 * collections, copy-share between two users, public links, shared
 * cookbooks, and an authz negative check (owner-only Share button).
 *
 * Follows the happy-path/ux.spec convention: unique per-run registered
 * users, serial execution, no cleanup needed. Two users are needed here, so
 * this suite uses two browser contexts (one per user) plus a third,
 * logged-out context for the public-link check — see ux.spec's single
 * shared-page pattern for the single-user precedent this generalizes.
 */

import { test, expect, type Page } from "@playwright/test";

const RUN = Date.now();
const EMAIL_A = `e2e-share-a+${RUN}@example.com`;
const EMAIL_B = `e2e-share-b+${RUN}@example.com`;
const PASSWORD = "password123";
const NAME_A = "Sharer Ann";
const NAME_B = "Sharer Bob";

test.describe.configure({ mode: "serial" });

let pageA: Page;
let pageB: Page;

// Recipe ids captured as they're created, for direct navigation later.
let recipeIdA1: string; // "Zesty Lemon Curd" — search/collection/shared-cookbook/authz
let cookbookUrl: string; // shared cookbook detail page URL

/** Registers a fresh user on `page` and lands on the (empty) cookbook. */
async function register(page: Page, name: string, email: string) {
  await page.goto("/register");
  await page.getByLabel("Display name").fill(name);
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: "Create account" }).click();
  await expect(page).toHaveURL("/");
}

/** Creates a minimal valid recipe and returns its id. */
async function createRecipe(page: Page, title: string): Promise<string> {
  await page.goto("/recipes/new");
  await page.getByRole("textbox", { name: "Recipe title" }).fill(title);

  const line1 = page.locator(".line").nth(0);
  await line1.getByRole("textbox", { name: "Ingredient line as written" }).fill("1 cup flour");
  await line1.getByRole("textbox", { name: "Ingredient name for matching" }).fill("flour");
  await line1.getByRole("textbox", { name: "Quantity" }).fill("1");
  await line1.getByRole("textbox", { name: "Unit" }).fill("cup");

  await page.getByLabel("Step 1").fill("Mix it.");
  await page.getByRole("button", { name: "Save recipe" }).click();

  await expect(page).toHaveURL(/\/recipes\/[0-9a-f-]+$/);
  await expect(page.getByRole("heading", { level: 1 })).toContainText(title);
  return page.url().split("/recipes/")[1]!;
}

test.describe("phase 2 sharing", () => {
  test.beforeAll(async ({ browser }) => {
    pageA = await (await browser.newContext()).newPage();
    pageB = await (await browser.newContext()).newPage();
  });

  test.afterAll(async () => {
    await pageA.context().close();
    await pageB.context().close();
  });

  // -----------------------------------------------------------------------
  // 0. Register both users up front (B must exist before A shares to them).
  // -----------------------------------------------------------------------
  test("register user A and user B", async () => {
    await register(pageA, NAME_A, EMAIL_A);
    await register(pageB, NAME_B, EMAIL_B);
  });

  // -----------------------------------------------------------------------
  // 1. Search: two recipes, distinct titles, fragment narrows to one.
  // -----------------------------------------------------------------------
  test("search: fragment of one title shows only that recipe", async () => {
    recipeIdA1 = await createRecipe(pageA, "Zesty Lemon Curd");
    await createRecipe(pageA, "Maple Walnut Muffins");

    await pageA.goto("/");
    await pageA.getByRole("searchbox", { name: "Search recipes" }).fill("Zesty");

    await expect(
      pageA.locator(".recipe-card").filter({ hasText: "Zesty Lemon Curd" }),
    ).toBeVisible();
    await expect(
      pageA.locator(".recipe-card").filter({ hasText: "Maple Walnut Muffins" }),
    ).not.toBeVisible();

    // Clear search for the following tests.
    await pageA.getByRole("searchbox", { name: "Search recipes" }).fill("");
  });

  // -----------------------------------------------------------------------
  // 2. Collections: create via detail-page CollectionsControl, assign,
  //    filter the cookbook by it, then switch back to "Any collection".
  // -----------------------------------------------------------------------
  test("collections: create + assign on detail page, filter cookbook by it", async () => {
    await pageA.goto(`/recipes/${recipeIdA1}`);
    await expect(pageA.getByRole("heading", { level: 1 })).toContainText("Zesty Lemon Curd");

    await pageA.getByRole("button", { name: /^Collections/ }).click();
    const panel = pageA.getByRole("group", { name: "Manage this recipe's collections" });
    await panel.getByLabel("New collection").fill("Brunch Club");
    await panel.getByRole("button", { name: "Add" }).click();

    // Creating auto-assigns the collection to this recipe.
    await expect(pageA.getByRole("button", { name: /^Collections \(1\)/ })).toBeVisible();

    await pageA.goto("/");
    const collectionSelect = pageA.getByRole("combobox", { name: "Collection" });
    const optionValue = await collectionSelect
      .locator("option", { hasText: "Brunch Club" })
      .getAttribute("value");
    await collectionSelect.selectOption(optionValue!);

    await expect(
      pageA.locator(".recipe-card").filter({ hasText: "Zesty Lemon Curd" }),
    ).toBeVisible();
    await expect(
      pageA.locator(".recipe-card").filter({ hasText: "Maple Walnut Muffins" }),
    ).not.toBeVisible();

    // Switch back to "Any collection" — both recipes reappear.
    await collectionSelect.selectOption("");
    await expect(
      pageA.locator(".recipe-card").filter({ hasText: "Maple Walnut Muffins" }),
    ).toBeVisible();
  });

  // -----------------------------------------------------------------------
  // 3. Copy-share: A shares a copy by email to B; B sees it with provenance.
  // -----------------------------------------------------------------------
  test("copy-share: A sends a copy to B by email; B sees it with provenance", async () => {
    await createRecipe(pageA, "Copy Share Casserole");

    await pageA.getByRole("button", { name: "Share" }).click();
    await pageA.getByLabel("Recipient email").fill(EMAIL_B);
    await pageA.getByRole("button", { name: "Send" }).click();

    await expect(
      pageA.getByRole("status").getByText(`Sent to ${EMAIL_B}`),
    ).toBeVisible();

    await pageB.goto("/");
    // .recipe-card is itself the <Link> (see RecipeCard.tsx), not a wrapper
    // around one — click the card directly rather than a nested role query.
    const card = pageB.locator(".recipe-card").filter({ hasText: "Copy Share Casserole" });
    await expect(card).toBeVisible();
    await card.click();

    await expect(pageB.getByRole("heading", { level: 1 })).toContainText("Copy Share Casserole");
    await expect(pageB.locator(".rd__provenance")).toContainText(`Shared by ${EMAIL_A}`);
  });

  // -----------------------------------------------------------------------
  // 4. Public link: create → renders logged-out → revoke → unavailable.
  // -----------------------------------------------------------------------
  test("public link: renders logged out, revoke makes it unavailable", async () => {
    await createRecipe(pageA, "Public Link Tart");

    await pageA.getByRole("button", { name: "Share" }).click();
    await pageA.getByRole("button", { name: "Create link" }).click();

    const linkInput = pageA.getByLabel("Public link");
    await expect(linkInput).toBeVisible();
    const url = await linkInput.inputValue();
    expect(url).toContain("/p/");

    const publicContext = await pageA.context().browser()!.newContext();
    const publicPage = await publicContext.newPage();
    await publicPage.goto(url);
    await expect(publicPage.getByRole("heading", { level: 1 })).toContainText("Public Link Tart");

    // Revoke as A.
    await pageA.getByRole("button", { name: "Revoke" }).click();
    await pageA.getByRole("button", { name: "Yes" }).click();
    await expect(pageA.getByRole("status").getByText("Link revoked")).toBeVisible();

    await publicPage.reload();
    await expect(
      publicPage.getByRole("heading", { name: "This link is no longer available" }),
    ).toBeVisible();

    await publicContext.close();
  });

  // -----------------------------------------------------------------------
  // 5. Shared cookbook: A creates + invites B; B sees it, adds own recipe;
  //    A sees B's recipe too.
  // -----------------------------------------------------------------------
  test("shared cookbook: create, invite, both members' recipes visible", async () => {
    await pageA.goto("/shares");
    await pageA.getByLabel("New shared cookbook").fill("Family Feast");
    await pageA.getByRole("button", { name: "Create" }).click();

    await expect(pageA.getByRole("heading", { level: 1 })).toContainText("Family Feast");
    cookbookUrl = pageA.url();

    await pageA.getByLabel("Invite by email").fill(EMAIL_B);
    await pageA.getByRole("button", { name: "Invite" }).click();
    await expect(pageA.getByLabel("Invite by email")).toHaveValue("");

    await pageA
      .getByLabel("Add one of your recipes")
      .selectOption({ label: "Zesty Lemon Curd" });
    await pageA.getByRole("button", { name: "Add" }).click();
    await expect(pageA.getByRole("link", { name: /Zesty Lemon Curd/ })).toBeVisible();

    // B sees the invitation on /shares, opens it, sees A's recipe.
    await pageB.goto("/shares");
    await pageB.getByRole("link", { name: /Family Feast/ }).click();
    await expect(pageB.getByRole("heading", { level: 1 })).toContainText("Family Feast");
    await expect(pageB.getByRole("link", { name: /Zesty Lemon Curd/ })).toBeVisible();

    // B adds one of their own recipes.
    await createRecipe(pageB, "Berry Blondies");
    await pageB.goto(cookbookUrl);
    await pageB
      .getByLabel("Add one of your recipes")
      .selectOption({ label: "Berry Blondies" });
    await pageB.getByRole("button", { name: "Add" }).click();
    await expect(pageB.getByRole("link", { name: /Berry Blondies/ })).toBeVisible();

    // A reloads and sees B's recipe too, plus both members listed.
    await pageA.reload();
    await expect(pageA.getByRole("link", { name: /Berry Blondies/ })).toBeVisible();
    await expect(pageA.locator(".shc__member-list")).toContainText(NAME_A);
    await expect(pageA.locator(".shc__member-list")).toContainText(NAME_B);
  });

  // -----------------------------------------------------------------------
  // 6. Authz negative: B (non-owner member) opens A's shared recipe — the
  //    owner-only Share button must not be present.
  // -----------------------------------------------------------------------
  test("authz: non-owner shared-cookbook member has no Share button", async () => {
    await pageB.goto(`/recipes/${recipeIdA1}`);
    await expect(pageB.getByRole("heading", { level: 1 })).toContainText("Zesty Lemon Curd");
    await expect(pageB.getByRole("button", { name: "Share" })).toHaveCount(0);
  });
});
