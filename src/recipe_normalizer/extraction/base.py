"""Extractor plugin interface for the acquire phase (spec §6.1/§6.2).

Extraction is stateless: plugins receive plain payload dicts and the shared
seams (LLMClient, FileStore), and return an Acquired value. The worker owns
job bookkeeping — this package never imports ingestion.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar, Protocol

if TYPE_CHECKING:
    from recipe_normalizer.extraction.normalize import NormalizeResult
    from recipe_normalizer.filestore import FileStore
    from recipe_normalizer.llm.client import LLMClient

__all__ = ["EXTRACTORS", "Acquired", "Extractor", "TierFailed", "register"]


@dataclass
class Acquired:
    """Result of the acquire phase, ready for the shared normalize stage."""

    text: str | None = None
    # (data, media_type) pairs sent to vision in the normalize pass.
    images: list[tuple[bytes, str]] = field(default_factory=list)
    # Hero/dish image when the source provides one (becomes recipe.image_ref).
    source_image: tuple[bytes, str] | None = None
    # Filestore refs for raw retention (spec §6.4) — retry never re-scrapes.
    artifacts: dict[str, str] = field(default_factory=dict)
    # tier_used, actions_log, ... — lands in recipe.extraction_meta.
    meta: dict[str, Any] = field(default_factory=dict)
    # Deterministic tiers (URL tier 1 JSON-LD) prebuild the NormalizeResult
    # themselves; when set, the worker SKIPS the normalize() LLM pass entirely.
    prebuilt: NormalizeResult | None = None


class TierFailed(Exception):
    """An acquire tier failed; the next tier (if any) should run.

    Carries an optional filestore ref to the final screenshot so the user can
    see why the tier gave up (spec §6.1 budget exhaustion), plus any artifacts
    (screenshot, browser action log) retained for the review screen.
    """

    def __init__(
        self,
        reason: str,
        *,
        screenshot_ref: str | None = None,
        artifacts: dict[str, str] | None = None,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.screenshot_ref = screenshot_ref
        self.artifacts = artifacts


class Extractor(Protocol):
    """Per-source-type acquire plugin. New input types = new plugins."""

    # InputType VALUE string ("url" | "pdf" | "image" | "text") — kept as a plain
    # string so extraction never imports ingestion.
    input_type: ClassVar[str]

    def acquire(self, payload: dict[str, Any], *, llm: LLMClient, store: FileStore) -> Acquired:
        """Turn a job payload into Acquired content (raises TierFailed on failure)."""
        ...


# ---------------------------------------------------------------------------
# Registry — plugins self-register on import of the extraction package.
# ---------------------------------------------------------------------------

EXTRACTORS: dict[str, Extractor] = {}


def register[E: Extractor](extractor: E) -> E:
    """Register *extractor* under its input_type; returns it unchanged.

    Raises ValueError on a duplicate input_type — an accidental collision
    should be loud; there is no legitimate override case.
    """
    if extractor.input_type in EXTRACTORS:
        raise ValueError(f"extractor already registered for input_type {extractor.input_type!r}")
    EXTRACTORS[extractor.input_type] = extractor
    return extractor
