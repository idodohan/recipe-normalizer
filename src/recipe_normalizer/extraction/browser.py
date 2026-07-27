"""Tier-3 agentic browser (spec §6.1): drive a real browser with the LLM.

When tiers 1+2 fail (JS-rendered pages, "jump to recipe" walls, cookie/overlay
gates), the model operates a headless Chromium through a small tool set —
screenshot / click / press_escape / scroll / wait / capture_content — until it
captures readable recipe text. Hard budgets bound the run: at most 15 tool-loop
iterations AND 90s wall clock. On exhaustion the final screenshot + action log
are retained so the user sees why the tier gave up (spec §6.1).

Playwright is imported lazily (only the real page factory needs it) so this
module — and url_plugin's ``_load_browse`` seam — import cleanly without it.
NO network / NO real browser in tests: ``page_factory`` and ``clock`` are
injectable, and the LLM tool loop is the seam from llm.client.

SSRF guard: the initial navigation is checked with netguard.assert_public_url
before ``page.goto``, AND a ``page.route("**/*")`` guard is installed first so
HTTP(S) requests the browser makes — document redirects, subresources, and
anything the model's ``click`` tool navigates to — are resolved through the same
netguard check and aborted when not globally routable. Chromium follows
redirects on its own, so without that guard a page could bounce the browser to
169.254.169.254 and have ``capture_content`` exfiltrate cloud credentials. The
context is created with ``service_workers="block"`` because a service worker's
fetches would bypass ``page.route``; WebSocket handshakes are out of scope for
route interception (no request type we act on rides them). The current page URL
is re-checked before every tool action as a backstop, and a navigation that
lands somewhere non-public aborts the tier with no screenshot.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import time
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import trafilatura

from recipe_normalizer.extraction.base import Acquired, TierFailed
from recipe_normalizer.llm.client import BudgetExceeded, image_block
from recipe_normalizer.netguard import UnsafeUrlError, assert_public_url

if TYPE_CHECKING:
    from contextlib import AbstractContextManager

    from playwright.sync_api import ViewportSize

    from recipe_normalizer.filestore import FileStore
    from recipe_normalizer.llm.client import LLMClient

__all__ = ["browse_for_recipe", "looks_like_recipe_text"]

logger = logging.getLogger(__name__)

_MAX_ITERATIONS = 15
_TIME_BUDGET_S = 90.0
_CLICK_TIMEOUT_MS = 5_000
_MAX_WAIT_MS = 5_000
_VIEWPORT: ViewportSize = {"width": 1280, "height": 2000}
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_BULLETS = "-*•·▪◦‣⁃–—"
_VULGAR_FRACTIONS = "½⅓⅔¼¾⅕⅖⅗⅘⅙⅚⅛⅜⅝⅞⅐⅑⅒"
# A line "looks like" an ingredient line when it leads with a quantity.
_INGREDIENT_LINE = re.compile(rf"^[{re.escape(_BULLETS)}\s]*[\d{_VULGAR_FRACTIONS}]")
_MIN_INGREDIENT_LINES = 3


_SYSTEM = """\
You are operating a web browser to reach the full text of a single cooking or \
drink recipe on the page you have been given. The page may hide the recipe \
behind obstacles: cookie banners, newsletter pop-ups, overlay ads, a "Jump to \
Recipe" link near the top, or collapsed/"show more" sections.

Strategy:
- Start from the screenshot you are given; take a fresh screenshot whenever you \
need to see the current state.
- Dismiss cookie banners and pop-ups (press_escape, or click their accept/close \
control), click "Jump to Recipe" when present, scroll down, and expand any \
collapsed ingredient or instruction sections.
- When the ingredients AND instructions are visible, call capture_content to \
grab the page text. If the captured text is not yet a complete recipe, keep \
working and capture again.

