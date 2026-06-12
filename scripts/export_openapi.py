"""Export the OpenAPI schema to web/openapi.json.

Usage:
    uv run python scripts/export_openapi.py
"""

from __future__ import annotations

import json
import os
import sys

# Ensure src/ is on sys.path when run directly
_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from recipe_normalizer.main import create_app  # noqa: E402


def main() -> None:
    app = create_app()
    schema = app.openapi()

    web_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")
    os.makedirs(web_dir, exist_ok=True)

    output_path = os.path.join(web_dir, "openapi.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(schema, f, sort_keys=True, indent=2)
        f.write("\n")

    print(f"OpenAPI schema written to {output_path}")


if __name__ == "__main__":
    main()
