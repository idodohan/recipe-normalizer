"""Tests for the URL extractor plugin: tier 1 (JSON-LD) + tier 2 (readable text).

NO network: fetch / fetch_bytes are injected fakes returning committed fixture
HTML; the LLM seam is stubbed (tier1_enrich + tier2_judge only — the full
extract.normalize pass must NEVER run inside the plugin).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from recipe_normalizer.extraction import EXTRACTORS
from recipe_normalizer.extraction.base import TierFailed
from recipe_normalizer.extraction.url_plugin import (
    EnrichedLine,
    EnrichedLines,
    FetchResult,
    UrlExtractor,
    default_fetch,
)
from recipe_normalizer.filestore import LocalFileStore
from recipe_normalizer.llm.client import LLMError

FIXTURES = Path(__file__).parent / "fixtures" / "html"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _fetch_for(html: str, *, content_type: str = "text/html; charset=utf-8") -> Any:
    def fetch(url: str) -> FetchResult:
        return FetchResult(url=url, status=200, html=html, content_type=content_type)

    return fetch


class FakeBytesFetch:
    def __init__(self, data: bytes = b"jpeg-bytes", media_type: str = "image/jpeg") -> None:
        self.data = data
        self.media_type = media_type
        self.calls: list[str] = []

    def __call__(self, url: str) -> tuple[bytes, str]:
        self.calls.append(url)
        return self.data, self.media_type


class FailingBytesFetch:
    def __call__(self, url: str) -> tuple[bytes, str]:
        raise httpx.ConnectError("image host down")


class UrlStubLLM:
    """LLM seam stub for the url plugin.

    - extract.tier1_enrich → canned ``enriched`` lines, or (default) one blank
      EnrichedLine per numbered input line, or raises when ``enrich_error``.
    - classify_bool (extract.tier2_judge) → ``judge``.
    - Any other structured feature (especially extract.normalize) fails the test.
    """

    def __init__(
        self,
        *,
        enriched: list[EnrichedLine] | None = None,
        judge: bool = True,
        enrich_error: Exception | None = None,
    ) -> None:
        self.enriched = enriched
        self.judge = judge
        self.enrich_error = enrich_error
        self.structured_calls: list[dict[str, Any]] = []
        self.classify_calls: list[dict[str, Any]] = []

    def structured(
        self,
        *,
        feature: str,
        output_model: type[Any],
        content: str | list[dict[str, Any]],
        system: str | None = None,
        model: str | None = None,
        fast: bool = False,
        max_tokens: int = 8000,
    ) -> Any:
        self.structured_calls.append(
            {"feature": feature, "content": content, "system": system, "fast": fast}
        )
        if feature != "extract.tier1_enrich":
            raise AssertionError(f"unexpected structured feature inside url plugin: {feature!r}")
        if self.enrich_error is not None:
            raise self.enrich_error
        if self.enriched is not None:
            return EnrichedLines(lines=self.enriched)
        assert isinstance(content, str)
        count = sum(1 for line in content.splitlines() if line.strip() and line[0].isdigit())
        return EnrichedLines(lines=[EnrichedLine() for _ in range(count)])

    def classify_bool(
        self, *, feature: str, question: str, content: str, max_chars: int = 30000
    ) -> bool:
        self.classify_calls.append({"feature": feature, "question": question, "content": content})
        assert feature == "extract.tier2_judge"
        return self.judge


@pytest.fixture()
def store(tmp_path: Path) -> LocalFileStore:
    return LocalFileStore(tmp_path)


# ---------------------------------------------------------------------------
# Tier 1 — full JSON-LD fixture
# ---------------------------------------------------------------------------


def _flour_enrichment() -> list[EnrichedLine]:
    """Canned enrichment for jsonld_full.html's 8 ingredient lines."""
    return [
        EnrichedLine(name="all-purpose flour", quantity=2.0, unit="cup"),
        EnrichedLine(name="unsalted butter", quantity=1.0, unit="cup", note="room temperature"),
        EnrichedLine(name="granulated sugar", quantity=1.25, unit="cup"),
        EnrichedLine(name="egg", quantity=4.0, note="large"),
        EnrichedLine(name="vanilla extract", quantity=2.0, unit="tsp"),
        EnrichedLine(name="whole milk", quantity=0.5, unit="cup"),
        EnrichedLine(name="baking powder", quantity=1.0, unit="tsp"),
        EnrichedLine(name="salt", note="to taste"),
    ]


