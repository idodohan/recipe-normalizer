"""Tests for the text passthrough plugin."""

from __future__ import annotations

from pathlib import Path

from recipe_normalizer.extraction.text_plugin import TextExtractor
from recipe_normalizer.filestore import LocalFileStore
from tests.extraction.conftest import StubLLM


def test_acquire_saves_raw_artifact_and_returns_text(tmp_path: Path) -> None:
    store = LocalFileStore(tmp_path)
    llm = StubLLM()
    extractor = TextExtractor()

    acquired = extractor.acquire({"text": "2 cups flour\nMix well."}, llm=llm, store=store)  # type: ignore[arg-type]

    assert acquired.text == "2 cups flour\nMix well."
    ref = acquired.artifacts["raw_text_ref"]
    assert store.open(ref).decode("utf-8") == "2 cups flour\nMix well."
    # Passthrough plugin: no LLM involved in acquire.
    assert llm.calls == []


def test_acquire_round_trips_hebrew_text(tmp_path: Path) -> None:
    store = LocalFileStore(tmp_path)
    hebrew = "2 כוסות קמח\nלערבב היטב."

    acquired = TextExtractor().acquire({"text": hebrew}, llm=StubLLM(), store=store)  # type: ignore[arg-type]

    assert acquired.text == hebrew
    assert store.open(acquired.artifacts["raw_text_ref"]).decode("utf-8") == hebrew
