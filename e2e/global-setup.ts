import { spawnSync } from "child_process";
import path from "path";

/**
 * Global setup: seed the ingredient catalog before any tests run.
 * The seed script is idempotent, so re-runs are safe.
 */
async function globalSetup() {
  const root = path.resolve(__dirname, "..");
  console.log("[global-setup] Seeding catalog…");
  const result = spawnSync(
    "uv",
    ["run", "python", "-m", "recipe_normalizer.catalog.seed_loader"],
    { cwd: root, stdio: "inherit" },
  );
  if (result.status !== 0) {
    throw new Error(`Catalog seed failed with exit code ${result.status}`);
  }
  console.log("[global-setup] Catalog seeded.");
}

export default globalSetup;