def test_tier1_full_jsonld(store: LocalFileStore) -> None:
    llm = UrlStubLLM(enriched=_flour_enrichment())
    image_fetch = FakeBytesFetch()
    extractor = UrlExtractor(
        fetch=_fetch_for(_fixture("jsonld_full.html")), fetch_bytes=image_fetch
    )

    acquired = extractor.acquire(
        {"url": "https://www.dailycrumb.test/recipes/classic-vanilla-pound-cake"},
        llm=llm,  # type: ignore[arg-type]
        store=store,
    )

    assert acquired.meta == {"tier_used": 1}
    assert acquired.prebuilt is not None
    recipe = acquired.prebuilt.recipes[0]
    assert recipe.title == "Classic Vanilla Pound Cake"
    assert len(recipe.steps) == 5
    assert recipe.servings_amount == 4.0
    assert recipe.prep_min == 15 and recipe.cook_min == 30 and recipe.total_min == 45
    # Enrichment zipped onto the verbatim lines.
    lines = recipe.groups[0].lines
    assert lines[0].original_text == "2 cups all-purpose flour"
    assert lines[0].name == "all-purpose flour"
    assert lines[0].quantity == 2.0
    assert lines[0].unit == "cup"
    assert lines[-1].original_text == "salt to taste"
    assert lines[-1].quantity is None and lines[-1].note == "to taste"
    # ONE cheap enrichment call, fast model; never the full normalize pass.
    assert [c["feature"] for c in llm.structured_calls] == ["extract.tier1_enrich"]
    assert llm.structured_calls[0]["fast"] is True
    # Judge never consulted when JSON-LD is complete.
    assert llm.classify_calls == []
    # Hero image fetched via the injected bytes fetcher.
    assert image_fetch.calls == ["https://cdn.dailycrumb.test/images/pound-cake-hero.jpg"]
    assert acquired.source_image == (b"jpeg-bytes", "image/jpeg")
    # Raw HTML retained.
    raw = store.open(acquired.artifacts["raw_html_ref"]).decode("utf-8")
    assert "Classic Vanilla Pound Cake" in raw


def test_tier1_enrich_llm_error_proceeds_unenriched(store: LocalFileStore) -> None:
    llm = UrlStubLLM(enrich_error=LLMError("model refused"))
    extractor = UrlExtractor(
        fetch=_fetch_for(_fixture("jsonld_full.html")), fetch_bytes=FakeBytesFetch()
    )

    acquired = extractor.acquire({"url": "https://x.test/"}, llm=llm, store=store)  # type: ignore[arg-type]

    assert acquired.meta == {"tier_used": 1}
    assert acquired.prebuilt is not None
    lines = acquired.prebuilt.recipes[0].groups[0].lines
    assert lines[0].original_text == "2 cups all-purpose flour"
    assert all(line.name is None and line.quantity is None for line in lines)


def test_tier1_enrich_length_mismatch_leaves_lines_unparsed(store: LocalFileStore) -> None:
    llm = UrlStubLLM(enriched=[EnrichedLine(name="only one line")])  # fixture has 8
    extractor = UrlExtractor(
        fetch=_fetch_for(_fixture("jsonld_full.html")), fetch_bytes=FakeBytesFetch()
    )

    acquired = extractor.acquire({"url": "https://x.test/"}, llm=llm, store=store)  # type: ignore[arg-type]

    assert acquired.prebuilt is not None
    lines = acquired.prebuilt.recipes[0].groups[0].lines
    assert all(line.name is None and line.quantity is None for line in lines)


