"""Sharing module — copy-on-share, public links, and shared cookbooks.

Owns all sharing-related state. Talks to `cookbook` only via
`cookbook.service` / `cookbook.schemas` (never `cookbook.models`); `cookbook`
must never import `sharing` (see .importlinter) — sharing depends on
cookbook, not the other way around.
"""
