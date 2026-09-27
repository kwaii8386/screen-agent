"""Wayland backend built from standard CLI tools: grim/wl-shot for capture, ydotool for input.

Works on wlroots compositors (Sway, Hyprland, river, Wayfire) and anywhere else the
tools happen to be installed. It is also the fallback for GNOME when the Mutter D-Bus
backend is unavailable - GNOME needs ydotool running as a uinput daemon.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

from ..core import Rect, ScreenAgentError, UnsupportedOperation, run, run_capture, scroll_delta
from .base import Backend, BackendInfo, Cursor

__all__ = ["WaylandToolsBackend"]

_CAPTURE_TOOLS = ("grim", "wlshot", "wayshot", "hyprshot")
_TYPE_TOOLS = ("wtype", "dotool", "ydotool", "xdotool")


class WaylandToolsBackend(Backend):
    """Screenshot/record + input through external Wayland command line tools."""

    name = "wayland-tools"
    display_name = "Wayland (grim/ydotool CLI tools)"

    def __init__(self) -> None:
        super().__init__()
        self._notes: list[str] = []
        self._checked_ydaemon = False

    @classmethod
    def detect(cls) -> str:
        if os.environ.get("SCREEN_AGENT_BACKEND"):
            return ""
        if not os.environ.get("WAYLAND_DISPLAY"):
            return "WAYLAND_DISPLAY is not set"
        capture = next((t for t in _CAPTURE_TOOLS if shutil.which(t)), None)
        if not capture:
            return f"none of {', '.join(_CAPTURE_TOOLS)} found"
        if not any(shutil.which(t) for t in _TYPE_TOOLS):
            return f"none of {', '.join(_TYPE_TOOLS)} found"
        return ""

    # ---------------------------------------------------------------- geometry
    def _query_desktop(self) -> Rect:
        from .mutter import xrandr_desktop  # noqa: PLC0415

        return xrandr_desktop()

    def _capture_tool(self) -> str:
        for tool in _CAPTURE_TOOLS:
            if shutil.which(tool):
                return tool
        raise UnsupportedOperation(
            f"no Wayland screenshot tool found; install one of {', '.join(_CAPTURE_TOOLS)}"
        )

    async def info(self) -> BackendInfo:
        cap = next((t for t in _CAPTURE_TOOLS if shutil.which(t)), None)
        typ = next((t for t in _TYPE_TOOLS if shutil.which(t)), None)
        notes = list(self._notes)
        if not cap:
            notes.append(f"install one of: {', '.join(_CAPTURE_TOOLS)}")
        if not typ:
            notes.append(f"install one of: {', '.join(_TYPE_TOOLS)}")
        elif typ == "ydotool":
            notes.append("ydotool needs its daemon running: sudo ydotoold &")
        elif typ == "dotool":
            notes.append("dotool needs read/write access to /dev/uinput")
        return BackendInfo(
            backend=self.name,
            platform=os.uname().sysname.lower() if hasattr(os, "uname") else "posix",
            session=self._env_session(),
            desktop=self.desktop,
            capabilities={
                "screenshot": cap is not None,
                "record": shutil.which("wf-recorder") is not None,
                "mouse": typ in ("ydotool", "dotool", "xdotool"),
                "keyboard": typ is not None,
                "absolute_pointer": typ in ("ydotool", "dotool", "xdotool"),
            },
            tools={
                "capture": cap or "MISSING",
                "input": typ or "MISSING",
                "wf-recorder": shutil.which("wf-recorder"),
            },
            notes=notes,
        )

    # ------------------------------------------------------------------ screen
    async def capture_png(self, path: Path, rect: Rect, cursor: bool = True) -> tuple[int, int]:
        from ..core import _png_size  # noqa: PLC0415

        tool = self._capture_tool()
        if path.exists():
            path.unlink()
        full = self.desktop
        if tool == "grim":
            if (rect.x, rect.y, rect.w, rect.h) == (full.x, full.y, full.w, full.h):
                await run("grim", str(path), timeout=20)
            else:
                await run("grim", "-g", f"{rect.x},{rect.y} {rect.w}x{rect.h}", str(path), timeout=20)
        elif tool == "wlshot":
            if (rect.x, rect.y, rect.w, rect.h) != (full.x, full.y, full.w, full.h):
                await run("wlshot", "-d", "0", "-f", str(path), "-r", f"{rect.x},{rect.y} {rect.w}x{rect.h}", timeout=20)
            else:
                await run("wlshot", "-d", "0", "-f", str(path), timeout=20)
        else:  # wayshot / hyprshot handle -g "x,y w,h"
            await run(tool, "-o", str(path), "-g", f"{rect.x},{rect.y} {rect.w}x{rect.h}", timeout=20)
        return _png_size(path)

    async def record_video(self, path: Path, seconds: float) -> Path:
        if not shutil.which("wf-recorder"):
            raise UnsupportedOperation("install wf-recorder to record video on Wayland")
        if path.exists():
            path.unlink()
        await run_capture(
            ["wf-recorder", "-f", str(path), "-g", str(self.desktop)],
            deadline=max(1.0, seconds) + 2.0,
        )
        if not path.exists() or path.stat().st_size == 0:
            raise ScreenAgentError("wf-recorder produced no output")
        return path

    # ----------------------------------------------------------------- pointer
    async def _ensure_ydaemon(self) -> None:
        if self._checked_ydaemon:
            return
        self._checked_ydaemon = True
        if shutil.which("ydotool") and not _ydaemon_alive():
            raise ScreenAgentError(
                "ydotool is installed but its daemon is not running. "
                "Start it once with: sudo ydotoold &"
            )

    async def move(self, x: int, y: int, cursor: str = Cursor.DEFAULT) -> None:
        await self._ensure_ydaemon()
        tool = _input_tool()
        if tool in ("ydotool", "dotool"):
            home = ["ydotool", "mousemove_absolute", "-", "-", str(x), str(y)] if tool == "ydotool" \
                else ["dotool", "mousemove_absolute", str(x), str(y)]
            await run(*home, timeout=10)
        elif tool == "xdotool":
            await run("xdotool", "mousemove", "--sync", str(x), str(y), timeout=10)
        else:
            raise UnsupportedOperation(
                f"{tool} cannot move the pointer; install ydotool or dotool for mouse control"
            )
        await asyncio.sleep(0.02)

    async def move_by(self, dx: int, dy: int) -> None:
        await self._ensure_ydaemon()
        tool = _input_tool()
        if tool == "ydotool":
            await run("ydotool", "mousemove_relative", "--", str(dx), str(dy), timeout=10)
        elif tool == "dotool":
            await run("dotool", "mousemove_relative", str(dx), str(dy), timeout=10)
        elif tool == "xdotool":
            await run("xdotool", "mousemove_relative", "--sync", "--", str(dx), str(dy), timeout=10)
        else:
            raise UnsupportedOperation(f"{tool} cannot move the pointer")
        await asyncio.sleep(0.02)

    async def click(
        self, x: int | None = None, y: int | None = None, button: str = "left", clicks: int = 1
    ) -> None:
        await self._ensure_ydaemon()
        if x is not None and y is not None:
            await self.move(x, y)
        code = {"left": 0xC0, "right": 0xC1, "middle": 0xC2}.get(button.strip().lower())
        if code is None:
            raise ScreenAgentError(f"unknown button {button!r}; use left, middle or right")
        tool = _input_tool()
        for _ in range(max(1, int(clicks))):
            if tool in ("ydotool", "dotool"):
                await run(tool, str(code), timeout=10)
                await run(tool, str(code + 1), timeout=10)
            elif tool == "xdotool":
                num = {"left": 1, "middle": 2, "right": 3}[button.strip().lower()]
                await run("xdotool", "click", str(num), timeout=10)
            else:
                raise UnsupportedOperation(f"{tool} cannot click the mouse")
            await asyncio.sleep(0.09)

    async def drag(
        self, x1: int, y1: int, x2: int, y2: int, button: str = "left", steps: int = 24
    ) -> None:
        await self._ensure_ydaemon()
        tool = _input_tool()
        if tool not in ("ydotool", "dotool"):
            raise UnsupportedOperation("dragging needs ydotool or dotool")
        code = {"left": 0xC0, "right": 0xC1, "middle": 0xC2}.get(button.strip().lower(), 0xC0)
        await self.move(x1, y1)
        await run(tool, str(code), timeout=10)
        steps = max(2, int(steps))
        for i in range(1, steps + 1):
            t = i / steps
            await self.move(int(x1 + (x2 - x1) * t), int(y1 + (y2 - y1) * t))
            await asyncio.sleep(0.012)
        await run(tool, str(code + 1), timeout=10)

    async def scroll(self, amount: int = 3, direction: str = "down", horizontal: bool = False) -> None:
        await self._ensure_ydaemon()
        tool = _input_tool()
        n = scroll_delta(int(amount), direction, horizontal)  # positive == up
        # evdev wheel: 0xC5 vertical, 0xC6 horizontal, +3 is the matching down button
        btn = 0xC5 if not horizontal else 0xC6
        if n < 0:
            btn += 3
        if tool not in ("ydotool", "dotool"):
            raise UnsupportedOperation("scrolling needs ydotool or dotool")
        for _ in range(abs(n)):
            await run(tool, str(btn), timeout=10)
            await run(tool, str(btn + 1), timeout=10)
            await asyncio.sleep(0.05)

    # ---------------------------------------------------------------- keyboard
    async def type_text(self, text: str, interval: float = 0.008) -> int:
        tool = _input_tool()
        if tool in ("wtype", "ydotool", "dotool", "xdotool"):
            if tool == "wtype":
                await run("wtype", "--", text, timeout=60)
            else:
                await self.key_press_char(text)
            return len(text)
        raise UnsupportedOperation(f"{tool} cannot type text")

    async def key_press_char(self, text: str) -> None:
        """ydotool wants 1-based evdev keycodes; map via a small ASCII table."""
        for ch in text:
            code = _EVDEV_ASCII.get(ch)
            if code is None:
                code = _EVDEV_ASCII.get(ch.upper())
            if code is None:
                continue
            await run("ydotool", "key", str(code), ":1", timeout=10)
            await run("ydotool", "key", str(code), ":0", timeout=10)

    async def key_press(self, combo: str) -> None:
        from ..keys import parse_combo  # noqa: PLC0415

        parsed = parse_combo(combo)
        tool = _input_tool()
        if tool == "wtype":
            # wtype only takes literal text
            if parsed.modifiers or not parsed.is_named:
                raise UnsupportedOperation(
                    "wtype cannot send modifier combos; use ydotool for that"
                )
            await run("wtype", "--", parsed.key, timeout=15)
            return
        if tool == "xdotool":
            mapping = {
                "return": "Return", "tab": "Tab", "escape": "Escape", "space": "space",
                "backspace": "BackSpace", "delete": "Delete", "insert": "Insert",
                "home": "Home", "end": "End", "pageup": "Prior", "pagedown": "Next",
                "up": "Up", "down": "Down", "left": "Left", "right": "Right",
                "super": "super", "ctrl": "ctrl", "alt": "alt", "shift": "shift",
            }
            parts = [mapping.get(m, m) for m in parsed.modifiers]
            parts.append(mapping.get(parsed.key, parsed.key))
            await run("xdotool", "key", "--clearmodifiers", "--", "+".join(parts), timeout=15)
            return
        # ydotool / dotool: evdev keycodes
        held: list[int] = []
        for mod in parsed.modifiers:
            code = _EVDEV_MODIFIERS.get(mod)
            if code is None:
                raise UnsupportedOperation(f"modifier {mod!r} is not available via {tool}")
            await run(tool, str(code), "1", timeout=10)
            held.append(code)
            await asyncio.sleep(0.015)
        code = _evdev_for(parsed.key)
        try:
            await run(tool, str(code), "1", timeout=10)
            await asyncio.sleep(0.02)
            await run(tool, str(code), "0", timeout=10)
        finally:
            for m in reversed(held):
                await run(tool, str(m), "0", timeout=10)


def _input_tool() -> str:
    for tool in _TYPE_TOOLS:
        if shutil.which(tool):
            return tool
    raise UnsupportedOperation(
        f"no Wayland input tool found; install one of {', '.join(_TYPE_TOOLS)}"
    )


def _ydaemon_alive() -> bool:
    proc = Path("/proc")
    if not proc.exists():
        return False
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            comm = (entry / "comm").read_text().strip()
        except OSError:
            continue
        if comm in ("ydotoold", "ydotool"):
            return True
    return False


# evdev keycodes: ASCII
_EVDEV_ASCII = {
    "1": 2, "2": 3, "3": 4, "4": 5, "5": 6, "6": 7, "7": 8, "8": 9, "9": 10, "0": 11,
    "-": 12, "=": 13, "q": 16, "w": 17, "e": 18, "r": 19, "t": 20, "y": 21, "u": 22,
    "i": 23, "o": 24, "p": 25, "[": 26, "]": 27, "\n": 28, "a": 30, "s": 31, "d": 32,
    "f": 33, "g": 34, "h": 35, "j": 36, "k": 37, "l": 38, ";": 39, "'": 40, "`": 41,
    "\\": 43, "z": 44, "x": 45, "c": 46, "v": 47, "b": 48, "n": 49, "m": 50, ",": 51,
    ".": 52, "/": 53, " ": 57, "\t": 15, "A": 30, "S": 31, "D": 32, "F": 33, "G": 34,
    "H": 35, "J": 36, "K": 37, "L": 38, "Z": 44, "X": 45, "C": 46, "V": 47, "B": 48,
    "N": 49, "M": 50, "Q": 16, "W": 17, "E": 18, "R": 19, "T": 20, "Y": 21, "U": 22,
    "I": 23, "O": 24, "P": 25,
}
_EVDEV_SPECIAL = {
    "return": 28, "enter": 28, "tab": 15, "escape": 1, "esc": 1, "space": 57,
    "backspace": 14, "delete": 111, "insert": 110, "home": 102, "end": 107,
    "pageup": 104, "pagedown": 109, "up": 103, "down": 108, "left": 105, "right": 106,
    "capslock": 58, "numlock": 69, "scrolllock": 70, "printscreen": 99, "menu": 127,
    "pause": 119, "minus": 12, "period": 52, "comma": 51, "slash": 53,
    "semicolon": 39, "apostrophe": 40, "grave": 41, "backslash": 43,
    "lbracket": 26, "rbracket": 27, "equal": 13, "plus": 78,
}
for _i in range(1, 25):
    _EVDEV_SPECIAL[f"f{_i}"] = 58 + _i
for _i in range(10):
    _EVDEV_SPECIAL[f"kp{_i}"] = 79 + _i

_EVDEV_MODIFIERS = {
    "ctrl": 29, "alt": 56, "shift": 42, "super": 125, "meta": 125, "fn": 87,
    "hyper": 125,
}


def _evdev_for(key: str) -> int:
    name = key.lower()
    if name in _EVDEV_SPECIAL:
        return _EVDEV_SPECIAL[name]
    if name in _EVDEV_ASCII:
        return _EVDEV_ASCII[name]
    if len(name) == 1 and name.upper() in _EVDEV_ASCII:
        return _EVDEV_ASCII[name.upper()]
    raise UnsupportedOperation(f"no evdev keycode for {key!r}")
