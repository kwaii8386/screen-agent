"""MCP surface: the tools an AI can call to see and control the desktop."""

from __future__ import annotations

import inspect
import time
from pathlib import Path
from typing import Any

from . import __version__
from .backends import Backend, load_backend
from .core import default_shot_dir, parse_region, scale_image

__all__ = ["build_server", "run_stdio"]

INSTRUCTIONS = """\
Control the local desktop: take screenshots, record the screen, move and click the mouse, \
scroll, type text and press key combos.

Workflow that works well:
  1. screenshot()  -> returns a file path; read that path to actually SEE the screen.
  2. act           -> mouse_click / type_text / key_press / mouse_drag / mouse_scroll.
  3. screenshot()  -> again, to verify what actually happened.

Coordinates are absolute pixels with (0,0) at the top-left of the desktop, as reported by \
screen_info(). region is either 'full' or 'x,y,w,h'.

This is the user's real, live machine: mouse_click, type_text, key_press and mouse_drag \
change their session, so only perform actions the user asked for, prefer screenshotting \
before acting, and verify afterwards.\
"""

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

    @mcp.tool()
    async def screen_info() -> str:
        """Describe the desktop: size, backend in use and what each capability supports."""
        info = await be().info()
        return info.render() + (
            "\nscreenshot dir : " + str(default_shot_dir())
            + "\ntips           : call screenshot(), then read the returned path to see it"
        )

    @mcp.tool()
    async def screenshot(
        path: str = "", region: str = "full", cursor: bool = True, scale: int = 0
    ) -> str:
        """Capture the screen (or a region) to a PNG and return the file path.

        region: 'full' or 'x,y,w,h' in desktop pixels.
        cursor: include the mouse pointer (only on some backends).
        scale: downscale so the widest side is at most this many pixels (0 = keep native),
        which is useful to save tokens on large screens.
        """
        backend = be()
        rect = parse_region(region, backend.desktop)
        target = shot_path(path)
        size = await backend.capture_png(target, rect, cursor=cursor)
        if scale and scale > 0:
            size = scale_image(target, int(scale))
        return (
            f"Saved {target} ({size[0]}x{size[1]} px, region {rect}). "
            f"Read that file path to view the image."
        )

    @mcp.tool()
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

    @mcp.tool()
    async def mouse_move(x: int, y: int, cursor: str = "default") -> str:
        """Move the mouse to absolute pixel coordinates (0,0 = top-left of the desktop).

        cursor is an optional shape: default, hand, text, crosshair, wait, not-allowed,
        n-resize, s-resize, e-resize, w-resize (backends may ignore it).
        """
        await be().move(int(x), int(y), cursor=cursor)
        return f"Mouse is at ({int(x)}, {int(y)})."

    @mcp.tool()
    async def mouse_move_by(dx: int, dy: int) -> str:
        """Move the mouse by a relative offset in pixels."""
        await be().move_by(int(dx), int(dy))
        return f"Moved the mouse by ({int(dx)}, {int(dy)})."

    @mcp.tool()
    async def mouse_click(x: int = -1, y: int = -1, button: str = "left", clicks: int = 1) -> str:
        """Click the mouse, optionally moving to (x, y) first. clicks=2 double-clicks."""
        backend = be()
        bx = int(x) if x >= 0 else None
        by = int(y) if y >= 0 else None
        await backend.click(bx, by, button=button, clicks=int(clicks))
        target = f" at ({bx}, {by})" if bx is not None and by is not None else ""
        return f"Clicked {button}{target} {int(clicks)}x."

    @mcp.tool()
    async def mouse_drag(
        x1: int, y1: int, x2: int, y2: int, button: str = "left", steps: int = 24
    ) -> str:
        """Press the mouse at (x1,y1), drag to (x2,y2), release. Useful for sliders and text."""
        await be().drag(int(x1), int(y1), int(x2), int(y2), button=button, steps=int(steps))
        return f"Dragged ({int(x1)},{int(y1)}) -> ({int(x2)},{int(y2)})."

    @mcp.tool()
    async def mouse_scroll(amount: int = 3, direction: str = "down", horizontal: bool = False) -> str:
        """Scroll the wheel by `amount` notches: direction is 'up' or 'down' (or left/right)."""
        await be().scroll(int(amount), direction, horizontal)
        return f"Scrolled {direction} {int(amount)}."

    @mcp.tool()
    async def type_text(text: str, interval_ms: int = 8) -> str:
        """Type literal text into the focused window, one character at a time."""
        count = await be().type_text(text, interval=float(interval_ms) / 1000.0)
        return f"Typed {count} character(s): {text!r}"

    @mcp.tool()
    async def key_press(keys: str) -> str:
        """Press a key or a combo.

        'Return', 'Tab', 'Escape', 'F5', the arrows, and the punctuation names
        ('minus', 'plus', 'lbracket', 'comma', ...) all work, on their own or with
        modifiers: 'ctrl+c', 'ctrl+shift+t', 'alt+F4'. A literal '+' is a key too, so
        'ctrl++' means Ctrl and the plus sign.
        """
        await be().key_press(keys)
        return f"Pressed {keys}."

    return mcp


def run_stdio(backend: Backend | None = None) -> None:
    """Serve the MCP protocol on stdin/stdout (how every MCP client starts a server)."""
    build_server(backend).run(transport="stdio")
