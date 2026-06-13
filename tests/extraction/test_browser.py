"""Tests for the tier-3 agentic browser (extraction/browser.py).

NO real browser, NO network, NO real LLM: FakePage stands in for the
playwright Page (injected via ``page_factory``), and ScriptedLLM implements
``tool_loop`` by ACTUALLY calling ``execute`` per a scripted action list —
so the real tool wiring (click fallback, capture heuristic, budget checks,
screenshot/artifact persistence) is exercised end to end.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from recipe_normalizer.extraction.base import Acquired, TierFailed
from recipe_normalizer.extraction.browser import browse_for_recipe, looks_like_recipe_text
from recipe_normalizer.filestore import LocalFileStore
from recipe_normalizer.llm.client import BudgetExceeded

# A real (1x1) PNG so anything that inspects the bytes stays happy.
TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

RECIPE_HTML = """<!DOCTYPE html>
<html lang="en"><head><title>Best Banana Bread</title></head>
<body><main><article>
<h1>Best Banana Bread</h1>
<p>This banana bread is the one my grandmother made every Sunday, and it has
never once failed me in twenty years of baking it at home.</p>
<h2>Ingredients</h2>
<ul>
<li>2 cups all-purpose flour</li>
<li>1 cup mashed ripe banana</li>
<li>0.5 cup melted butter</li>
<li>2 eggs, lightly beaten</li>
<li>1 tsp baking soda</li>
</ul>
<h2>Method</h2>
<p>Preheat the oven to 175C. Mix the dry ingredients, fold in the wet ones,
pour into a loaf pan and bake for 55 minutes until golden on top.</p>
</article></main></body></html>"""

ARTICLE_HTML = """<!DOCTYPE html>
<html lang="en"><head><title>Why We Bake</title></head>
<body><main><article>
<h1>Why We Bake</h1>
<p>Baking has always been a communal act, one that binds neighborhoods
together far more tightly than most of us tend to acknowledge today.</p>
<p>Historians trace communal ovens back many centuries, and the sociology of
sharing bread remains a rich field of contemporary academic study.</p>
<p>In this essay we explore the meaning of baking without ever giving you an
actual recipe, because this publication is far too serious for that.</p>
</article></main></body></html>"""


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeClickTarget:
    def __init__(self, page: FakePage, kind: str, target: str, fail: bool) -> None:
        self._page = page
        self._kind = kind
        self._target = target
        self._fail = fail

    @property
    def first(self) -> FakeClickTarget:
        return self

    def click(self, timeout: float | None = None) -> None:
        if self._fail:
            raise RuntimeError(f"no element for {self._target!r}")
        self._page.calls.append((f"click_{self._kind}", self._target))


class FakeKeyboard:
    def __init__(self, page: FakePage) -> None:
        self._page = page

    def press(self, key: str) -> None:
        self._page.calls.append(("press", key))


class FakeMouse:
    def __init__(self, page: FakePage) -> None:
        self._page = page

    def wheel(self, dx: int, dy: int) -> None:
        self._page.calls.append(("wheel", dx, dy))


class FakePage:
    """Records every call; ``content()`` walks through *htmls* (sticky last)."""

    def __init__(self, htmls: list[str] | None = None, *, fail_text_click: bool = False) -> None:
        self.htmls = htmls or [RECIPE_HTML]
        self.fail_text_click = fail_text_click
        self.calls: list[tuple[Any, ...]] = []
        self.keyboard = FakeKeyboard(self)
        self.mouse = FakeMouse(self)
        self._content_idx = 0

    def goto(self, url: str, **kwargs: Any) -> None:
        self.calls.append(("goto", url, kwargs))

    def screenshot(self, *, type: str = "png") -> bytes:  # noqa: A002 - playwright's name
        self.calls.append(("screenshot", type))
        return TINY_PNG

    def content(self) -> str:
        html = self.htmls[min(self._content_idx, len(self.htmls) - 1)]
        self._content_idx += 1
        return html

    def get_by_text(self, text: str) -> FakeClickTarget:
        return FakeClickTarget(self, "text", text, fail=self.fail_text_click)

    def locator(self, selector: str) -> FakeClickTarget:
        return FakeClickTarget(self, "css", selector, fail=False)

    def wait_for_timeout(self, ms: float) -> None:
        self.calls.append(("wait_for_timeout", ms))


class ScriptedLLM:
    """tool_loop stub that drives the REAL execute callable per a script.

    Mirrors LLMClient.tool_loop's contract: plain Exceptions from execute
    become error results fed back to the model (the loop continues), while
    the browser module's BaseException control-flow signals propagate.
    """

    def __init__(self, actions: list[tuple[str, dict[str, Any]]], *, exhaust: bool = False):
        self.actions = actions
        self.exhaust = exhaust
        self.results: list[Any] = []
        self.loop_kwargs: dict[str, Any] | None = None

    def tool_loop(
        self,
        *,
        feature: str,
        system: str,
        tools: list[dict[str, Any]],
        initial_content: list[dict[str, Any]],
        execute: Any,
        max_iterations: int = 15,
        max_tokens: int = 8000,
    ) -> Any:
        self.loop_kwargs = {
            "feature": feature,
            "system": system,
            "tools": tools,
            "initial_content": initial_content,
            "max_iterations": max_iterations,
        }
        for name, tool_input in self.actions:
            try:
                self.results.append(execute(name, tool_input))
            except Exception as exc:  # same shape as the real loop's is_error path
                self.results.append(f"ERROR: {exc}")
        if self.exhaust:
            raise BudgetExceeded(f"tool loop exceeded {max_iterations} iterations")
        return SimpleNamespace(stop_reason="end_turn", content=[])


def _factory_for(page: FakePage) -> Any:
    return lambda: contextlib.nullcontext(page)


@pytest.fixture()
def store(tmp_path: Path) -> LocalFileStore:
    return LocalFileStore(tmp_path)


HAPPY_SCRIPT: list[tuple[str, dict[str, Any]]] = [
    ("screenshot", {}),
    ("press_escape", {}),
    ("scroll", {"pixels": 1200}),
    ("capture_content", {}),
]


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_happy_path_captures_recipe(store: LocalFileStore) -> None:
    page = FakePage([RECIPE_HTML])
    llm = ScriptedLLM(HAPPY_SCRIPT)

    acquired = browse_for_recipe(
        "https://blocked.test/banana-bread",
        llm=llm,  # type: ignore[arg-type]
        store=store,
        page_factory=_factory_for(page),
    )

    assert isinstance(acquired, Acquired)
    assert acquired.text is not None
    assert "2 cups all-purpose flour" in acquired.text
    assert acquired.source_image is None
    assert acquired.meta["tier_used"] == 3
    # One log entry per scripted action, in order.
    assert [entry["tool"] for entry in acquired.meta["actions_log"]] == [
        "screenshot",
        "press_escape",
        "scroll",
        "capture_content",
    ]
    # Final screenshot persisted.
    assert store.open(acquired.artifacts["tier3_screenshot_ref"]) == TINY_PNG
    # Real navigation params.
    goto = next(call for call in page.calls if call[0] == "goto")
    assert goto[1] == "https://blocked.test/banana-bread"
    assert goto[2] == {"wait_until": "domcontentloaded", "timeout": 20_000}
    # The tools actually ran against the page.
    assert ("press", "Escape") in page.calls
    assert ("wheel", 0, 1200) in page.calls


def test_loop_setup_budget_and_initial_screenshot(store: LocalFileStore) -> None:
    page = FakePage([RECIPE_HTML])
    llm = ScriptedLLM(HAPPY_SCRIPT)

    browse_for_recipe(
        "https://x.test/r",
        llm=llm,
        store=store,
        page_factory=_factory_for(page),  # type: ignore[arg-type]
    )

    assert llm.loop_kwargs is not None
    assert llm.loop_kwargs["feature"] == "extract.browser"
    assert llm.loop_kwargs["max_iterations"] == 15
    assert [tool["name"] for tool in llm.loop_kwargs["tools"]] == [
        "screenshot",
        "click",
        "press_escape",
        "scroll",
        "wait",
        "capture_content",
    ]
    # Opening screenshot + goal text.
    initial = llm.loop_kwargs["initial_content"]
    assert initial[0]["type"] == "image"
    assert initial[0]["source"]["media_type"] == "image/png"
    assert "https://x.test/r" in initial[1]["text"]
    # System prompt covers the goal, obstacles, strategy and the budget.
    system = llm.loop_kwargs["system"]
    for needle in ("recipe", "cookie", "Jump to Recipe", "screenshot", "15 actions"):
        assert needle.lower() in system.lower()


def test_screenshot_tool_returns_image_block(store: LocalFileStore) -> None:
    page = FakePage([RECIPE_HTML])
    llm = ScriptedLLM([("screenshot", {}), ("capture_content", {})])

    browse_for_recipe(
        "https://x.test/r",
        llm=llm,
        store=store,
        page_factory=_factory_for(page),  # type: ignore[arg-type]
    )

    block = llm.results[0]
    assert isinstance(block, list)
    assert block[0]["type"] == "image"
    assert base64.standard_b64decode(block[0]["source"]["data"]) == TINY_PNG


def test_insufficient_capture_keeps_loop_going(store: LocalFileStore) -> None:
    """First capture is an essay → INSUFFICIENT result string; second succeeds."""
    page = FakePage([ARTICLE_HTML, RECIPE_HTML])
    llm = ScriptedLLM([("capture_content", {}), ("capture_content", {})])

    acquired = browse_for_recipe(
        "https://x.test/r",
        llm=llm,
        store=store,
        page_factory=_factory_for(page),  # type: ignore[arg-type]
    )

    assert isinstance(llm.results[0], str)
    assert llm.results[0].startswith("CAPTURED_TEXT_INSUFFICIENT:")
    assert "Why We Bake" in llm.results[0]
    assert acquired.text is not None and "banana" in acquired.text
    assert len(acquired.meta["actions_log"]) == 2


def test_click_prefers_text_then_falls_back_to_css(store: LocalFileStore) -> None:
    page = FakePage([RECIPE_HTML])
    llm = ScriptedLLM([("click", {"target": "Jump to Recipe"}), ("capture_content", {})])
    browse_for_recipe(
        "https://x.test/r",
        llm=llm,
        store=store,
        page_factory=_factory_for(page),  # type: ignore[arg-type]
    )
    assert ("click_text", "Jump to Recipe") in page.calls

    fallback_page = FakePage([RECIPE_HTML], fail_text_click=True)
    llm = ScriptedLLM([("click", {"target": ".jump-to-recipe"}), ("capture_content", {})])
    browse_for_recipe(
        "https://x.test/r",
        llm=llm,  # type: ignore[arg-type]
        store=store,
        page_factory=_factory_for(fallback_page),
    )
    assert ("click_css", ".jump-to-recipe") in fallback_page.calls


def test_wait_is_clamped_to_5000_ms(store: LocalFileStore) -> None:
    page = FakePage([RECIPE_HTML])
    llm = ScriptedLLM([("wait", {"ms": 60_000}), ("capture_content", {})])

    browse_for_recipe(
        "https://x.test/r",
        llm=llm,
        store=store,
        page_factory=_factory_for(page),  # type: ignore[arg-type]
    )

    assert ("wait_for_timeout", 5000) in page.calls


# ---------------------------------------------------------------------------
# Budget exhaustion
# ---------------------------------------------------------------------------


def test_action_budget_exhaustion_fails_with_screenshot_and_log(store: LocalFileStore) -> None:
    page = FakePage([ARTICLE_HTML])
    llm = ScriptedLLM([("screenshot", {}), ("scroll", {"pixels": 800})], exhaust=True)

    with pytest.raises(TierFailed) as exc_info:
        browse_for_recipe(
            "https://x.test/r",
            llm=llm,
            store=store,
            page_factory=_factory_for(page),  # type: ignore[arg-type]
        )

    exc = exc_info.value
    assert "browser budget exhausted" in exc.reason
    assert exc.screenshot_ref is not None
    assert store.open(exc.screenshot_ref) == TINY_PNG
    assert exc.artifacts is not None
    assert exc.artifacts["tier3_screenshot_ref"] == exc.screenshot_ref
    log = json.loads(store.open(exc.artifacts["browser_log_ref"]))
    assert [entry["tool"] for entry in log] == ["screenshot", "scroll"]


def test_model_quitting_without_capture_fails(store: LocalFileStore) -> None:
    page = FakePage([ARTICLE_HTML])
    llm = ScriptedLLM([("screenshot", {})])  # end_turn without capturing

    with pytest.raises(TierFailed) as exc_info:
        browse_for_recipe(
            "https://x.test/r",
            llm=llm,
            store=store,
            page_factory=_factory_for(page),  # type: ignore[arg-type]
        )

    assert "without capturing" in exc_info.value.reason
    assert exc_info.value.screenshot_ref is not None
    assert exc_info.value.artifacts is not None
    assert "browser_log_ref" in exc_info.value.artifacts


def test_wall_clock_budget_exceeded(store: LocalFileStore) -> None:
    page = FakePage([RECIPE_HTML])
    llm = ScriptedLLM([("screenshot", {}), ("scroll", {"pixels": 500}), ("capture_content", {})])
    ticks = iter([0.0, 0.0, 100.0])  # start, first action OK, then 100s elapsed

    def clock() -> float:
        return next(ticks, 100.0)

    with pytest.raises(TierFailed) as exc_info:
        browse_for_recipe(
            "https://x.test/r",
            llm=llm,  # type: ignore[arg-type]
            store=store,
            page_factory=_factory_for(page),
            time_budget_s=90.0,
            clock=clock,
        )

    exc = exc_info.value
    assert "browser budget exhausted" in exc.reason
    assert "time" in exc.reason
    # Only the first action made it into the log.
    log = json.loads(store.open(exc.artifacts["browser_log_ref"]))  # type: ignore[index]
    assert [entry["tool"] for entry in log] == ["screenshot"]


def test_failure_screenshot_save_is_best_effort(store: LocalFileStore) -> None:
    class BrokenScreenshotPage(FakePage):
        def __init__(self) -> None:
            super().__init__([ARTICLE_HTML])
            self._shots = 0

        def screenshot(self, *, type: str = "png") -> bytes:  # noqa: A002
            self._shots += 1
            if self._shots > 1:  # opening screenshot works; final one is broken
                raise RuntimeError("page closed")
            return TINY_PNG

    page = BrokenScreenshotPage()
    llm = ScriptedLLM([("scroll", {"pixels": 100})], exhaust=True)

    with pytest.raises(TierFailed) as exc_info:
        browse_for_recipe(
            "https://x.test/r",
            llm=llm,
            store=store,
            page_factory=_factory_for(page),  # type: ignore[arg-type]
        )

    exc = exc_info.value
    assert exc.screenshot_ref is None  # no screenshot, but the failure is still graceful
    assert exc.artifacts is not None and "browser_log_ref" in exc.artifacts


# ---------------------------------------------------------------------------
# looks_like_recipe_text
# ---------------------------------------------------------------------------


def test_looks_like_recipe_text_true_for_quantity_lines() -> None:
    text = "Banana Bread\n2 cups flour\n1 cup sugar\n3 ripe bananas\nMix and bake."
    assert looks_like_recipe_text(text) is True


def test_looks_like_recipe_text_true_for_bulleted_lines() -> None:
    text = "Ingredients\n- 2 cups flour\n- 1 cup sugar\n- 3 ripe bananas"
    assert looks_like_recipe_text(text) is True


def test_looks_like_recipe_text_true_for_vulgar_fractions() -> None:
    text = "½ cup butter\n¼ tsp salt\n⅓ cup milk"
    assert looks_like_recipe_text(text) is True


def test_looks_like_recipe_text_true_for_hebrew_quantities() -> None:
    text = "שקשוקה\n2 כוסות קמח\n1 כפית מלח\n3 ביצים\nלערבב הכל"
    assert looks_like_recipe_text(text) is True


def test_looks_like_recipe_text_false_for_article() -> None:
    text = (
        "Why We Bake\n"
        "Baking has always been a communal act.\n"
        "Historians trace communal ovens back centuries.\n"
        "1990 was a turning point for home baking.\n"
        "There is no recipe here."
    )
    assert looks_like_recipe_text(text) is False  # one digit-leading line is not enough


def test_looks_like_recipe_text_false_for_empty() -> None:
    assert looks_like_recipe_text("") is False


# ---------------------------------------------------------------------------
# Opt-in real-browser smoke (needs `playwright install chromium`)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not os.environ.get("RN_LIVE_BROWSER_TESTS"),
    reason="set RN_LIVE_BROWSER_TESTS=1 (and run `playwright install chromium`) to run",
)
def test_live_chromium_captures_local_fixture(tmp_path: Path) -> None:
    """Drive a REAL headless Chromium against a file:// fixture and capture it."""
    fixture = tmp_path / "recipe.html"
    fixture.write_text(RECIPE_HTML, encoding="utf-8")
    store = LocalFileStore(tmp_path / "store")
    llm = ScriptedLLM([("screenshot", {}), ("scroll", {"pixels": 800}), ("capture_content", {})])

    acquired = browse_for_recipe(
        fixture.as_uri(),
        llm=llm,  # type: ignore[arg-type]
        store=store,
    )

    assert acquired.text is not None and "all-purpose flour" in acquired.text
    assert acquired.meta["tier_used"] == 3
    assert store.open(acquired.artifacts["tier3_screenshot_ref"])  # real PNG bytes
