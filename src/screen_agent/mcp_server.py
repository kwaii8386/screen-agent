"""MCP surface: the tools an AI can call to see and control the desktop."""

from __future__ import annotations

import functools
import inspect
import json
import time
from pathlib import Path
from typing import Any

from . import __version__
from .backends import Backend, load_backend
from .control import run_actions
from .core import (
    ReportedError,
    UnsupportedOperation,
    default_shot_dir,
    parse_region,
    scale_image,
)

__all__ = ["build_server", "run_stdio"]

INSTRUCTIONS = """\
Control the local desktop: see what is on screen, click it, type at it, and read the \
structure of a web page without guessing pixels.

Cheapest perception first, because a screenshot is the expensive way to learn something \
that is usually already known exactly:
  1. screen_state()  -> window titles and rectangles, costs microseconds, no image.
  2. page_state() / page_find(selector) -> exact text, href and geometry of a \
link or button in a Chromium browser, also microseconds. Prefer this over a screenshot \
for anything inside a web page.
  3. screenshot()  -> only when you genuinely need to see pixels. Returns a file path; \
read that path to actually look at it.
  4. act           -> page_click(selector, nth) for page elements, otherwise \
mouse_click / type_text / key_press / mouse_drag / mouse_scroll.
  5. verify        -> page_state() or screen_state() to confirm what changed. Far \
cheaper than a screenshot, and after a click it says whether the page actually moved.

Coordinates are absolute pixels with (0,0) at the top-left of the desktop, as reported by \
screen_info(). region is either 'full' or 'x,y,w,h'.

The page_* tools need a Chromium browser (Chrome, Edge, CocCoc) started with \
--remote-debugging-port=9222 and its own --user-data-dir. If none is listening they raise \
an error naming those flags; they never fall back to guessing a coordinate, because a \
guessed click that lands on the wrong link is worse than an error.

They are the screen-agent tools above, and nothing else does this job. If you have another \
browser tool from another source in your tool list, it is a different thing with a \
different connection; do not consult it, and do not conclude from it that these are \
broken. page_state answers "which page am I on" on its own, and if it raises, the answer \
is in the error it raises.

This is the user's real, live machine: mouse_click, type_text, key_press and mouse_drag \
change their session, so only perform actions the user asked for, prefer screenshotting \
before acting, and verify afterwards.\
"""


def _tool_error_type() -> type[Exception]:
    """The SDK's "this failure was deliberate" exception, for either major version.

    Resolved at build time rather than at import so that this module still loads when
    the lookup fails for an SDK that moved it again - the alternative is refusing to
    start, which is worse than losing the message.
    """
    for module_name in (
        "mcp.server.mcpserver.exceptions",  # mcp >= 2.0
        "mcp.server.mcpserver.tools.base",
        "mcp.server.fastmcp.exceptions",  # mcp 1.x
    ):
        try:
            found = __import__(module_name, fromlist=["ToolError"]).ToolError
        except Exception:
            continue
        if isinstance(found, type) and issubclass(found, Exception):
            return found
    return RuntimeError


def _takes(func: Any, name: str) -> bool:
    """Whether *func* accepts a keyword argument called *name*.

    Used to decide if a backend supports the capture cache, instead of calling it and
    catching ``TypeError``: a genuine ``TypeError`` from inside a backend's capture would
    be swallowed by such a handler and the screen captured a second time, hiding a real
    bug behind a successful-looking result.
    """
    try:
        return name in inspect.signature(func).parameters
    except (TypeError, ValueError):  # a C callable or a mock
        return True


def _json(value: Any) -> str:
    """Pretty-print a tool result.

    The structured tools return JSON rather than prose on purpose: the caller is a model
    that has to act on the exact value, and a sentence that says "the url changed from X
    to Y" is one more thing to mis-parse than two quoted strings.
    """
    return json.dumps(value, indent=2, default=str)


def _server_class() -> tuple[type, bool]:
    """The MCP server class, for either major version of the ``mcp`` SDK.

    ``mcp`` 2.x renamed ``FastMCP`` to ``MCPServer`` and added a ``version`` argument;
    1.x only has ``FastMCP(name, instructions)``. Both are supported, so the package
    works with whichever one is installed instead of failing at import time.
    """
    try:  # mcp >= 2.0
        from mcp.server.mcpserver import MCPServer  # noqa: PLC0415

        return MCPServer, True
    except ImportError:  # pragma: no cover - depends on the installed mcp
        from mcp.server.fastmcp import FastMCP  # noqa: PLC0415

        return FastMCP, False