Budget: you have at most 15 actions and limited time. Be efficient — do not \
re-screenshot needlessly. Stop by capturing as soon as the full recipe is on \
screen."""

_GOAL_TEMPLATE = (
    "Reach and capture the full recipe on this page: {url}\n"
    "The opening screenshot above shows the page as first loaded."
)

_TOOLS: list[dict[str, Any]] = [
    {
        "name": "screenshot",
        "description": "Capture a screenshot of the current viewport so you can see the page.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "click",
        "description": (
            "Click an element. Provide visible link/button text (preferred) or a CSS selector."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"target": {"type": "string"}},
            "required": ["target"],
        },
    },
    {
        "name": "press_escape",
        "description": "Press the Escape key to dismiss a modal, pop-up, or overlay.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "scroll",
        "description": "Scroll the page vertically by the given number of pixels (negative = up).",
        "input_schema": {
            "type": "object",
            "properties": {"pixels": {"type": "integer"}},
            "required": ["pixels"],
        },
    },
    {
        "name": "wait",
        "description": "Wait for the page to settle (milliseconds, capped at 5000).",
        "input_schema": {
            "type": "object",
            "properties": {"ms": {"type": "integer"}},
            "required": ["ms"],
        },
    },
    {
        "name": "capture_content",
        "description": (
            "Extract the readable text of the current page. Call this once the full "
            "recipe (ingredients and instructions) is on screen."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
]


def looks_like_recipe_text(text: str) -> bool:
    """Cheap heuristic: does *text* contain enough quantity-leading lines?

    A line counts when, after stripping bullet markers, it leads with a digit or
    a unicode vulgar fraction. Three such lines reads as an ingredient list; one
    stray "1990 was..." in an article does not.
    """
    count = sum(1 for line in text.splitlines() if _INGREDIENT_LINE.match(line))
    return count >= _MIN_INGREDIENT_LINES


# ---------------------------------------------------------------------------
# Control-flow signals — BaseException so they bypass tool_loop's
# ``except Exception`` (which turns plain errors into tool_result feedback).
# ---------------------------------------------------------------------------


class _Captured(BaseException):  # noqa: N818 - internal control signal, not an error
    def __init__(self, text: str) -> None:
        super().__init__("captured")
        self.text = text


class _BudgetExhausted(BaseException):  # noqa: N818 - internal control signal
    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class _UnsafeNavigation(BaseException):  # noqa: N818 - internal control signal
    def __init__(self, url: str) -> None:
        super().__init__(f"page navigated to a non-public URL ({url})")
        self.url = url


# ---------------------------------------------------------------------------
# SSRF guard — every request the browser makes goes through netguard
# ---------------------------------------------------------------------------

# Schemes that cannot reach the network at all; blocking them would only break
# inline assets on legitimate pages.
_INERT_SCHEMES = frozenset({"data", "blob", "about"})


def _request_allowed(url: str, cache: dict[str, bool]) -> bool:
    """Allow/deny one browser request, memoized per origin.

    ``assert_public_url`` only depends on scheme + host, so the (DNS-resolving)
    decision is cached per origin — a page pulls dozens of subresources from a
    handful of hosts.
    """
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    if scheme in _INERT_SCHEMES:
        return True
    origin = f"{scheme}://{(parsed.hostname or '').lower()}"
    cached = cache.get(origin)
    if cached is not None:
        return cached
    try:
        assert_public_url(url)
    except UnsafeUrlError as exc:
        logger.warning("tier-3 blocked request to %s: %s", url, exc)
        allowed = False
    else:
        allowed = True
    cache[origin] = allowed
    return allowed


def _install_route_guard(page: Any) -> list[str]:
    """Abort every request whose host is not globally routable.

    Returns a list that accumulates the URLs blocked by the guard, so a caller
    whose ``page.goto`` fails can tell an SSRF abort (a blocked navigation
    surfaces as a generic ``net::ERR_FAILED`` from Playwright, not our own
    exception type) from an ordinary load failure.
    """
    cache: dict[str, bool] = {}
    blocked: list[str] = []

    def handler(route: Any) -> None:
        url = ""
        try:
            url = route.request.url
            if _request_allowed(url, cache):
                route.continue_()
                return
            blocked.append(url)
            route.abort()
        except Exception:
            # A handler that raises leaves the request hanging until the page
            # times out — always resolve it, denying by default.
            logger.warning("tier-3 route guard failed for %s", url, exc_info=True)
            blocked.append(url or "<unknown>")
            with contextlib.suppress(Exception):
                route.abort()

    page.route("**/*", handler)
    return blocked


def _assert_current_url_public(page: Any) -> None:
    """Backstop for navigation the route guard cannot veto (history, fragments)."""
    url = str(page.url or "")
    if not url or urlparse(url).scheme.lower() in _INERT_SCHEMES:
        return
    try:
        assert_public_url(url)
    except UnsafeUrlError as exc:
        raise _UnsafeNavigation(url) from exc


def _default_page_factory() -> AbstractContextManager[Any]:
    """Real headless-Chromium context manager (lazy playwright import)."""

    @contextlib.contextmanager
    def _open() -> Iterator[Any]:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                # service_workers="block": a service worker's fetches bypass
                # page.route, so blocking them entirely keeps the SSRF guard
                # comprehensive for the request types we can intercept.
                context = browser.new_context(
                    viewport=_VIEWPORT,
                    user_agent=_USER_AGENT,
                    service_workers="block",
                )
                page = context.new_page()
                yield page
            finally:
                browser.close()

    return _open()


def browse_for_recipe(
    url: str,
    *,
    llm: LLMClient,
    store: FileStore,
    page_factory: Callable[[], AbstractContextManager[Any]] | None = None,
    time_budget_s: float = _TIME_BUDGET_S,
    clock: Callable[[], float] | None = None,
) -> Acquired:
    """Drive a browser to capture the recipe text; raise TierFailed on failure."""
    page_factory = page_factory or _default_page_factory
    clock = clock or time.monotonic
    actions_log: list[dict[str, Any]] = []

    with page_factory() as page:
        try:
            assert_public_url(url)
        except UnsafeUrlError as exc:
            raise TierFailed(f"blocked unsafe URL: {exc}") from exc
        blocked = _install_route_guard(page)  # must precede any navigation
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=20_000)
            _assert_current_url_public(page)
        except _UnsafeNavigation as exc:
            raise TierFailed(f"blocked unsafe URL: {exc}") from exc
        except Exception as exc:
            # A route-guard abort of the main navigation surfaces here as a
            # generic Playwright error (net::ERR_FAILED), not _UnsafeNavigation.
            # Attribute it correctly and, either way, fail the tier cleanly
            # instead of leaking an opaque browser error to the worker.
            if blocked:
                raise TierFailed(f"blocked unsafe URL: {blocked[0]}") from exc
            raise TierFailed(f"page failed to load: {exc}") from exc
        opening = page.screenshot(type="png")
        start = clock()
        initial_content: list[dict[str, Any]] = [
            image_block(opening, "image/png"),
            {"type": "text", "text": _GOAL_TEMPLATE.format(url=url)},
        ]
        execute = _make_execute(
            page, actions_log, start=start, clock=clock, time_budget_s=time_budget_s
        )
        try:
            llm.tool_loop(
                feature="extract.browser",
                system=_SYSTEM,
                tools=_TOOLS,
                initial_content=initial_content,
                execute=execute,
                max_iterations=_MAX_ITERATIONS,
            )
        except _Captured as captured:
            return _on_capture(page, store, actions_log, captured.text)
        except _UnsafeNavigation as exc:
            # No screenshot/artifacts: whatever the page holds now came from a
            # host netguard rejects, and must not be retained or shown.
            raise TierFailed(f"blocked unsafe URL: {exc}") from exc
        except _BudgetExhausted as exc:
            raise _fail(
                page, store, actions_log, f"browser budget exhausted ({exc.detail})"
            ) from exc
        except BudgetExceeded as exc:
            raise _fail(
                page, store, actions_log, "browser budget exhausted (action limit reached)"
            ) from exc
        # The model returned end_turn without ever capturing the recipe.
        raise _fail(page, store, actions_log, "browser loop ended without capturing a recipe")


def _make_execute(
    page: Any,
    actions_log: list[dict[str, Any]],
    *,
    start: float,
    clock: Callable[[], float],
    time_budget_s: float,
) -> Callable[[str, dict[str, Any]], str | list[dict[str, Any]]]:
    def execute(name: str, tool_input: dict[str, Any]) -> str | list[dict[str, Any]]:
        if clock() - start > time_budget_s:
            raise _BudgetExhausted("wall-clock time limit reached")
        # The page may have navigated on its own (JS, meta refresh) since the
        # last action — never act on a page that is no longer public.
        _assert_current_url_public(page)

        if name == "screenshot":
            shot = page.screenshot(type="png")
            _log(actions_log, name, tool_input)
            return [image_block(shot, "image/png")]

        if name == "click":
            target = str(tool_input.get("target", ""))
            try:
                page.get_by_text(target).first.click(timeout=_CLICK_TIMEOUT_MS)
            except Exception:
                page.locator(target).first.click(timeout=_CLICK_TIMEOUT_MS)
            _assert_current_url_public(page)  # the click may have navigated
            _log(actions_log, name, tool_input)
            return f"clicked {target!r}"

        if name == "press_escape":
            page.keyboard.press("Escape")
            _log(actions_log, name, tool_input)
            return "pressed Escape"

        if name == "scroll":
            pixels = int(tool_input.get("pixels", 1000))
            page.mouse.wheel(0, pixels)
            _log(actions_log, name, tool_input)
            return f"scrolled {pixels}px"

        if name == "wait":
            ms = min(int(tool_input.get("ms", 1000)), _MAX_WAIT_MS)
            page.wait_for_timeout(ms)
            _log(actions_log, name, tool_input)
            return f"waited {ms}ms"

        if name == "capture_content":
            text = trafilatura.extract(page.content(), favor_recall=True, include_tables=True) or ""
            _log(actions_log, name, tool_input)
            if text and looks_like_recipe_text(text):
                raise _Captured(text)
            return (
                "CAPTURED_TEXT_INSUFFICIENT: the page does not yet show a complete recipe "
                f"(ingredient list + instructions). Keep working. Extracted so far:\n{text}"
            )

        _log(actions_log, name, tool_input)
        return f"unknown tool {name!r}"

    return execute


def _log(actions_log: list[dict[str, Any]], tool: str, tool_input: dict[str, Any]) -> None:
    actions_log.append({"tool": tool, "input": dict(tool_input)})


def _on_capture(
    page: Any, store: FileStore, actions_log: list[dict[str, Any]], text: str
) -> Acquired:
    artifacts: dict[str, str] = {}
    screenshot_ref = _save_screenshot(page, store)
    if screenshot_ref is not None:
        artifacts["tier3_screenshot_ref"] = screenshot_ref
    return Acquired(
        text=text,
        source_image=None,
        artifacts=artifacts,
        meta={"tier_used": 3, "actions_log": actions_log},
    )


def _fail(
    page: Any, store: FileStore, actions_log: list[dict[str, Any]], reason: str
) -> TierFailed:
    """Build a graceful TierFailed with the final screenshot + action log retained."""
    screenshot_ref = _save_screenshot(page, store)
    artifacts: dict[str, str] = {
        "browser_log_ref": store.save(
            json.dumps(actions_log, ensure_ascii=False).encode("utf-8"), suffix="json"
        )
    }
    if screenshot_ref is not None:
        artifacts["tier3_screenshot_ref"] = screenshot_ref
    return TierFailed(reason, screenshot_ref=screenshot_ref, artifacts=artifacts)


def _save_screenshot(page: Any, store: FileStore) -> str | None:
    """Best-effort: a closed/broken page must not mask the real failure."""
    try:
        return store.save(page.screenshot(type="png"), suffix="png")
    except Exception:
        logger.warning("could not capture final tier-3 screenshot", exc_info=True)
        return None
