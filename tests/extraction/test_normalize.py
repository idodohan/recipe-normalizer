"""Tests for the shared LLM normalize stage (spec §6.3)."""

from __future__ import annotations

import base64

from recipe_normalizer.extraction.base import Acquired
from recipe_normalizer.extraction.normalize import (
    NORMALIZE_SYSTEM,
    NormalizedGroup,
    NormalizedLine,
    NormalizedRecipe,
    NormalizedStep,
    NormalizeResult,
    normalize,
)
from tests.extraction.conftest import StubLLM


def _canned_result() -> NormalizeResult:
    return NormalizeResult(
        is_recipe=True,
        confidence=0.92,
        recipes=[
            NormalizedRecipe(
                title="Pancakes",
                groups=[
                    NormalizedGroup(
                        lines=[
                            NormalizedLine(
                                original_text="2 cups all-purpose flour",
                                name="all-purpose flour",
                                quantity=2.0,
                                unit="cup",
                            )
                        ]
                    )
                ],
                steps=[NormalizedStep(original_text="Mix everything.")],
            )
        ],
    )


def test_normalize_builds_text_and_image_blocks() -> None:
    canned = _canned_result()
    stub = StubLLM(result=canned)
    acquired = Acquired(text="Pancakes recipe ...", images=[(b"\x89PNGfake", "image/png")])

    out = normalize(acquired, llm=stub)  # type: ignore[arg-type]

    assert out is canned
    assert len(stub.calls) == 1
    call = stub.calls[0]
    assert call["feature"] == "extract.normalize"
    assert call["system"] is NORMALIZE_SYSTEM
    assert call["output_model"] is NormalizeResult
    assert call["max_tokens"] == 16000

    blocks = call["content"]
    assert blocks[0] == {"type": "text", "text": "Pancakes recipe ..."}
    image = blocks[1]
    assert image["type"] == "image"
    assert image["source"]["media_type"] == "image/png"
    assert base64.standard_b64decode(image["source"]["data"]) == b"\x89PNGfake"


def test_normalize_text_only_builds_single_text_block() -> None:
    stub = StubLLM(result=_canned_result())
    normalize(Acquired(text="just text"), llm=stub)  # type: ignore[arg-type]
    assert stub.calls[0]["content"] == [{"type": "text", "text": "just text"}]


def test_normalize_images_only_builds_image_blocks() -> None:
    stub = StubLLM(result=_canned_result())
    acquired = Acquired(images=[(b"a", "image/jpeg"), (b"b", "image/png")])
    normalize(acquired, llm=stub)  # type: ignore[arg-type]
    blocks = stub.calls[0]["content"]
    assert [b["type"] for b in blocks] == ["image", "image"]
    assert [b["source"]["media_type"] for b in blocks] == ["image/jpeg", "image/png"]


def test_normalize_empty_acquired_short_circuits_without_llm_call() -> None:
    stub = StubLLM()  # no canned result: any structured() call would fail the test
    out = normalize(Acquired(), llm=stub)  # type: ignore[arg-type]
    assert out.is_recipe is False
    assert out.reason == "no content acquired"
    assert out.recipes == []
    assert out.confidence == 0.0
    assert stub.calls == []


def test_normalize_whitespace_only_text_short_circuits() -> None:
    stub = StubLLM()
    out = normalize(Acquired(text="   \n"), llm=stub)  # type: ignore[arg-type]
    assert out.is_recipe is False
    assert stub.calls == []


def test_system_prompt_pins_dish_type_vocab_and_verbatim_rule() -> None:
    """The prompt IS the product's extraction quality — pin its key guarantees."""
    for dish_type in (
        "cocktail",
        "drink",
        "smoothie",
        "main",
        "dessert",
        "side",
        "breakfast",
        "soup",
        "salad",
        "bread",
        "sauce",
        "snack",
    ):
        assert dish_type in NORMALIZE_SYSTEM
    upper = NORMALIZE_SYSTEM.upper()
    assert "VERBATIM" in upper
    # Never fabricate a recipe from a non-recipe.
    assert "NEVER" in upper
    assert "is_recipe" in NORMALIZE_SYSTEM
    # English canonical name for matching; Hebrew called out for verbatim text.
    assert "English" in NORMALIZE_SYSTEM
    assert "Hebrew" in NORMALIZE_SYSTEM
