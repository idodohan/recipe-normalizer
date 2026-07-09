"""AI module — per-recipe chat, cookbook Q&A, transformations, recommendations.

Owns all conversation/message state. Talks to `cookbook` only via
`cookbook.service` / `cookbook.schemas` (never `cookbook.models`), to
`sharing` only via `sharing.service` (never `sharing.models`), to `users`
only via `users.service` (never `users.models`), and to `llm` only via its
client/schema layer (never `llm.models`) — see .importlinter. Nothing else
in this codebase imports `ai` (it sits at the top of the dependency graph,
like `sharing`, but one layer further out).

This task (Phase 3 Task 1) only adds the storage layer: `Conversation` /
`Message` models plus owner-scoped CRUD. No LLM calls happen yet — those
land in later Phase 3 tasks (chat, cookbook Q&A, transformations).
"""