def test_tier1_image_fetch_failure_is_best_effort(store: LocalFileStore) -> None:
    llm = UrlStubLLM()
    extractor = UrlExtractor(
        fetch=_fetch_for(_fixture("jsonld_full.html")), fetch_bytes=FailingBytesFetch()
    )

    acquired = extractor.acquire({"url": "https://x.test/"}, llm=llm, store=store)  # type: ignore[arg-type]

    assert acquired.meta == {"tier_used": 1}
    assert acquired.source_image is None


def test_tier1_graph_variant(store: LocalFileStore) -> None:
    llm = UrlStubLLM()
    image_fetch = FakeBytesFetch()
    extractor = UrlExtractor(
        fetch=_fetch_for(_fixture("jsonld_graph.html")), fetch_bytes=image_fetch
    )

    acquired = extractor.acquire({"url": "https://x.test/"}, llm=llm, store=store)  # type: ignore[arg-type]

    assert acquired.meta == {"tier_used": 1}
    assert acquired.prebuilt is not None
    recipe = acquired.prebuilt.recipes[0]
    assert recipe.title == "Slow-Braised Short Ribs"
    assert recipe.cook_min == 80  # PT1H20M
    assert recipe.servings_amount == 6.0  # numeric recipeYield
    assert len(recipe.steps) == 5  # plain-string instructions
    # ImageObject url resolved and fetched.
    assert image_fetch.calls == ["https://images.hearthvine.test/short-ribs-1200.jpg"]


def test_tier1_hebrew_verbatim(store: LocalFileStore) -> None:
    llm = UrlStubLLM()
    extractor = UrlExtractor(
        fetch=_fetch_for(_fixture("jsonld_hebrew.html")), fetch_bytes=FakeBytesFetch()
    )

    acquired = extractor.acquire({"url": "https://x.test/"}, llm=llm, store=store)  # type: ignore[arg-type]

    assert acquired.meta == {"tier_used": 1}
    assert acquired.prebuilt is not None
    recipe = acquired.prebuilt.recipes[0]
    assert recipe.title == "שקשוקה ביתית מושלמת"
    assert recipe.language == "he"
    lines = recipe.groups[0].lines
    assert lines[0].original_text == "2 כפות שמן זית"
    assert recipe.steps[0].original_text.startswith("מחממים את שמן הזית")


# ---------------------------------------------------------------------------
# Tier 2 — readable HTML + judge
# ---------------------------------------------------------------------------


def test_tier2_readable_recipe_judged_yes(store: LocalFileStore) -> None:
    llm = UrlStubLLM(judge=True)
    image_fetch = FakeBytesFetch()
    extractor = UrlExtractor(
        fetch=_fetch_for(_fixture("readable_norecipe_jsonld.html")), fetch_bytes=image_fetch
    )

    acquired = extractor.acquire({"url": "https://x.test/"}, llm=llm, store=store)  # type: ignore[arg-type]

    assert acquired.meta == {"tier_used": 2}
    assert acquired.prebuilt is None
    assert acquired.text is not None and "garlic" in acquired.text
    # Judge consulted exactly once; no structured calls at all inside the plugin.
    assert len(llm.classify_calls) == 1
    assert llm.structured_calls == []
    # Readable text + raw html retained.
    assert store.open(acquired.artifacts["readable_text_ref"]).decode("utf-8") == acquired.text
    assert "raw_html_ref" in acquired.artifacts
    # og:image captured best-effort.
    assert image_fetch.calls == ["https://tomskitchen.test/photos/garlic-pasta.jpg"]
    assert acquired.source_image == (b"jpeg-bytes", "image/jpeg")


def test_tier2_article_judged_no_raises_tier_failed(store: LocalFileStore) -> None:
    llm = UrlStubLLM(judge=False)
    extractor = UrlExtractor(fetch=_fetch_for(_fixture("article.html")))

    with pytest.raises(TierFailed) as exc_info:
        extractor.acquire({"url": "https://x.test/"}, llm=llm, store=store)  # type: ignore[arg-type]

    assert "no complete recipe" in exc_info.value.reason
    assert len(llm.classify_calls) == 1


