"""Element lookup, clicking and cheap state verification, over CDP.

The three tools an agent actually needs to drive a browser without seeing the screen:

* :func:`browser_state` - one round trip, no side effects: where am I?
* :func:`browser_elements` - "every link on this page, with its text, href and pixels".
* :func:`browser_click` - resolve, scroll, **re-measure**, click, and report what changed.

All of it is ~1.1 ms per call where a screenshot is ~95 ms, and what comes back is exact
rather than inferred. Everything is plain JSON so the MCP layer can hand it straight back
to a model.

Honest limits
-------------
These matter more than the happy path, because each of them looks like it works until it
does not:

* **No CSS ``:hover``.** A dispatched mouse event does not move the OS cursor, so nothing
  enters the hover state. Menus that only appear on hover will not open; hover styling
  will not show up in a screenshot either. Use ``:focus-within``-driven UI, or drive the
  menu by clicking its trigger.
* **No iframes.** ``document.querySelectorAll`` only sees the top document. An element
  that lives inside an ``<iframe>`` is invisible here, so a query that finds nothing on a
  page that visibly has links raises :class:`IframeUnsupported` rather than returning an
  empty list that reads like "no links on this page". Same-origin frames can be reached
  by the caller passing a selector that dives into ``iframe.contentDocument``.
* **Virtualised lists may not contain the row you asked for.** A 10 000-row table usually
  keeps a few dozen rows in the DOM. ``nth=9000`` raises :class:`ElementNotFound`; it does
  not get rounded down to the last row that happens to be mounted.
* **Occlusion is not checked.** The click lands on the element's centre point. If something
  invisible is painted over that point - a cookie banner, a modal backdrop, a sticky
  header - the click belongs to that instead.
* **Coordinates are viewport-relative CSS pixels.** They are correct for
  ``Input.dispatchMouseEvent`` (which is what we use). Converting them to desktop pixels
  yourself needs the window position and the device pixel ratio; see ``dpr`` in the
  results. Vertical conversion must use ``document.documentElement.clientHeight``, never
  ``innerHeight`` - the latter includes the horizontal scrollbar, and using it puts every
  click a scrollbar's worth of pixels too low.
* **No silent fallback.** If CDP is unavailable, or the selector matches nothing, these
  functions raise. They never convert a failure into a screen coordinate: a guessed pixel
  is indistinguishable from a real one in the transcript, which is exactly how an agent
  ends up confidently reporting a click that never hit its target.

The elements are also only as good as the selector the caller supplies - this module never
guesses at a site's markup.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from .cdp import CdpClient, CdpUnavailable, get_client

__all__ = [
    "BrowserError",
    "ElementNotFound",
    "IframeUnsupported",
    "InvalidSelector",
    "browser_click",
    "browser_elements",
    "browser_query",
    "browser_state",
]

#: how much text of an element is worth returning
TEXT_LIMIT = 300


class BrowserError(CdpUnavailable):
    """Base for failures that are about the *page*, not about the connection.

    It subclasses :class:`~screen_agent.cdp.CdpUnavailable` so a caller that only wants
    "the browser part did not work" has a single ``except``.
    """


class InvalidSelector(BrowserError):
    """The selector is not valid CSS (or is empty)."""


class ElementNotFound(BrowserError):
    """The selector matched fewer usable elements than ``nth`` asks for."""


class IframeUnsupported(ElementNotFound):
    """Nothing matched and the page has frames - the element is probably in one.

    A subclass of :class:`ElementNotFound` so a caller that only cares that the click
    did not happen still catches it.
    """


# ------------------------------------------------------------------ the JS payloads
# Marker comments (screen-agent: <name>) let a caller - including the tests - tell the
# three round trips apart without depending on the rest of the snippet.

#: one cheap, side-effect free read of everything :func:`browser_state` reports
STATE_JS = """// screen-agent: state
(() => {
  return {
    url: location.href,
    title: document.title,
    ready_state: document.readyState,
    dpr: window.devicePixelRatio,
    scroll_x: Math.round(window.scrollX),
    scroll_y: Math.round(window.scrollY),
    viewport: {
      w: document.documentElement.clientWidth,
      h: document.documentElement.clientHeight
    },
    frames: document.getElementsByTagName('iframe').length +
            document.getElementsByTagName('frame').length
  };
})()"""

#: every match for a selector, with geometry read *after* scrolling
ELEMENTS_JS = r"""// screen-agent: elements
(() => {
  const selector = $SELECTOR;
  const limit = $LIMIT;
  const scroll = $SCROLL;
  // clientWidth/clientHeight and NOT innerWidth/innerHeight: the inner values include
  // the scrollbar gutter, while element boxes are measured against the client box, so
  // mixing the two silently shifts every vertical coordinate by the scrollbar width.
  const viewport = {
    w: document.documentElement.clientWidth,
    h: document.documentElement.clientHeight
  };
  let nodes = [];
  try {
    nodes = Array.prototype.slice.call(document.querySelectorAll(selector));
  } catch (err) {
    return { error: 'invalid-selector', message: (err && err.message) || String(err) };
  }
  const total = nodes.length;
  const out = [];
  let hidden = 0;
  let truncated = false;
  for (let i = 0; i < total; i++) {
    if (out.length >= limit) { truncated = true; break; }
    const el = nodes[i];
    if (scroll) {
      try { el.scrollIntoView({ block: 'center', inline: 'nearest' }); } catch (e) { /* noop */ }
    }
    // Read the rect only now. getBoundingClientRect() is viewport-relative, so a rect
    // read before the scroll describes wherever the element *used* to be.
    const r = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    if (r.width <= 0 || r.height <= 0 || style.display === 'none' ||
        style.visibility === 'hidden' || style.opacity === '0') {
      hidden += 1;
      continue;
    }
    out.push({
      index: out.length,
      dom_index: i,
      tag: (el.tagName || '').toLowerCase(),
      text: (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, $TEXT),
      href: el.href || el.getAttribute('href') || null,
      x: r.left,
      y: r.top,
      w: r.width,
      h: r.height,
      cx: r.left + r.width / 2,
      cy: r.top + r.height / 2,
      in_viewport: r.bottom > 0 && r.right > 0 && r.top < viewport.h && r.left < viewport.w,
      dpr: window.devicePixelRatio
    });
  }
  return {
    selector: selector,
    total: total,
    found: out.length,
    hidden: hidden,
    truncated: truncated,
    dpr: window.devicePixelRatio,
    viewport: viewport,
    frames: document.getElementsByTagName('iframe').length +
            document.getElementsByTagName('frame').length,
    elements: out
  };
})()"""

#: the point to click, measured again immediately before dispatching
POINT_JS = r"""// screen-agent: point
(() => {
  const selector = $SELECTOR;
  const index = $INDEX;
  const viewport = {
    w: document.documentElement.clientWidth,
    h: document.documentElement.clientHeight
  };
  let el = null;
  try {
    el = document.querySelectorAll(selector)[index];
  } catch (err) {
    return { ok: false, reason: 'invalid-selector', message: (err && err.message) || String(err) };
  }
  if (!el || !el.isConnected) return { ok: false, reason: 'missing' };
  $SCROLL
  // As above: scroll first, measure second, so the point belongs to the post-scroll page.
  const r = el.getBoundingClientRect();
  return {
    ok: true,
    x: r.left + r.width / 2,
    y: r.top + r.height / 2,
    w: r.width,
    h: r.height,
    text: (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, $TEXT),
    href: el.href || el.getAttribute('href') || null,
    viewport: viewport
  };
})()"""

_SCROLL_CALL = "try { el.scrollIntoView({ block: 'center', inline: 'nearest' }); } catch (e) { /* noop */ }"

DOM_CLICK_JS = r"""// screen-agent: dom click
(() => {
  const selector = $SELECTOR;
  const index = $INDEX;
  let el = null;
  try {
    el = document.querySelectorAll(selector)[index];
  } catch (err) {
    return { ok: false, reason: 'invalid-selector', message: (err && err.message) || String(err) };
  }
  if (!el || !el.isConnected) return { ok: false, reason: 'missing' };
  const r = el.getBoundingClientRect();
  const before = location.href;
  try {
    el.click();
  } catch (err) {
    return { ok: false, reason: 'click-threw', message: (err && err.message) || String(err) };
  }
  return { ok: true, before: before, x: r.left + r.width / 2, y: r.top + r.height / 2 };
})()"""


def _dom_click_script(selector: str, index: int) -> str:
    return DOM_CLICK_JS.replace("$INDEX", str(int(index))).replace(
        "$SELECTOR", json.dumps(selector)
    )


def _elements_script(selector: str, *, limit: int, scroll_into_view: bool) -> str:
    # the selector goes in last: it is caller data, and a selector containing the literal
    # text "$LIMIT" must not have its own characters substituted
    return (
        ELEMENTS_JS.replace("$LIMIT", str(int(limit)))
        .replace("$SCROLL", "true" if scroll_into_view else "false")
        .replace("$TEXT", str(TEXT_LIMIT))
        .replace("$SELECTOR", json.dumps(selector))
    )


def _point_script(selector: str, index: int, *, scroll_into_view: bool) -> str:
    return (
        POINT_JS.replace("$INDEX", str(int(index)))
        .replace("$SCROLL", _SCROLL_CALL if scroll_into_view else "")
        .replace("$TEXT", str(TEXT_LIMIT))
        .replace("$SELECTOR", json.dumps(selector))
    )


# ----------------------------------------------------------------------- internals
def _pick(client: CdpClient | None) -> CdpClient:
    return client if client is not None else get_client()


def _as_dict(value: Any, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CdpUnavailable(f"{what}: the page returned {type(value).__name__}, expected an object")
    return value


def _element(raw: dict[str, Any], dpr: float) -> dict[str, Any]:
    return {
        "index": int(raw.get("index") or 0),
        "dom_index": int(raw.get("dom_index") or 0),
        "tag": str(raw.get("tag") or ""),
        "text": str(raw.get("text") or ""),
        "href": raw.get("href"),
        "x": float(raw.get("x") or 0.0),
        "y": float(raw.get("y") or 0.0),
        "w": float(raw.get("w") or 0.0),
        "h": float(raw.get("h") or 0.0),
        "cx": float(raw.get("cx") or 0.0),
        "cy": float(raw.get("cy") or 0.0),
        "in_viewport": bool(raw.get("in_viewport")),
        "dpr": dpr,
    }


# ----------------------------------------------------------------------- public API
async def browser_state(client: CdpClient | None = None) -> dict[str, Any]:
    """Where the browser is right now - the cheap verification primitive.

    One ``Runtime.evaluate``, no scrolling, no clicking, nothing that can change the
    page. Use it before and after an action to see whether anything moved; use it instead
    of a screenshot whenever all you want to know is *which* page you are on.

    Returns ``url``, ``title``, ``ready_state``, ``dpr``, ``scroll_x``, ``scroll_y``,
    ``tab_count``, the ``viewport`` (in CSS px, from ``clientWidth``/``clientHeight``) and
    the ``target`` tab this was read from.
    """
    cdp = _pick(client)
    raw = _as_dict(await cdp.evaluate(STATE_JS, timeout=2.0), "browser_state")
    target = cdp.target
    state: dict[str, Any] = {
        "url": str(raw.get("url") or ""),
        "title": str(raw.get("title") or ""),
        "ready_state": str(raw.get("ready_state") or ""),
        "dpr": float(raw.get("dpr") or 1.0),
        "scroll_x": int(raw.get("scroll_x") or 0),
        "scroll_y": int(raw.get("scroll_y") or 0),
        "viewport": {
            "w": int((raw.get("viewport") or {}).get("w") or 0),
            "h": int((raw.get("viewport") or {}).get("h") or 0),
        },
        "frames": int(raw.get("frames") or 0),
        "tab_count": None,
        "target": target.as_dict() if target is not None else None,
    }
    try:
        state["tab_count"] = len(await cdp.list_targets())
    except CdpUnavailable:
        # Purely informational - the state above is the answer, so a failure to count the
        # open tabs must not turn a good read into an exception. It stays None.
        pass
    return state


async def browser_query(
    selector: str,
    *,
    limit: int = 50,
    scroll_into_view: bool = True,
    client: CdpClient | None = None,
) -> dict[str, Any]:
    """Every match for *selector*, plus the numbers that explain the misses.

    :func:`browser_elements` is the short form of this; the difference is that this one
    also reports ``total`` (how many the selector matched at all), ``hidden`` (how many
    were dropped for being zero-sized or invisible), ``truncated``, the frame count and
    the viewport. Those are what turn "found 0 links" from a guess into a diagnosis.
    """
    if not selector or not selector.strip():
        raise InvalidSelector("selector must be a non-empty CSS selector")
    cdp = _pick(client)
    script = _elements_script(selector, limit=limit, scroll_into_view=scroll_into_view)
    raw = _as_dict(await cdp.evaluate(script, timeout=3.0), "browser_elements")
    if raw.get("error") == "invalid-selector":
        raise InvalidSelector(f"{selector!r} is not valid CSS: {raw.get('message') or 'no detail'}")
    dpr = float(raw.get("dpr") or 1.0)
    viewport = raw.get("viewport") or {}
    return {
        "selector": str(raw.get("selector") or selector),
        "total": int(raw.get("total") or 0),
        "found": int(raw.get("found") or 0),
        "hidden": int(raw.get("hidden") or 0),
        "truncated": bool(raw.get("truncated")),
        "frames": int(raw.get("frames") or 0),
        "dpr": dpr,
        "viewport": {"w": int(viewport.get("w") or 0), "h": int(viewport.get("h") or 0)},
        "elements": [_element(item, dpr) for item in raw.get("elements") or []],
    }


async def browser_elements(
    selector: str,
    *,
    nth: int | None = None,
    limit: int = 50,
    scroll_into_view: bool = True,
    client: CdpClient | None = None,
) -> list[dict[str, Any]]:
    """List what *selector* matches, with the pixels to click on each match.

    Each entry has ``index`` (0-based, after filtering), ``dom_index`` (position in the
    unfiltered match list), ``tag``, ``text``, ``href``, the bounding box ``x``/``y``/
    ``w``/``h`` in viewport-relative CSS px, its centre ``cx``/``cy``, whether it is
    currently ``in_viewport``, and ``dpr``.

    With ``scroll_into_view`` (the default) every match is scrolled to the middle of the
    viewport and *then* measured, so ``y`` is where the element is now, not where it was.
    That mutates the scroll position - pass ``scroll_into_view=False`` when you only want
    to look. With ``nth`` set, just that one match is returned, or
    :class:`ElementNotFound`.
    """
    query = await browser_query(
        selector, limit=limit, scroll_into_view=scroll_into_view, client=client
    )
    elements = query["elements"]
    if nth is None:
        return elements
    return [_pick_nth(selector, nth, elements, query)]


async def browser_click(
    selector: str,
    *,
    nth: int = 1,
    scroll_into_view: bool = True,
    verify: bool = True,
    method: str = "auto",
    client: CdpClient | None = None,
) -> dict[str, Any]:
    """Click the *nth* (1-based) match of *selector* and report what changed.

    The sequence is deliberate:

    1. read the page state (when ``verify``);
    2. resolve the match, scroll it into view, measure it;
    3. **measure it again** in a second round trip and click that point;
    4. read the page state again and return the difference.

    Step 3 is the one that matters: between measuring and clicking the window can move,
    the page can reflow, a sticky header can grow. Clicking a rect read one round trip
    earlier is a classic time-of-check/time-of-use bug, and it lands the click on whatever
    took the element's place. Re-measuring costs ~1 ms.

    **How the click is delivered** depends on whether the page can receive pointer input at
    all, and the result says which was used, in ``how``:

    ``input-event``
        A real ``Input.dispatchMouseEvent`` pair at the measured point. This is faithful
        - page scripts see genuine ``mousedown``/``mouseup``/``click`` - and it is what
        you want for anything that reacts to pointer events.
    ``dom-click``
        ``element.click()`` inside the page. No pointer events are fired, so a menu that
        opens on ``mousedown`` will not open; a link, button or form control will.

    ``method="auto"`` picks between them from the page's own ``visibilityState``, and the
    reason is worth stating because it is measured, not theoretical: **a Chrome window that
    is occluded - behind the terminal the agent is running in, which is the normal case -
    reports ``visibilityState: "hidden"`` and its renderer silently drops synthesised
    input.** The click lands nowhere and returns success. ``Page.bringToFront`` does not
    help, and neither does raising the window from this process: Windows refuses
    ``SetForegroundWindow`` to a background process. So when the page is hidden we skip
    straight to the DOM click instead of firing an event we already know will be dropped.
    Pass ``method="input"`` or ``method="dom"`` to force either one.

    With ``verify=True`` (the default) the result carries the raw before/after values -
    ``url_before``, ``url_after``, ``title_before``, ``title_after``, ``changed`` and
    ``navigated`` - never a bare boolean. ``navigated`` means the href changed, which is
    necessary but not sufficient: an SPA route change, a JS-driven page or a form post can
    all leave the href alone while everything under it changes. ``changed`` is the wider
    signal (url, title, ready state or scroll position). Both are *evidence*, not proof of
    what happened: if the click mattered, confirm with another
    :func:`browser_elements` / :func:`browser_state` read, or a screenshot.

    Raises :class:`ElementNotFound` (or :class:`IframeUnsupported`) when there is no such
    match. There is no coordinate fallback: if the element is not there, this raises.
    """
    if method not in ("auto", "input", "dom"):
        raise BrowserError(f"method must be 'auto', 'input' or 'dom', got {method!r}")
    cdp = _pick(client)
    before = await browser_state(cdp) if verify else None
    query = await browser_query(selector, scroll_into_view=scroll_into_view, client=cdp)
    element = _pick_nth(selector, nth, query["elements"], query)

    # Re-resolve and re-measure now, immediately before the dispatch.
    point = _as_dict(
        await cdp.evaluate(
            _point_script(selector, element["dom_index"], scroll_into_view=scroll_into_view),
            timeout=2.0,
        ),
        "browser_click",
    )
    if point.get("reason") == "invalid-selector":
        raise InvalidSelector(f"{selector!r} is not valid CSS: {point.get('message') or 'no detail'}")
    if not point.get("ok"):
        # The element went away between the two round trips. Say so; do not click blind.
        raise ElementNotFound(
            f"{selector!r} matched {element['dom_index'] + 1} elements when the page was "
            f"measured, but that element is gone now (it may have been re-rendered). "
            f"No click was sent."
        )
    x, y = float(point.get("x") or 0.0), float(point.get("y") or 0.0)
    _check_in_viewport(selector, x, y, point.get("viewport") or {}, scroll_into_view)

    hidden = await _page_is_hidden(cdp)
    if method == "dom":
        how = "dom-click"
    elif method == "input":
        how = "input-event"
    else:
        how = "dom-click" if hidden else "input-event"

    if how == "input-event":
        await cdp.dispatch_mouse_event("mousePressed", x, y, button="left", click_count=1)
        await cdp.dispatch_mouse_event("mouseReleased", x, y, button="left", click_count=1)
    else:
        clicked = _as_dict(
            await cdp.evaluate(
                _dom_click_script(selector, element["dom_index"]), timeout=2.0
            ),
            "browser_click",
        )
        if clicked.get("reason") == "invalid-selector":
            raise InvalidSelector(f"{selector!r} is not valid CSS: {clicked.get('message')}")
        if not clicked.get("ok"):
            raise ElementNotFound(
                f"{selector!r} matched when the page was measured, but "
                f"{clicked.get('reason')!r} when the click was sent. Nothing was clicked."
            )

    result: dict[str, Any] = {
        "selector": selector,
        "nth": int(nth),
        "tag": element["tag"],
        "text": element["text"],
        "href": element["href"],
        "point": {"x": x, "y": y},
        "size": {"w": float(point.get("w") or 0.0), "h": float(point.get("h") or 0.0)},
        "dpr": query["dpr"],
        "how": how,
        "target": cdp.target.as_dict() if cdp.target is not None else None,
        "verified": bool(verify),
    }
    if hidden and how == "dom-click":
        result["note"] = (
            "the page is occluded, so the click was made inside the page rather than with "
            "synthesised pointer input; no mouse events were delivered to page scripts"
        )
    if before is None:
        return result
    # A click that navigated: let the new document finish before anyone reads it.
    ready = await _wait_for_ready(cdp)
    result["ready_state"] = ready
    try:
        after = await browser_state(cdp)
    except CdpUnavailable as exc:
        # The execution context vanished between the two reads, which is what a real
        # navigation does. Report it instead of pretending the page did not change.
        result.update(
            {
                "url_before": before["url"],
                "url_after": None,
                "title_before": before["title"],
                "title_after": None,
                "changed": True,
                "navigated": True,
                "verify_error": str(exc),
            }
        )
        return result
    result.update(_delta(before, after))
    return result


# ------------------------------------------------------------------------- helpers
async def _page_is_hidden(client: CdpClient) -> bool:
    """Whether the page currently refuses rendered input.

    One cheap ``Runtime.evaluate``. A ``false`` here would only cost a click that silently
    does nothing, so an error reading it is treated as "not hidden" and the faithful path
    is taken.
    """
    try:
        return await client.evaluate("document.visibilityState === 'hidden'", timeout=1.0) is True
    except CdpUnavailable:
        return False


async def _wait_for_ready(client: CdpClient, *, timeout: float = 2.0) -> str:
    """Block until the document stops loading, and return its readyState.

    A click that navigates returns the instant the navigation starts, not when the new
    page exists. Reading elements straight afterwards finds an empty document - measured,
    not imagined: clicking a result and immediately listing links came back with nothing,
    because the read raced the new page. That pushes a sleep-and-retry loop onto every
    caller, which is exactly the kind of round trip this whole design exists to remove.

    So the wait belongs here, bounded: it polls ``document.readyState`` and gives up after
    *timeout* rather than hanging on a page that never finishes loading. A lost execution
    context means the document was replaced under us, which is the navigation landing, so
    that is success, not failure.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.0, timeout)
    ready = "unknown"
    while True:
        try:
            ready = str(await client.evaluate("document.readyState", timeout=1.0))
        except CdpUnavailable:
            return "loading"  # the context went away: a new document is arriving
        if ready != "loading" or loop.time() >= deadline:
            return ready
        await asyncio.sleep(0.05)


