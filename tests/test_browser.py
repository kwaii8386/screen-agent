"""Unit tests for the browser tools, against a fake CDP client.

There is no browser, no socket and no mouse anywhere in this file. :class:`FakeClient`
answers the three round trips the tools make - state, elements, point - and records every
mouse event, so the tests can assert exactly what would have been sent to a page.

What is pinned here is the part that produces confidently wrong actions:

* the geometry comes back in the shape the caller is told to expect;
* the rect is read *after* scrolling, and read *again* right before the click (a rect
  cached from one round trip earlier is a time-of-check/time-of-use bug);
* the click is one matched ``mousePressed``/``mouseReleased`` pair;
* verification reports the raw before/after values, not a bare boolean;
* a missing element raises instead of turning into a screen coordinate, and when CDP is
  unavailable **no** desktop input backend is touched.
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from screen_agent import backends, browser  # noqa: E402
from screen_agent.browser import (  # noqa: E402
    BrowserError,
    ElementNotFound,
    IframeUnsupported,
    InvalidSelector,
    browser_click,
    browser_elements,
    browser_query,
    browser_state,
)
from screen_agent.cdp import CdpUnavailable, Target, no_endpoint_help  # noqa: E402

SELECTOR = "a[href]"


def state(**over: Any) -> dict[str, Any]:
    payload = {
        "url": "https://example.test/docs",
        "title": "Docs",
        "ready_state": "complete",
        "dpr": 1.5,
        "scroll_x": 0,
        "scroll_y": 240,
        "viewport": {"w": 1280, "h": 720},
        "frames": 0,
    }
    payload.update(over)
    return payload


def raw_element(index: int = 0, **over: Any) -> dict[str, Any]:
    payload = {
        "index": index,
        "dom_index": index,
        "tag": "a",
        "text": f"Link {index}",
        "href": f"https://example.test/p{index}",
        "x": 10.0 + index,
        "y": 100.0 + index,
        "w": 120.0,
        "h": 32.0,
        "cx": 70.0 + index,
        "cy": 116.0 + index,
        "in_viewport": True,
    }
    payload.update(over)
    return payload


def elements(*items: dict[str, Any], **over: Any) -> dict[str, Any]:
    found = list(items)
    payload = {
        "selector": SELECTOR,
        "total": len(found),
        "found": len(found),
        "hidden": 0,
        "truncated": False,
        "frames": 0,
        "dpr": 1.5,
        "viewport": {"w": 1280, "h": 720},
        "elements": found,
    }
    payload.update(over)
    return payload


def point(x: float = 70.0, y: float = 116.0, **over: Any) -> dict[str, Any]:
    payload = {
        "ok": True,
        "x": x,
        "y": y,
        "w": 120.0,
        "h": 32.0,
        "text": "Link 0",
        "href": "https://example.test/p0",
        "viewport": {"w": 1280, "h": 720},
    }
    payload.update(over)
    return payload


class FakeClient:
    """Answers the three round trips and records every mouse event."""

    def __init__(
        self,
        *,
        state_payload: Any = None,
        elements_payload: Any = None,
        point_payload: Any = None,
        targets: list[Target] | None = None,
        error: Exception | None = None,
        hidden: bool = False,
    ) -> None:
        self.state = state_payload if state_payload is not None else state()
        self.elements = elements_payload if elements_payload is not None else elements(raw_element())
        self.point = point_payload if point_payload is not None else point()
        self.targets = targets if targets is not None else [
            Target(id="T1", type="page", title="Docs", url="https://example.test/docs")
        ]
        self.error = error
        self.expressions: list[str] = []
        self.mouse: list[dict[str, Any]] = []
        self.dom_clicks: list[str] = []
        self.hidden = hidden
        self.ready_state = "complete"
        self.list_calls = 0
        self.target = self.targets[0] if self.targets else None

    def _next(self, payload: Any) -> Any:
        if isinstance(payload, list):  # a queue of answers, one per round trip
            return payload.pop(0) if len(payload) > 1 else payload[0]
        return payload

    async def evaluate(self, expression: str, *, timeout: float = 2.0) -> Any:
        self.expressions.append(expression)
        if self.error is not None:
            raise self.error
        if "screen-agent: state" in expression:
            return copy.deepcopy(self._next(self.state))
        if "screen-agent: elements" in expression:
            return copy.deepcopy(self._next(self.elements))
        if "screen-agent: point" in expression:
            return copy.deepcopy(self._next(self.point))
        if "screen-agent: dom click" in expression:
            self.dom_clicks.append(expression)
            return {"ok": True, "before": "https://example.test/docs", "x": 1.0, "y": 2.0}
        if "readyState" in expression:
            return self.ready_state
        if "visibilityState" in expression:
            return self.hidden
        raise AssertionError(f"unexpected expression: {expression[:100]}")  # pragma: no cover

    async def dispatch_mouse_event(
        self,
        event: str,
        x: float,
        y: float,
        *,
        button: str = "left",
        click_count: int = 1,
        timeout: float = 2.0,
    ) -> None:
        self.mouse.append(
            {"event": event, "x": x, "y": y, "button": button, "clickCount": click_count}
        )

    async def list_targets(self) -> list[Target]:
        self.list_calls += 1
        if self.error is not None:
            raise self.error
        return list(self.targets)

    async def close(self) -> None:
        return None

    # -- helpers ------------------------------------------------------------
    def expression_for(self, marker: str) -> str:
        for expression in self.expressions:
            if f"screen-agent: {marker}" in expression:
                return expression
        raise AssertionError(f"no {marker} expression was sent")


# -------------------------------------------------------------------------- state
def test_browser_state_reports_the_page_and_the_tab_it_came_from():
    client = FakeClient(targets=[
        Target(id="T1", type="page", title="Docs", url="https://example.test/docs"),
        Target(id="T2", type="page", title="Other", url="https://other.test/"),
    ])
    result = asyncio.run(browser_state(client))
    assert result["url"] == "https://example.test/docs"
    assert result["title"] == "Docs"
    assert result["ready_state"] == "complete"
    assert result["dpr"] == 1.5
    assert result["scroll_x"] == 0
    assert result["scroll_y"] == 240
    assert result["tab_count"] == 2
    assert result["viewport"] == {"w": 1280, "h": 720}
    assert result["target"] == {
        "id": "T1",
        "type": "page",
        "title": "Docs",
        "url": "https://example.test/docs",
    }


def test_browser_state_is_one_evaluate_and_changes_nothing():
    client = FakeClient()
    asyncio.run(browser_state(client))
    assert len(client.expressions) == 1
    assert "screen-agent: state" in client.expressions[0]
    assert client.mouse == []


def test_browser_state_survives_a_dead_target_list():
    class HalfDead(FakeClient):
        async def list_targets(self) -> list[Target]:
            raise CdpUnavailable("no endpoint")

    result = asyncio.run(browser_state(HalfDead()))
    assert result["tab_count"] is None  # informational only, never fatal
    assert result["title"] == "Docs"


# ----------------------------------------------------------------- the JS contract
def test_the_js_measures_against_the_client_box_and_never_the_window():
    elements_script = browser._elements_script(SELECTOR, limit=50, scroll_into_view=True)
    point_script = browser._point_script(SELECTOR, 0, scroll_into_view=True)
    for script in (browser.STATE_JS, elements_script, point_script):
        assert "documentElement.clientWidth" in script
        assert "documentElement.clientHeight" in script
        code = "\n".join(
            line for line in script.splitlines() if not line.strip().startswith("//")
        )
        # the inner* values include the scrollbar gutter; using them with a bounding rect
        # moves every vertical coordinate by the width of the scrollbar
        assert "innerWidth" not in code
        assert "innerHeight" not in code


def test_the_js_scrolls_into_view_before_it_measures():
    elements_script = browser._elements_script(SELECTOR, limit=50, scroll_into_view=True)
    point_script = browser._point_script(SELECTOR, 0, scroll_into_view=True)
    assert elements_script.index("el.scrollIntoView(") < elements_script.index(
        "el.getBoundingClientRect()"
    )
    assert point_script.index("el.scrollIntoView(") < point_script.index("el.getBoundingClientRect()")


def test_scroll_into_view_false_does_not_scroll():
    # the elements snippet keeps the call but guards it at runtime; the point snippet is
    # rebuilt without it, so there is no way to scroll even by accident
    script = browser._elements_script(SELECTOR, limit=50, scroll_into_view=False)
    assert "const scroll = false" in script
    assert script.index("if (scroll)") < script.index("el.scrollIntoView(")
    assert script.index("el.scrollIntoView(") < script.index("el.getBoundingClientRect()")
    assert "scrollIntoView" not in browser._point_script(SELECTOR, 0, scroll_into_view=False)


def test_the_caller_selector_is_used_verbatim():
    weird = "nav > ul li:nth-child(3) > a[href^='/docs']"
    script = browser._elements_script(weird, limit=5, scroll_into_view=True)
    assert f"const selector = {json.dumps(weird)}" in script
    # a selector full of quotes cannot break out of the JS string literal
    assert 'const selector = "a[title=\\"x\\"]"' in browser._elements_script(
        'a[title="x"]', limit=5, scroll_into_view=True
    )
    # and a selector that happens to contain a placeholder is not corrupted by it
    assert 'const selector = "a[data-x=\\"$TEXT\\"]"' in browser._elements_script(
        'a[data-x="$TEXT"]', limit=5, scroll_into_view=True
    )


# ---------------------------------------------------------------------- elements
def test_elements_are_parsed_into_plain_data():
    client = FakeClient(elements_payload=elements(
        raw_element(0, text="Home", href="https://example.test/"),
        raw_element(1, tag="button", text="Sign in", href=None),
    ))
    result = asyncio.run(browser_elements(SELECTOR, client=client))
    assert [e["text"] for e in result] == ["Home", "Sign in"]
    assert result[0]["href"] == "https://example.test/"
    assert result[1]["href"] is None
    assert result[0]["x"] == 10.0 and result[0]["y"] == 100.0
    assert result[0]["w"] == 120.0 and result[0]["h"] == 32.0
    assert (result[0]["cx"], result[0]["cy"]) == (70.0, 116.0)
    assert result[0]["in_viewport"] is True
    assert all(e["dpr"] == 1.5 for e in result)  # css px, with the ratio to undo
    assert all(isinstance(e["index"], int) for e in result)
    assert client.mouse == []  # looking is not touching


def test_elements_nth_selects_one_element():
    client = FakeClient(elements_payload=elements(
        raw_element(0), raw_element(1), raw_element(2)
    ))
    only = asyncio.run(browser_elements(SELECTOR, nth=2, client=client))
    assert len(only) == 1
    assert only[0]["index"] == 1


def test_query_reports_the_misses_so_an_empty_result_can_be_diagnosed():
    client = FakeClient(elements_payload=elements(hidden=12, total=12, found=0, frames=2))
    result = asyncio.run(browser_query(SELECTOR, client=client))
    assert result["found"] == 0
    assert result["total"] == 12
    assert result["hidden"] == 12
    assert result["frames"] == 2
    assert result["elements"] == []


def test_an_invalid_selector_raises_instead_of_returning_nothing():
    with pytest.raises(InvalidSelector):
        asyncio.run(browser_elements("   ", client=FakeClient()))
    with pytest.raises(InvalidSelector) as caught:
        asyncio.run(browser_elements("a[[[", client=FakeClient(elements_payload={
            "error": "invalid-selector", "message": "SyntaxError"
        })))
    assert "SyntaxError" in str(caught.value)


def test_hidden_and_zero_sized_elements_are_filtered_by_the_js():
    client = FakeClient(elements_payload=elements(raw_element(0), raw_element(1, dom_index=4)))
    result = asyncio.run(browser_elements(SELECTOR, client=client))
    script = client.expression_for("elements")
    assert "r.width <= 0 || r.height <= 0" in script
    assert "style.visibility === 'hidden'" in script
    # a filtered element is gone from the listing but keeps its place in the DOM order
    assert [e["dom_index"] for e in result] == [0, 4]


# ------------------------------------------------------------------------- click
def test_click_sends_exactly_one_matched_press_and_release():
    client = FakeClient()
    asyncio.run(browser_click(SELECTOR, client=client))
    assert [m["event"] for m in client.mouse] == ["mousePressed", "mouseReleased"]
    assert {(m["x"], m["y"]) for m in client.mouse} == {(70.0, 116.0)}
    assert all(m["button"] == "left" and m["clickCount"] == 1 for m in client.mouse)


def test_click_dispatches_at_the_re_read_rect_not_the_first_measurement():
    # the page moved between the two round trips: the listing is stale, the point is not
    client = FakeClient(
        elements_payload=elements(raw_element(0, cx=70.0, cy=116.0)),
        point_payload=point(903.0, 411.0),
    )
    result = asyncio.run(browser_click(SELECTOR, client=client))
    assert [m["x"] for m in client.mouse] == [903.0, 903.0]
    assert result["point"] == {"x": 903.0, "y": 411.0}
    # state, elements, point, visibilityState, readyState, state - the point is measured,
    # then clicked, then the new document is waited for before it is read back
    assert len(client.expressions) == 6
    assert "screen-agent: point" in client.expressions[2]


def test_click_re_reads_and_scrolls_in_both_round_trips():
    client = FakeClient()
    asyncio.run(browser_click(SELECTOR, scroll_into_view=True, client=client))
    assert "el.scrollIntoView(" in client.expression_for("elements")
    assert "el.scrollIntoView(" in client.expression_for("point")
    assert "el.getBoundingClientRect()" in client.expression_for("point")


def test_click_without_scrolling_asks_for_no_scroll():
    client = FakeClient()
    asyncio.run(browser_click(SELECTOR, scroll_into_view=False, client=client))
    assert "scrollIntoView" not in client.expression_for("point")
    assert client.mouse  # the click still happened


def test_click_verification_returns_the_raw_before_and_after_values():
    client = FakeClient(state_payload=[
        state(url="https://example.test/docs", title="Docs"),
        state(url="https://example.test/login", title="Sign in", ready_state="loading"),
    ])
    result = asyncio.run(browser_click(SELECTOR, client=client))
    assert result["url_before"] == "https://example.test/docs"
    assert result["url_after"] == "https://example.test/login"
    assert result["title_before"] == "Docs"
    assert result["title_after"] == "Sign in"
    assert result["changed"] is True
    assert result["navigated"] is True
    assert result["href"] == "https://example.test/p0"
    assert result["verified"] is True


def test_clicking_again_does_not_claim_navigation_when_the_href_is_unchanged():
    client = FakeClient(state_payload=[state(), state()])
    result = asyncio.run(browser_click(SELECTOR, client=client))
    assert result["url_before"] == result["url_after"] == "https://example.test/docs"
    assert result["title_before"] == result["title_after"] == "Docs"
    assert result["navigated"] is False
    assert result["changed"] is False


def test_a_same_href_dom_change_is_reported_as_changed_but_not_navigated():
    client = FakeClient(state_payload=[state(), state(title="Docs (updated)")])
    result = asyncio.run(browser_click(SELECTOR, client=client))
    assert result["navigated"] is False
    assert result["changed"] is True


def test_verification_can_be_turned_off():
    client = FakeClient()
    result = asyncio.run(browser_click(SELECTOR, verify=False, client=client))
    assert result["verified"] is False
    assert "url_before" not in result
    # elements, point, and the one visibilityState read that picks how to click
    assert len([e for e in client.expressions if "screen-agent: state" not in e]) == 3
    assert len(client.mouse) == 2


def test_a_lost_execution_context_is_reported_rather_than_pretended_away():
    class Navigating(FakeClient):
        async def evaluate(self, expression: str, *, timeout: float = 2.0) -> Any:
            if "screen-agent: state" in expression and len(self.expressions):
                raise CdpUnavailable("Cannot find context with specified id")
            return await super().evaluate(expression, timeout=timeout)

    result = asyncio.run(browser_click(SELECTOR, client=Navigating()))
    assert result["url_before"] == "https://example.test/docs"
    assert result["url_after"] is None
    assert result["navigated"] is True
    assert "context" in result["verify_error"]


# ------------------------------------------------- an occluded page takes the click
def test_an_occluded_page_is_clicked_inside_the_page_not_with_a_synthesised_event():
    """A window behind another window drops CDP mouse events, silently and with a
    success response. This is the ordinary case - the agent's own terminal is in front -
    so 'auto' must not waste a click on it."""
    client = FakeClient(hidden=True)
    result = asyncio.run(browser_click(SELECTOR, client=client))
    assert client.mouse == []  # no synthesised event was sent into the void
    assert len(client.dom_clicks) == 1
    assert result["how"] == "dom-click"
    assert "occluded" in result["note"]


def test_a_visible_page_still_gets_a_real_mouse_event():
    client = FakeClient(hidden=False)
    result = asyncio.run(browser_click(SELECTOR, client=client))
    assert [m["event"] for m in client.mouse] == ["mousePressed", "mouseReleased"]
    assert client.dom_clicks == []
    assert result["how"] == "input-event"
    assert "note" not in result


def test_the_method_can_be_forced_both_ways():
    forced_dom = FakeClient(hidden=False)
    assert asyncio.run(browser_click(SELECTOR, method="dom", client=forced_dom))["how"] == (
        "dom-click"
    )
    assert forced_dom.mouse == []

    forced_input = FakeClient(hidden=True)
    assert asyncio.run(browser_click(SELECTOR, method="input", client=forced_input))["how"] == (
        "input-event"
    )
    assert len(forced_input.mouse) == 2


def test_an_unknown_method_is_refused():
    with pytest.raises(BrowserError) as caught:
        asyncio.run(browser_click(SELECTOR, method="teleport", client=FakeClient()))
    assert "teleport" in str(caught.value)


def test_a_dom_click_that_finds_nothing_raises_rather_than_pretending():
    class Gone(FakeClient):
        async def evaluate(self, expression: str, *, timeout: float = 2.0) -> Any:
            if "screen-agent: dom click" in expression:
                self.expressions.append(expression)
                return {"ok": False, "reason": "missing"}
            return await super().evaluate(expression, timeout=timeout)

    client = Gone(hidden=True)
    with pytest.raises(ElementNotFound) as caught:
        asyncio.run(browser_click(SELECTOR, client=client))
    assert "Nothing was clicked" in str(caught.value)
    assert client.mouse == []


# --------------------------------------------------------------- click failures
def test_clicking_a_missing_nth_raises_and_sends_no_mouse_event():
    client = FakeClient(elements_payload=elements(raw_element(0), raw_element(1)))
    with pytest.raises(ElementNotFound) as caught:
        asyncio.run(browser_click(SELECTOR, nth=7, client=client))
    assert client.mouse == []
    message = str(caught.value)
    assert "no element 7" in message
    assert "matched 2" in message
    assert "Nothing was clicked" in message


def test_clicking_a_row_a_virtualised_list_never_rendered_raises():
    client = FakeClient(elements_payload=elements(raw_element(0), total=10000, truncated=True))
    with pytest.raises(ElementNotFound) as caught:
        asyncio.run(browser_click(SELECTOR, nth=9000, client=client))
    assert "Virtualised" in str(caught.value)
    assert client.mouse == []


def test_a_page_with_frames_says_so_rather_than_returning_nothing():
    client = FakeClient(elements_payload=elements(total=0, found=0, frames=3))
    with pytest.raises(IframeUnsupported) as caught:
        asyncio.run(browser_click("a[href]", client=client))
    message = str(caught.value)
    assert "iframe" in message
    assert "3 frame" in message
    assert client.mouse == []
    # it is still an ElementNotFound, so one except clause catches both
    assert isinstance(caught.value, ElementNotFound)


def test_an_element_that_vanishes_between_the_two_reads_is_not_clicked_blind():
    client = FakeClient(point_payload={"ok": False, "reason": "missing"})
    with pytest.raises(ElementNotFound) as caught:
        asyncio.run(browser_click(SELECTOR, client=client))
    assert client.mouse == []
    assert "re-rendered" in str(caught.value)


def test_a_point_outside_the_viewport_is_refused_instead_of_clicked():
    client = FakeClient(
        point_payload=point(5000.0, 411.0, viewport={"w": 1280, "h": 720})
    )
    with pytest.raises(ElementNotFound) as caught:
        asyncio.run(browser_click(SELECTOR, scroll_into_view=False, client=client))
    assert "outside" in str(caught.value)
    assert "scroll_into_view=True" in str(caught.value)
    assert client.mouse == []


# ------------------------------------------------------------------- no fallback
def test_no_input_injection_fallback_when_cdp_is_unavailable(monkeypatch):
    """The whole point: a dead CDP endpoint must never become a click on the desktop."""
    injected: list[tuple] = []

    class Tripwire(backends.Backend):
        name = "tripwire"

        async def click(self, x=None, y=None, button="left", clicks=1):
            injected.append(("click", x, y))
            raise AssertionError("input was injected into the real desktop")

        async def move(self, x, y, cursor="default"):
            injected.append(("move", x, y))
            raise AssertionError("input was injected into the real desktop")

        async def type_text(self, text, interval=0.008):
            injected.append(("type", text))
            raise AssertionError("input was injected into the real desktop")

        async def key_press(self, combo):
            injected.append(("key", combo))
            raise AssertionError("input was injected into the real desktop")

    monkeypatch.setattr(backends, "load_backend", lambda *a, **k: Tripwire())
    dead = FakeClient(error=CdpUnavailable(no_endpoint_help(9222)))

    with pytest.raises(CdpUnavailable) as caught:
        asyncio.run(browser_click(SELECTOR, client=dead))
    assert "--remote-debugging-port=9222" in str(caught.value)
    assert "--user-data-dir" in str(caught.value)
    assert injected == []
    assert dead.mouse == []

    for call in (browser_state, browser_elements, browser_query):
        with pytest.raises(CdpUnavailable):
            asyncio.run(call(SELECTOR, client=dead) if call is not browser_state
                        else browser_state(dead))
    assert injected == []


def test_the_browser_module_never_imports_the_desktop_backends():
    source = inspect.getsource(browser)
    assert "backends" not in source
    assert "mouse_click" not in source
    assert "type_text" not in source
    assert "key_press" not in source
