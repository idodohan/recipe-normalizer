"""URL extractor plugin: tiers 1+2 of the spec §6.1 escalation ladder.

Tier 1 — schema.org/Recipe JSON-LD: deterministic structure mapping (no LLM
for the page), plus ONE cheap fast-model pass that parses the ingredient
strings into name/quantity/unit/note. The result is a prebuilt
NormalizeResult; the worker skips the full normalize() pass entirely.

Tier 2 — readable HTML: trafilatura text + a cheap yes/no judge; the text
then flows through the shared normalize stage.

Tier 3 — agentic browser (extraction/browser.py): when tier 2 fails after a
successful fetch, the model drives a headless browser to reach the recipe.
It is reached via the ``_load_browse`` lazy-import seam so this module imports
without playwright; when playwright is absent the tier-2 reason surfaces.

NO network in tests: ``fetch`` (page HTML) and ``fetch_bytes`` (images) are
constructor-injected callables; the registered instance uses the real httpx
implementations. Tier 3 is faked in tests via the ``_load_browse`` seam.

Fetching is deliberately defensive: every redirect hop is netguard-checked,
status/content-type are judged from the response HEADERS, and the body is then
streamed with a hard cap on DECOMPRESSED bytes (a gzip bomb is tiny on the wire)
plus a wall-clock deadline for the whole fetch (httpx timeouts are per-operation
and a slowloris trickle never trips them).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import TYPE_CHECKING, Any, ClassVar

import httpx
import trafilatura
from pydantic import BaseModel, Field

from recipe_normalizer.extraction.base import Acquired, TierFailed, register
from recipe_normalizer.extraction.jsonld import (
    find_recipe_jsonld,
    is_complete,
    jsonld_to_normalize_result,
)
from recipe_normalizer.llm.client import CostCapExceeded, LLMError
from recipe_normalizer.netguard import MAX_REDIRECTS, UnsafeUrlError, assert_public_url

if TYPE_CHECKING:
    from recipe_normalizer.extraction.normalize import NormalizeResult
    from recipe_normalizer.filestore import FileStore
    from recipe_normalizer.llm.client import LLMClient

__all__ = ["EnrichedLine", "EnrichedLines", "FetchResult", "UrlExtractor", "default_fetch"]

logger = logging.getLogger(__name__)

# Minimum readable-text length for tier 2 — anything shorter cannot plausibly
# contain a complete recipe.
_MIN_READABLE_CHARS = 200


def _load_browse() -> Callable[..., Acquired]:
    """Lazy import of the tier-3 agentic browser (keeps playwright optional).

    Isolated in one function so tests can patch it and so a missing playwright
    install raises a clean ImportError that the plugin downgrades gracefully.
    """
    from recipe_normalizer.extraction.browser import browse_for_recipe

    return browse_for_recipe


_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,he;q=0.8",
}

_FETCH_TIMEOUT_S = 15.0
# httpx timeouts are per-operation: a server trickling one byte per second can
# hold a connection open forever without ever tripping them. This bounds the
# WHOLE fetch (all redirect hops + body read).
_FETCH_DEADLINE_S = 60.0
# Hard caps on DECOMPRESSED body bytes — a gzip bomb is small on the wire and
# only shows its size once inflated, so the cap is enforced while reading.
_MAX_HTML_BYTES = 5 * 1024 * 1024
_MAX_IMAGE_BYTES = 10 * 1024 * 1024  # matches cookbook.service.MAX_IMAGE_BYTES


# ---------------------------------------------------------------------------
# Fetching (injectable seam — tests never touch the network)
# ---------------------------------------------------------------------------


@dataclass
class FetchResult:
    url: str  # final url after redirects
    status: int
    html: str
    content_type: str


@dataclass
class _Body:
    url: str  # final url after redirects
    status: int
    data: bytes
    content_type: str


def _too_large(max_bytes: int) -> TierFailed:
    return TierFailed(f"response body too large (over {max_bytes // (1024 * 1024)} MB)")


def _read_capped(
    response: httpx.Response,
    *,
    max_bytes: int,
    deadline: float,
    clock: Callable[[], float],
) -> bytes:
    """Read a streaming body incrementally, aborting past *max_bytes* or *deadline*.

    Uses ``iter_bytes()`` with NO chunk size on purpose: httpx's fixed-size
    chunker buffers until it has a full chunk, so a slowloris trickling a few
    bytes per read would never yield and the deadline check would never run.
    Un-sized, it yields per decoded network chunk, so the wall-clock deadline
    is enforced against exactly that attack. The size cap is unaffected.
    """
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_bytes():
        if clock() > deadline:
            raise TierFailed(f"fetch took too long (over {_FETCH_DEADLINE_S:.0f}s)")
        total += len(chunk)
        if total > max_bytes:
            raise _too_large(max_bytes)
        chunks.append(chunk)
    return b"".join(chunks)


def _declared_length(response: httpx.Response) -> int | None:
    raw = response.headers.get("content-length")
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _safe_stream(
    url: str,
    *,
    max_bytes: int,
    accept: Callable[[str], None] | None = None,
    client: httpx.Client | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> _Body:
    """Stream a GET with per-hop SSRF validation and hard size/time bounds.

    Redirects are followed manually so every hop (not just the first URL) is
    checked against netguard. Status and content-type are judged from the
    response HEADERS — the body is only read once it is wanted, and then only
    incrementally up to *max_bytes* decompressed.
    """
    own_client = client is None
    http = client or httpx.Client(timeout=_FETCH_TIMEOUT_S)
    deadline = clock() + _FETCH_DEADLINE_S
    try:
        for _ in range(MAX_REDIRECTS + 1):
            if clock() > deadline:
                raise TierFailed(f"fetch took too long (over {_FETCH_DEADLINE_S:.0f}s)")
            assert_public_url(url)
            request = http.build_request("GET", url, headers=_HEADERS)
            response = http.send(request, stream=True, follow_redirects=False)
            try:
                if response.is_redirect and response.next_request is not None:
                    url = str(response.next_request.url)
                    continue
                response.raise_for_status()
                content_type = response.headers.get("content-type", "")
                if accept is not None:
                    accept(content_type)
                declared = _declared_length(response)
                if declared is not None and declared > max_bytes:
                    raise _too_large(max_bytes)
                data = _read_capped(response, max_bytes=max_bytes, deadline=deadline, clock=clock)
            finally:
                response.close()
            return _Body(
                url=str(response.url),
                status=response.status_code,
                data=data,
                content_type=content_type,
            )
        raise TierFailed(f"too many redirects (>{MAX_REDIRECTS})")
    except UnsafeUrlError as exc:
        raise TierFailed(f"blocked unsafe URL: {exc}") from exc
    except httpx.HTTPError as exc:
        raise TierFailed(f"could not fetch page: {exc}") from exc
    finally:
        if own_client:
            http.close()


def _charset_of(content_type: str) -> str:
    for parameter in content_type.split(";")[1:]:
        key, _, value = parameter.partition("=")
        if key.strip().lower() == "charset":
            return value.strip().strip("\"'") or "utf-8"
    return "utf-8"


def _decode(data: bytes, content_type: str) -> str:
    try:
        return data.decode(_charset_of(content_type), errors="replace")
    except LookupError:  # unknown charset label
        return data.decode("utf-8", errors="replace")


def _require_html(content_type: str) -> None:
    if "text/html" not in content_type.lower():
        raise TierFailed(f"not an HTML page (content-type: {content_type or '?'})")


def default_fetch(
    url: str,
    *,
    client: httpx.Client | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> FetchResult:
    """Real page fetcher: browser-ish UA, netguard-validated redirects, bounded
    body (5 MB decompressed) and a 60s wall-clock deadline."""
    body = _safe_stream(
        url, max_bytes=_MAX_HTML_BYTES, accept=_require_html, client=client, clock=clock
    )
    return FetchResult(
        url=body.url,
        status=body.status,
        html=_decode(body.data, body.content_type),
        content_type=body.content_type,
    )


def default_fetch_bytes(
    url: str,
    *,
    client: httpx.Client | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[bytes, str]:
    """Real binary fetcher for hero images; returns (data, media_type)."""
    body = _safe_stream(url, max_bytes=_MAX_IMAGE_BYTES, client=client, clock=clock)
    media_type = body.content_type or "application/octet-stream"
    return body.data, media_type.split(";")[0].strip()


# ---------------------------------------------------------------------------
# Tier-1 ingredient-line enrichment (the one cheap LLM call)
# ---------------------------------------------------------------------------


class EnrichedLine(BaseModel):
    name: str | None = None
    quantity: float | None = None
    unit: str | None = None
    note: str | None = None
    is_optional: bool = False


class EnrichedLines(BaseModel):
    lines: list[EnrichedLine] = Field(default_factory=list)


_ENRICH_SYSTEM = """\
You parse recipe ingredient lines. You receive a numbered list of ingredient lines, verbatim \
from a recipe. Return one output entry PER input line, in the same order. Per line:
- name: the canonical ingredient name in ENGLISH (translate when needed; e.g. קמח לכל מטרה → \
"all-purpose flour"). Minimal: drop quantities, units, and preparation ("chopped", "sifted"). \
Null only when the line names no identifiable ingredient.
- quantity: a number. Fractions and unicode fractions become decimals (1/2 → 0.5, 1½ → 1.5). \
For ranges ("2-3 cloves") use the lower bound and record the range in note. Null when no \
quantity is given ("salt to taste").
- unit: a short singular token ("cups" → cup, "tablespoons" → tbsp, "grams" → g); keep \
non-English units in their source language (e.g. כוס). Null when there is no unit ("2 eggs").
- note: qualifiers and preparation ("finely chopped", "room temperature", "to taste", "2-3"). \
Null if none.
- is_optional: true when the line marks the ingredient optional ("optional", "if desired", \
"אופציונלי").
Never invent values; missing stays null."""


def _enrich_lines(result: NormalizeResult, llm: LLMClient) -> None:
    """Parse the tier-1 ingredient strings with one cheap fast-model call.

    Structure from JSON-LD stays authoritative — the LLM only fills per-line
    name/quantity/unit/note/is_optional, zipped on defensively: a length
    mismatch or any LLMError leaves the lines unparsed (tier 1 must not die
    on the cheap call). CostCapExceeded still propagates: the cap is a
    job-level abort, not a call failure.
    """
    lines = [line for group in result.recipes[0].groups for line in group.lines]
    numbered = "\n".join(f"{i}. {line.original_text}" for i, line in enumerate(lines, start=1))
    try:
        enriched = llm.structured(
            feature="extract.tier1_enrich",
            output_model=EnrichedLines,
            content=numbered,
            system=_ENRICH_SYSTEM,
            fast=True,
        )
    except CostCapExceeded:
        raise
    except LLMError:
        logger.warning("tier-1 enrichment failed; proceeding with unparsed lines", exc_info=True)
        return
    if len(enriched.lines) != len(lines):
        logger.warning(
            "tier-1 enrichment returned %d lines for %d inputs; leaving lines unparsed",
            len(enriched.lines),
            len(lines),
        )
        return
    for line, parsed in zip(lines, enriched.lines, strict=True):
        line.name = parsed.name
        line.quantity = parsed.quantity
        line.unit = parsed.unit
        line.note = parsed.note
        line.is_optional = parsed.is_optional


# ---------------------------------------------------------------------------
# og:image (tier-2 hero image, best-effort)
# ---------------------------------------------------------------------------


class _OgImageCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.url: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "meta" or self.url is not None:
            return
        attr_map = dict(attrs)
        if attr_map.get("property") == "og:image" and attr_map.get("content"):
            self.url = attr_map["content"]


def _og_image_url(html: str) -> str | None:
    collector = _OgImageCollector()
    collector.feed(html)
    return collector.url


# ---------------------------------------------------------------------------
# The plugin
# ---------------------------------------------------------------------------


class UrlExtractor:
    """payload {"url": str} → Acquired via tier 1 (JSON-LD) or tier 2 (readable)."""

    input_type: ClassVar[str] = "url"

    def __init__(
        self,
        fetch: Callable[[str], FetchResult] | None = None,
        fetch_bytes: Callable[[str], tuple[bytes, str]] | None = None,
    ) -> None:
        self._fetch = fetch or default_fetch
        self._fetch_bytes = fetch_bytes or default_fetch_bytes

    def acquire(self, payload: dict[str, Any], *, llm: LLMClient, store: FileStore) -> Acquired:
        url: str = payload["url"]
        fetched = self._fetch(url)
        _require_html(fetched.content_type)  # injected fetchers may not check
        # Retain the raw page first (spec §6.4) — even if every tier fails.
        artifacts = {"raw_html_ref": store.save(fetched.html.encode("utf-8"), suffix="html")}

        acquired = self._tier1(fetched.html, llm=llm, artifacts=artifacts)
        if acquired is not None:
            return acquired
        try:
            return self._tier2(fetched.html, llm=llm, store=store, artifacts=artifacts)
        except TierFailed as tier2_failure:
            # Fetch succeeded but the text was insufficient → escalate to tier 3.
            # (Network/content-type failures raise above this and never escalate.)
            return self._tier3(
                url, llm=llm, store=store, artifacts=artifacts, tier2_failure=tier2_failure
            )

    # -- tier 1: deterministic JSON-LD ------------------------------------------

    def _tier1(self, html: str, *, llm: LLMClient, artifacts: dict[str, str]) -> Acquired | None:
        data = find_recipe_jsonld(html)
        if data is None:
            return None
        result, image_url = jsonld_to_normalize_result(data)
        if not is_complete(result):
            return None  # fall through to tier 2 before spending on enrichment
        _enrich_lines(result, llm)
        return Acquired(
            prebuilt=result,
            source_image=self._fetch_image(image_url),
            artifacts=artifacts,
            meta={"tier_used": 1},
        )

    # -- tier 2: readable text + cheap judge -------------------------------------

    def _tier2(
        self, html: str, *, llm: LLMClient, store: FileStore, artifacts: dict[str, str]
    ) -> Acquired:
        readable: str | None = trafilatura.extract(html, favor_recall=True, include_tables=True)
        if not readable or len(readable.strip()) < _MIN_READABLE_CHARS:
            raise TierFailed("page has no readable recipe content")
        has_recipe = llm.classify_bool(
            feature="extract.tier2_judge",
            question=(
                "Does this text contain at least one complete cooking or drink recipe, "
                "with both an ingredient list and preparation instructions?"
            ),
            content=readable,
        )
        if not has_recipe:
            # Tier 3 (agentic browser) is the next task; fail gracefully until then.
            raise TierFailed("no complete recipe found in page text (tier 3 not yet available)")
        artifacts["readable_text_ref"] = store.save(readable.encode("utf-8"), suffix="txt")
        return Acquired(
            text=readable,
            source_image=self._fetch_image(_og_image_url(html)),
            artifacts=artifacts,
            meta={"tier_used": 2},
        )

    # -- tier 3: agentic browser -------------------------------------------------

    def _tier3(
        self,
        url: str,
        *,
        llm: LLMClient,
        store: FileStore,
        artifacts: dict[str, str],
        tier2_failure: TierFailed,
    ) -> Acquired:
        try:
            browse = _load_browse()
        except ImportError:
            logger.warning("tier 3 unavailable (playwright not installed); failing on tier 2")
            raise tier2_failure from None
        try:
            acquired = browse(url, llm=llm, store=store)
        except TierFailed as tier3_failure:
            # Merge tier-2's retained raw html into the failure's artifacts so the
            # review screen can show the original page alongside the screenshot.
            tier3_failure.artifacts = {**artifacts, **(tier3_failure.artifacts or {})}
            raise
        acquired.artifacts = {**artifacts, **acquired.artifacts}
        acquired.meta.setdefault("tier_used", 3)
        return acquired

    # -- helpers -----------------------------------------------------------------

    def _fetch_image(self, image_url: str | None) -> tuple[bytes, str] | None:
        """Best-effort hero-image download — failures never sink the tier."""
        if not image_url:
            return None
        try:
            return self._fetch_bytes(image_url)
        except Exception:
            logger.warning("could not fetch recipe image %s", image_url, exc_info=True)
            return None


register(UrlExtractor())
