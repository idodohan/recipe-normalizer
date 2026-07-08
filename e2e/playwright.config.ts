import { defineConfig, devices } from "@playwright/test";
import path from "path";

/**
 * E2E tests run against the real dev compose postgres (localhost:5432, db rn).
 * A fresh random user is created per run so test state is isolated without
 * needing a dedicated test DB — acceptable for v1.
 *
 * The webServer entries boot the backend and frontend so that a bare
 * `npx playwright test` is self-contained (both are reused when already
 * running, e.g. during development).
 */
export default defineConfig({
  testDir: "./tests",
  /* Run tests serially within the file to match the describe({serial}) blocks */
  fullyParallel: false,
  /* Generous timeout for slow network/boot on CI */
  timeout: 60_000,
  expect: { timeout: 15_000 },
  retries: 0,
  reporter: [["list"], ["html", { open: "never" }]],

  use: {
    baseURL: "http://localhost:5173",
    /* Capture traces on failure for debugging */
    trace: "on-first-retry",
  },

  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],

  /* Seed the catalog once before any tests run */
  globalSetup: "./global-setup.ts",

  webServer: [
    {
      /* Backend — must be up before frontend (proxy target) */
      command: "uv run uvicorn recipe_normalizer.main:app --port 8000",
      cwd: path.resolve(__dirname, ".."),
      url: "http://localhost:8000/api/health",
      reuseExistingServer: true,
      timeout: 30_000,
      /* Allow the extraction E2E to submit the local fixture server (port
       * 8099) without tripping the SSRF guard — see netguard.py. */
      env: { ...process.env, RN_NETGUARD_ALLOW_HOSTS: "localhost" },
    },
    {
      /* Frontend dev server — proxies /api to localhost:8000 */
      command: "npm run dev",
      cwd: path.resolve(__dirname, "../web"),
      url: "http://localhost:5173",
      reuseExistingServer: true,
      timeout: 30_000,
    },
    {
      /* Static fixture server for the extraction E2E (recipe page + hero image) */
      command: "node fixture-server.mjs",
      cwd: __dirname,
      url: "http://localhost:8099/recipe.html",
      reuseExistingServer: true,
      timeout: 15_000,
    },
  ],
});