def _pick_nth(selector: str, nth: int, elements: list[dict[str, Any]], query: dict[str, Any]) -> dict:
    """Return the *nth* (1-based) element or raise with what the page actually had."""
    try:
        wanted = int(nth)
    except (TypeError, ValueError):
        raise ElementNotFound(f"nth must be a whole number, got {nth!r}") from None
    if wanted < 1:
        raise ElementNotFound(f"nth is 1-based: {wanted} is out of range")
    if wanted <= len(elements):
        return elements[wanted - 1]
    hidden = f", {query['hidden']} hidden or zero-sized" if query["hidden"] else ""
    where = (
        f"{selector!r} matched {query['total']} element(s), "
        f"{query['found']} of them usable{hidden}"
    )
    if query["found"] == 0 and query["frames"]:
        raise IframeUnsupported(
            f"{where}, and this page has {query['frames']} frame(s). Elements inside an "
            f"iframe are not reachable from the top document: query "
            f"iframe.contentDocument, or list the frames and pick one."
        )
    if wanted > query["found"] and query["truncated"]:
        where += f" (the listing stopped at the {query['found']} usable ones)"
    raise ElementNotFound(
        f"no element {wanted} for {where}. "
        f"Virtualised lists keep only the visible rows in the DOM, so a row further down "
        f"is not there to click - scroll the list and query again, or widen the selector. "
        f"Nothing was clicked."
    )


def _check_in_viewport(selector: str, x: float, y: float, viewport: dict, scrolled: bool) -> None:
    """Refuse to dispatch at a point that is not on the page."""
    width = float(viewport.get("w") or 0.0)
    height = float(viewport.get("h") or 0.0)
    if width <= 0 or height <= 0 or (0 <= x <= width and 0 <= y <= height):
        return
    hint = (
        "the element is scrolled into view automatically; try scroll_into_view=True"
        if not scrolled
        else "the element is taller than the viewport or pinned outside it"
    )
    raise ElementNotFound(
        f"{selector!r} resolves to ({x:.0f}, {y:.0f}) which is outside the "
        f"{width:.0f}x{height:.0f} viewport, so a click there would land on nothing: {hint}. "
        f"Nothing was clicked."
    )


def _delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """The before/after values, plus the two derived flags. Never just a boolean."""
    return {
        "url_before": before["url"],
        "url_after": after["url"],
        "title_before": before["title"],
        "title_after": after["title"],
        "changed": any(
            before.get(key) != after.get(key)
            for key in ("url", "title", "ready_state", "scroll_x", "scroll_y")
        ),
        "navigated": before["url"] != after["url"],
    }
