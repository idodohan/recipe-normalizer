.PHONY: client

## client: Regenerate the typed API client from the OpenAPI schema.
##         Run this whenever the backend API changes, then commit web/src/api/schema.d.ts.
##         See README (Task 24) for full workflow documentation.
client:
	uv run python scripts/export_openapi.py
	cd web && npm run generate:client