def test_tier2_short_readable_text_fails_without_judge(store: LocalFileStore) -> None:
    html = "<html><body><p>hi</p></body></html>"
    llm = UrlStubLLM()
    extractor = UrlExtractor(fetch=_fetch_for(html))

    with pytest.raises(TierFailed) as exc_info:
        extractor.acquire({"url": "https://x.test/"}, llm=llm, store=store)  # type: ignore[arg-type]

    assert "no readable recipe content" in exc_info.value.reason
    assert llm.classify_calls == []


def test_incomplete_jsonld_falls_through_to_tier2(store: LocalFileStore) -> None:
    """A Recipe node with only 2 ingredients is incomplete → tier 2, no enrich spend."""
    jsonld = {
        "@type": "Recipe",
        "name": "Half a Recipe",
        "recipeIngredient": ["1 cup water", "1 pinch salt"],
        "recipeInstructions": [{"@type": "HowToStep", "text": "Stir."}],
    }
    body = _fixture("readable_norecipe_jsonld.html").replace(
        "</head>",
        '<script type="application/ld+json">' + json.dumps(jsonld) + "</script></head>",
    )
    llm = UrlStubLLM(judge=True)
    extractor = UrlExtractor(fetch=_fetch_for(body), fetch_bytes=FakeBytesFetch())

    acquired = extractor.acquire({"url": "https://x.test/"}, llm=llm, store=store)  # type: ignore[arg-type]

    assert acquired.meta == {"tier_used": 2}
    assert acquired.prebuilt is None
    assert llm.structured_calls == []  # incomplete tier 1 spends nothing on enrichment
    assert len(llm.classify_calls) == 1


# ---------------------------------------------------------------------------
# Fetching / guards
# ---------------------------------------------------------------------------


def test_non_html_content_type_fails(store: LocalFileStore) -> None:
    llm = UrlStubLLM()
    extractor = UrlExtractor(fetch=_fetch_for("%PDF-1.7 ...", content_type="application/pdf"))

    with pytest.raises(TierFailed) as exc_info:
        extractor.acquire({"url": "https://x.test/doc.pdf"}, llm=llm, store=store)  # type: ignore[arg-type]

    assert "not an HTML page" in exc_info.value.reason


def test_default_fetch_http_error_raises_tier_failed() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(503))
    with pytest.raises(TierFailed) as exc_info:
        default_fetch("https://down.test/", client=httpx.Client(transport=transport))
    assert "could not fetch" in exc_info.value.reason


def test_default_fetch_network_error_raises_tier_failed() -> None:
    def explode(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dns failure")

    transport = httpx.MockTransport(explode)
    with pytest.raises(TierFailed) as exc_info:
        default_fetch("https://nowhere.test/", client=httpx.Client(transport=transport))
    assert "could not fetch" in exc_info.value.reason


def test_default_fetch_success_builds_fetch_result() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200, text="<html></html>", headers={"content-type": "text/html; charset=utf-8"}
        )
    )
    fetched = default_fetch("https://ok.test/page", client=httpx.Client(transport=transport))
    assert fetched.status == 200
    assert fetched.html == "<html></html>"
    assert fetched.content_type == "text/html; charset=utf-8"
    assert fetched.url == "https://ok.test/page"


def test_raw_html_artifact_saved_even_when_tiers_fail(store: LocalFileStore) -> None:
    """The raw page is retained BEFORE tier evaluation (spec §6.4 retention)."""
    html = _fixture("article.html")
    llm = UrlStubLLM(judge=False)
    extractor = UrlExtractor(fetch=_fetch_for(html))

    with pytest.raises(TierFailed):
        extractor.acquire({"url": "https://x.test/"}, llm=llm, store=store)  # type: ignore[arg-type]

    sha = hashlib.sha256(html.encode("utf-8")).hexdigest()
    assert store.exists(f"{sha[:16]}/{sha}.html")


def test_url_extractor_is_registered() -> None:
    assert isinstance(EXTRACTORS["url"], UrlExtractor)