def build_server(backend: Backend | None = None) -> Any:
    """Create the MCP server, bound to *backend* (or an auto-detected one)."""
    server_class, takes_version = _server_class()
    kwargs: dict[str, Any] = {"name": "screen-agent", "instructions": INSTRUCTIONS}
    if takes_version and "version" in inspect.signature(server_class).parameters:
        kwargs["version"] = __version__
    mcp = server_class(**kwargs)
    state: dict[str, Backend | None] = {"backend": backend}

    tool_error = _tool_error_type()

    def tool():
        """Register a tool, translating deliberate errors on the way out.

        The SDK decides what the model sees by catching its own ``ToolError``: anything
        else is treated as a crash and its message is dropped on the server, leaving the
        model with "Error executing tool <name>" and nothing to act on. Every tool goes
        through here so that a message written for the model actually reaches it - see
        ``core.ReportedError`` for the measurement that forced this.
        """

        def register(func):
            @functools.wraps(func)
            async def wrapper(*args, **kwargs):
                try:
                    return await func(*args, **kwargs)
                except ReportedError as exc:
                    raise tool_error(f"{func.__name__}: {exc}") from exc

            return mcp.tool()(wrapper)

        return register

    def be() -> Backend:
        if state["backend"] is None:
            state["backend"] = load_backend()
        return state["backend"]

    def shot_path(path: str, suffix: str = ".png") -> Path:
        if path:
            p = Path(path).expanduser()
            if not p.is_absolute():
                p = Path.cwd() / p
            p.parent.mkdir(parents=True, exist_ok=True)
            if not p.suffix:
                p = p.with_suffix(suffix)
            return p
        d = default_shot_dir()
        d.mkdir(parents=True, exist_ok=True)
        stamp = f"{time.strftime('%Y%m%d-%H%M%S')}-{int(time.time() * 1000) % 1000}"
        return d / f"{stamp}{suffix}"

    @tool()
    async def screen_info() -> str:
        """Describe the desktop: size, backend in use and what each capability supports."""
        info = await be().info()
        return info.render() + (
            "\nscreenshot dir : "
            + str(default_shot_dir())
            + "\ntips           : call screenshot(), then read the returned path to see it"
        )

    @tool()
    async def screenshot(
        path: str = "",
        region: str = "full",
        cursor: bool = True,
        scale: int = 0,
        stable: bool = False,
        stable_timeout_ms: int = 1000,
        max_age_ms: int = 0,
    ) -> str:
        """Capture the screen (or a region) to a PNG and return the file path.

        region: 'full' or 'x,y,w,h' in desktop pixels. A region costs a fraction of a
        full capture, so crop to what you actually need.
        cursor: include the mouse pointer (only on some backends).
        scale: downscale so the widest side is at most this many pixels (0 = keep native).
        On Windows the shrink happens inside GDI while the pixels are copied, so the
        full-size frame is never encoded at all - 3x faster than resizing afterwards.
        stable: keep re-capturing until two consecutive frames are identical, so you do
        not photograph a window that is still repainting. Returns a frame either way and
        says whether it settled.
        max_age_ms: reuse the previous capture if it is younger than this. 0 (the
        default) always grabs fresh. Every action already invalidates the cache, so a
        frame is never served across a click - but a returned frame may still be a
        little old, which the message reports.
        """
        if stable and max_age_ms:
            raise UnsupportedOperation("stable and max_age_ms ask for opposite things; pass one")
        backend = be()
        rect = parse_region(region, backend.desktop)
        target = shot_path(path)
        notes: list[str] = []
        if stable:
            capture_stable = getattr(backend, "capture_stable", None)
            if capture_stable is None:
                raise UnsupportedOperation(
                    f"{backend.name}: this backend cannot wait for a stable frame"
                )
            width, height, settled = await capture_stable(
                target,
                rect,
                cursor=cursor,
                timeout_ms=int(stable_timeout_ms),
            )
            if not settled:
                notes.append(
                    f"screen still changing after {int(stable_timeout_ms)}ms; "
                    "the frame may be mid-repaint"
                )
            if scale and int(scale) > 0:
                # capture_stable has no max_width, so honouring scale here means
                # rescaling the settled file - and saying so, rather than dropping the
                # argument the caller passed and reporting a size they did not ask for.
                width, height = scale_image(target, int(scale))
                notes.append("scaled after capture: stable captures do not downscale")
        else:
            capture_scaled = getattr(backend, "capture_scaled", None)
            if capture_scaled is None:  # pragma: no cover - every backend has it
                width, height = await backend.capture_png(target, rect, cursor=cursor)
                if scale and int(scale) > 0:
                    width, height = scale_image(target, int(scale))
            else:
                kwargs: dict[str, Any] = {"cursor": cursor}
                if scale and int(scale) > 0:
                    kwargs["max_width"] = int(scale)
                if max_age_ms and _takes(backend.capture_scaled, "max_age_ms"):
                    width, height = await capture_scaled(
                        target, rect, max_age_ms=int(max_age_ms), **kwargs
                    )
                else:
                    if max_age_ms:
                        notes.append(
                            f"{backend.name} does not cache frames; max_age_ms was ignored"
                        )
                    width, height = await capture_scaled(target, rect, **kwargs)
            age = getattr(backend, "last_capture_age_ms", 0)
            if age:
                notes.append(f"reused a frame captured {age}ms ago")
        tail = f" ({'; '.join(notes)})" if notes else ""
        return (
            f"Saved {target} ({width}x{height} px, region {rect}){tail}. "
            f"Read that file path to view the image."
        )

    @tool()
    async def screen_record(seconds: float = 5.0, path: str = "") -> str:
        """Record the whole screen for a few seconds and return the file path.

        The file is a .webm (or .mp4) when ffmpeg is installed; without it the Windows
        backend writes an animated .png instead, so the returned path is authoritative.
        """
        backend = be()
        target = shot_path(path, ".webm")
        result = await backend.record_video(target, float(seconds))
        size_kb = result.stat().st_size / 1024
        return (
            f"Recorded {seconds:g}s to {result} ({size_kb:.0f} KiB). "
            f"Read that file path to watch it."
        )

    @tool()
    async def mouse_move(x: int, y: int, cursor: str = "default") -> str:
        """Move the mouse to absolute pixel coordinates (0,0 = top-left of the desktop).

        cursor is an optional shape: default, hand, text, crosshair, wait, not-allowed,
        n-resize, s-resize, e-resize, w-resize (backends may ignore it).
        """
        await be().move(int(x), int(y), cursor=cursor)
        return f"Mouse is at ({int(x)}, {int(y)})."

    @tool()
    async def mouse_move_by(dx: int, dy: int) -> str:
        """Move the mouse by a relative offset in pixels."""
        await be().move_by(int(dx), int(dy))
        return f"Moved the mouse by ({int(dx)}, {int(dy)})."

    @tool()
    async def mouse_click(x: int = -1, y: int = -1, button: str = "left", clicks: int = 1) -> str:
        """Click the mouse, optionally moving to (x, y) first. clicks=2 double-clicks."""
        backend = be()
        bx = int(x) if x >= 0 else None
        by = int(y) if y >= 0 else None
        await backend.click(bx, by, button=button, clicks=int(clicks))
        target = f" at ({bx}, {by})" if bx is not None and by is not None else ""
        return f"Clicked {button}{target} {int(clicks)}x."

    @tool()
    async def mouse_drag(
        x1: int, y1: int, x2: int, y2: int, button: str = "left", steps: int = 24
    ) -> str:
        """Press the mouse at (x1,y1), drag to (x2,y2), release. Useful for sliders and text."""
        await be().drag(int(x1), int(y1), int(x2), int(y2), button=button, steps=int(steps))
        return f"Dragged ({int(x1)},{int(y1)}) -> ({int(x2)},{int(y2)})."

    @tool()
    async def mouse_scroll(
        amount: int = 3, direction: str = "down", horizontal: bool = False
    ) -> str:
        """Scroll the wheel by `amount` notches: direction is 'up' or 'down' (or left/right)."""
        await be().scroll(int(amount), direction, horizontal)
        return f"Scrolled {direction} {int(amount)}."

    @tool()
    async def type_text(text: str, interval_ms: int = 0) -> str:
        """Type literal text into the focused window, one character at a time.

        interval_ms paces the keystrokes so a human can see them type; the default of 0
        types as fast as the input queue accepts, which is what almost every caller
        wants. Ask for a value only when typing into something that drops input when it
        arrives too fast.
        """
        count = await be().type_text(text, interval=float(interval_ms) / 1000.0)
        return f"Typed {count} character(s): {text!r}"

    @tool()
    async def key_press(keys: str) -> str:
        """Press a key or a combo.

        'Return', 'Tab', 'Escape', 'F5', the arrows, and the punctuation names
        ('minus', 'plus', 'lbracket', 'comma', ...) all work, on their own or with
        modifiers: 'ctrl+c', 'ctrl+shift+t', 'alt+F4'. A literal '+' is a key too, so
        'ctrl++' means Ctrl and the plus sign.
        """
        await be().key_press(keys)
        return f"Pressed {keys}."

    @tool()
    async def screen_state() -> str:
        """What is on the desktop as facts, with no image and no pixels.

        Returns the foreground window, the visible top-level windows with their titles
        and rectangles, the pointer position, the desktop size and the DPI mode. Costs
        microseconds, so call this to find out where to look instead of taking a
        screenshot to find out what is open - then crop your next screenshot to the
        window you actually care about.
        """
        return _json(await be().screen_state())

    @tool()
    async def screen_do(actions: list[dict], stop_on_error: bool = True) -> str:
        """Run several desktop actions in one call and report what each did.

        actions: a list of objects, each with an "op" key:
          {"op":"click","x":100,"y":200,"clicks":1}
          {"op":"move","x":10,"y":20}      {"op":"move_by","dx":0,"dy":-50}
          {"op":"drag","x1":..,"y1":..,"x2":..,"y2":..}
          {"op":"scroll","amount":5,"direction":"down"}
          {"op":"type","text":"hello","interval_ms":0}
          {"op":"key","keys":"ctrl+s"}      {"op":"shot","region":"x,y,w,h","scale":0}
          {"op":"wait","ms":250}            (capped at 5000)

        Batching trades away the chance to look between actions, so use it for steps you
        already know - typing a string, pressing a sequence of keys, scrolling a known
        distance. When you are not sure what is on screen, take one screenshot and act
        with the single-action tools instead.
        """
        results = await run_actions(be(), actions, stop_on_error=stop_on_error)
        return _json(
            [{"op": r.op, "ok": r.ok, "detail": r.detail, "error": r.error} for r in results]
        )

    @tool()
    async def page_state() -> str:
        """The active browser tab's url, title, scroll position and pixel ratio, as JSON.

        The cheapest way to find out what a web page is doing, and the cheapest way to
        check that a click worked: call it before and after and compare. Needs a
        Chromium browser started with --remote-debugging-port=9222 and its own
        --user-data-dir; otherwise it raises rather than guessing.
        """
        from .browser import page_state as _state

        return _json(await _state())

    @tool()
    async def page_find(
        selector: str, nth: int = 0, limit: int = 50, scroll_into_view: bool = True
    ) -> str:
        """List the elements a CSS selector matches, with exact text, href and geometry.

        selector: any CSS selector, e.g. "a" for every link or ".b_algo h2 a" for search
        results. nth: return only that one, 1-based. limit: cap the list.
        scroll_into_view: scroll each match to the middle of the viewport before reading
        its position, which is what makes the coordinates usable.

        This is the tool that replaces reading pixels to find a link: it is exact, it
        costs about a millisecond, and it works for elements that are off-screen. The
        x/y/w/h are CSS pixels in the page's own coordinate space; to click at them
        prefer page_click, which handles the conversion.
        """
        from .browser import page_find as _elements

        if nth > 0:
            found = await _elements(
                selector, nth=nth, limit=limit, scroll_into_view=scroll_into_view
            )
            return _json([*found])
        return _json(await _elements(selector, limit=limit, scroll_into_view=scroll_into_view))

    @tool()
    async def page_click(
        selector: str,
        nth: int = 1,
        scroll_into_view: bool = True,
        verify: bool = True,
        method: str = "auto",
    ) -> str:
        """Click the nth element matching a CSS selector, exactly, and report the change.

        nth is 1-based. scroll_into_view brings it into the viewport first, then the
        position is read again immediately before the click, so a page that reflows
        between measuring and clicking still gets the right element. verify captures
        the url and title before and after and returns the difference.

        method is 'auto' (default), 'input' or 'dom'. 'auto' sends a real mouse event when
        the page can receive one, and clicks inside the page when it cannot - a browser
        window hidden behind another window drops synthesised clicks silently, which is
        the normal case when a terminal is in front. The result's "how" field says which
        happened; a "dom-click" fired no mouse events, so it will not open a menu that
        reacts to mousedown.

        Returns an error rather than clicking a guessed coordinate when the element is
        not there, when it is inside an iframe, or when no browser is listening on the
        debug port.
        """
        from .browser import page_click as _click

        return _json(
            await _click(
                selector,
                nth=int(nth),
                scroll_into_view=bool(scroll_into_view),
                verify=bool(verify),
                method=str(method),
            )
        )

    return mcp


def run_stdio(backend: Backend | None = None) -> None:
    """Serve the MCP protocol on stdin/stdout (how every MCP client starts a server)."""
    build_server(backend).run(transport="stdio")
