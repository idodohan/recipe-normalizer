"""Tests for the Extractor plugin interface and registry."""

from __future__ import annotations

from typing import Any, ClassVar

import pytest

import recipe_normalizer.extraction as extraction
from recipe_normalizer.extraction.base import EXTRACTORS, Acquired, TierFailed, register


def test_text_extractor_registered_on_package_import() -> None:
    """Importing the extraction package registers the text plugin."""
    from recipe_normalizer.extraction.text_plugin import TextExtractor

    assert "text" in extraction.EXTRACTORS
    assert isinstance(extraction.EXTRACTORS["text"], TextExtractor)
    # Package-level re-export and module-level dict are the same object.
    assert extraction.EXTRACTORS is EXTRACTORS


def test_register_returns_extractor_and_adds_to_registry() -> None:
    class DummyExtractor:
        input_type: ClassVar[str] = "dummy"

        def acquire(self, payload: dict[str, Any], *, llm: Any, store: Any) -> Acquired:
            return Acquired(text="dummy")

    dummy = DummyExtractor()
    try:
        returned = register(dummy)
        assert returned is dummy
        assert EXTRACTORS["dummy"] is dummy
    finally:
        EXTRACTORS.pop("dummy", None)


def test_register_duplicate_input_type_raises() -> None:
    """Accidental registry collisions must be loud — no silent override."""

    class FirstExtractor:
        input_type: ClassVar[str] = "collide"

        def acquire(self, payload: dict[str, Any], *, llm: Any, store: Any) -> Acquired:
            return Acquired()

    class SecondExtractor:
        input_type: ClassVar[str] = "collide"

        def acquire(self, payload: dict[str, Any], *, llm: Any, store: Any) -> Acquired:
            return Acquired()

    first = FirstExtractor()
    try:
        register(first)
        with pytest.raises(ValueError, match="collide"):
            register(SecondExtractor())
        # First registration untouched.
        assert EXTRACTORS["collide"] is first
    finally:
        EXTRACTORS.pop("collide", None)


def test_acquired_defaults_are_independent() -> None:
    a, b = Acquired(), Acquired()
    assert a.text is None
    assert a.images == []
    assert a.source_image is None
    assert a.artifacts == {}
    assert a.meta == {}
    # Mutable defaults must not be shared between instances.
    a.images.append((b"x", "image/png"))
    a.artifacts["k"] = "v"
    assert b.images == []
    assert b.artifacts == {}


def test_tier_failed_carries_reason_and_screenshot_ref() -> None:
    exc = TierFailed("budget exhausted", screenshot_ref="ab/cd.png")
    assert exc.reason == "budget exhausted"
    assert exc.screenshot_ref == "ab/cd.png"
    assert str(exc) == "budget exhausted"

    bare = TierFailed("no recipe found")
    assert bare.screenshot_ref is None
