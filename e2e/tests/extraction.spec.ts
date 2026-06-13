/**
 * E2E extraction flow: submit a URL in the Inbox → worker extracts (tier-1
 * JSON-LD) → review → edit title → Accept → recipe lands in the cookbook with
 * its hero image, and the detail page shows the extracted ingredients.
 *
 * Offline by default: the fixture-server (port 8099) serves the recipe page and
 * hero image, and the worker runs with RN_LLM_STUB=1 (the only LLM call on the
 * tier-1 path is line enrichment, which the stub answers). Set
 * RN_LIVE_LLM_TESTS=1 to run the worker against the real LLM instead.
 */

import { test, expect, type Page } from "@playwright/test";
import { spawnSync } from "child_process";
import path from "path";

const EMAIL = `e2e-extract+${Date.now()}@example.com`;
const PASSWORD = "password123";
const FIXTURE_URL = "http://localhost:8099/recipe.html";
const REPO_ROOT = path.resolve(__dirname, "..", "..");
const LIVE = process.env.RN_LIVE_LLM_TESTS === "1";

/** Run the worker once; drains a single queued job. */
function runWorkerOnce(): void {
  const result = spawnSync("uv", ["run", "python", "-m", "recipe_normalizer.worker", "--once"], {
    cwd: REPO_ROOT,
    env: { ...process.env, ...(LIVE ? {} : { RN_LLM_STUB: "1" }) },
    stdio: "inherit",
    timeout: 120_000,
  });
  if (result.status !== 0) {
    throw new Error(`worker --once exited ${result.status}`);
  }
}

test.describe.configure({ mode: "serial" });

let sharedPage: Page;

test.describe("extraction flow", () => {
  test.beforeAll(async ({ browser }) => {
    sharedPage = await (await browser.newContext()).newPage();
  });
  test.afterAll(async () => {
    await sharedPage.context().close();
  });

  test("register", async () => {
    const page = sharedPage;
    await page.goto("/register");
    await page.getByLabel("Display name").fill("Extract Cook");
    await page.getByLabel("Email").fill(EMAIL);
    await page.getByLabel("Password").fill(PASSWORD);
    await page.getByRole("button", { name: "Create account" }).click();
    await expect(page).toHaveURL("/");
  });

  test("submit a URL in the inbox", async () => {
    const page = sharedPage;
    await page.goto("/inbox");
    await page.locator(".submit-slot__input").fill(FIXTURE_URL);
    await page.getByRole("button", { name: "Fetch" }).click();
    // The job row appears (queued/running) for our fixture domain.
    await expect(page.locator(".job-row").filter({ hasText: "localhost" })).toBeVisible();
  });

  test("worker extracts → needs review", async () => {
    const page = sharedPage;
    // Drain the queue until our job reaches NEEDS REVIEW (worker is out-of-band).
    let reviewed = false;
    for (let attempt = 0; attempt < 6 && !reviewed; attempt++) {
      runWorkerOnce();
      await page.reload();
      await page.waitForLoadState("networkidle"); // let the jobs query resolve
      reviewed = await page
        .getByRole("link", { name: "Review" })
        .first()
        .isVisible()
        .catch(() => false);
    }
    expect(reviewed).toBe(true);
  });

  test("review → edit title → accept", async () => {
    const page = sharedPage;
    await page.getByRole("link", { name: "Review" }).first().click();
    await expect(page).toHaveURL(/\/jobs\/.+\/review/);

    // The extracted title is editable; the JSON-LD name is preserved verbatim.
    const title = page.getByRole("textbox", { name: "Recipe title" });
    await expect(title).toHaveValue("Skillet Cornbread");
    await title.fill("Skillet Cornbread (reviewed)");

    // The source panel shows the raw page (tier-1 retains raw_html_ref → text tab).
    await expect(page.locator(".source-panel")).toBeVisible();

    await page.getByRole("button", { name: "Accept recipe" }).click();
    // No drafts left → back to the inbox.
    await expect(page).toHaveURL("/inbox");
  });

  test("cookbook shows the accepted recipe with its hero image", async () => {
    const page = sharedPage;
    await page.goto("/");
    const card = page.locator(".recipe-card").filter({ hasText: "Skillet Cornbread (reviewed)" });
    await expect(card).toBeVisible();
    // The hero image fetched during extraction renders from /api/files.
    const img = card.locator("img");
    await expect(img).toBeVisible();
    await expect(img).toHaveAttribute("src", /\/api\/files\//);
  });

  test("detail page shows the extracted ingredients", async () => {
    const page = sharedPage;
    await page.getByRole("link", { name: /Skillet Cornbread \(reviewed\)/ }).click();
    await expect(page.getByRole("heading", { level: 1 })).toContainText(
      "Skillet Cornbread (reviewed)",
    );
    // Verbatim ingredient lines from the JSON-LD survive extraction + accept.
    await expect(
      page.locator(".ing-line").filter({ hasText: "1 cup all-purpose flour" }),
    ).toBeVisible();
    await expect(
      page.locator(".ing-line").filter({ hasText: "1 cup cornmeal" }),
    ).toBeVisible();
  });
});
