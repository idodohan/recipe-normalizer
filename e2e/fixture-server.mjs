// Tiny static server for E2E extraction fixtures (recipe HTML + hero image).
// The worker fetches these over HTTP exactly as it would a real recipe page,
// so the extraction E2E stays fully offline. Port 8099, files from ./fixtures.
import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { extname, join, normalize } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = join(fileURLToPath(new URL(".", import.meta.url)), "fixtures");
const TYPES = {
  ".html": "text/html; charset=utf-8",
  ".png": "image/png",
  ".jpg": "image/jpeg",
};

const server = createServer(async (req, res) => {
  try {
    const rel = normalize(decodeURIComponent((req.url ?? "/").split("?")[0])).replace(/^(\.\.[/\\])+/, "");
    const path = join(ROOT, rel === "/" ? "recipe.html" : rel);
    const body = await readFile(path);
    res.writeHead(200, { "content-type": TYPES[extname(path)] ?? "application/octet-stream" });
    res.end(body);
  } catch {
    res.writeHead(404, { "content-type": "text/plain" });
    res.end("not found");
  }
});

server.listen(8099, () => console.log("[fixture-server] http://localhost:8099"));
