"""macOS backend: CoreGraphics event injection plus the system screenshot tools.

Capture prefers the bundled ``screencapture`` binary (needs no extra package) and
falls back to Pillow's ``ImageGrab``. Input goes through CoreGraphics so we can post
real key and mouse events instead of driving System Events via AppleScript, which
makes key names and modifiers behave consistently.

Both require the user to grant screen recording / accessibility permission to the
process that starts the MCP server (usually your terminal).
"""

from __future__ import annotations

import asyncio
import ctypes
import ctypes.util
import os
import platform
import shutil
import tempfile
from pathlib import Path

from ..core import Rect, ScreenAgentError, UnsupportedOperation, run
from .base import Backend, BackendInfo, Cursor

__all__ = ["MacOSBackend"]

# CoreGraphics CGEventType
_KB_DOWN = 0x0
_KB_UP = 0x1
_MOUSE_DOWN = 0x0
_MOUSE_UP = 0x1
_MOUSE_MOVED = 0x0
_SCROLL = 0x0

_HID_EVENT_TAP = 0
_LEFT_DOWN, LEFT_UP, RIGHT_DOWN, RIGHT_UP, MID_DOWN, MID_UP = 1, 2, 3, 4, 25, 26
_MOVE_ABS = 0x1  # kCGEventFlagMaskAbsolute

_MODIFIER_FLAGS = {
    "shift": 0x0002_0000,
    "ctrl": 0x0004_0000,
    "alt": 0x0008_0000,  # option
    "super": 0x0010_0000,  # command
    "meta": 0x0008_0000,
    "fn": 0x0080_0000,
    "capslock": 0x0010_0000,
}

# macOS virtual key codes (Carbon HIToolbox Events.h) for keys we can name
_KEYCODES = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9,
    "b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17, "1": 18, "2": 19,
    "3": 20, "4": 21, "6": 22, "5": 23, "equal": 24, "9": 25, "7": 26, "minus": 27,
    "8": 28, "0": 29, "rbracket": 30, "o": 31, "u": 32, "lbracket": 33, "i": 34,
    "p": 35, "return": 36, "l": 37, "j": 38, "apostrophe": 39, "k": 40, "semicolon": 41,
    "backslash": 42, "comma": 43, "slash": 44, "n": 45, "m": 46, "period": 47, "tab": 48,
    "space": 49, "grave": 50, "backspace": 51, "escape": 53, "kpenter": 76,
    "f17": 64, "f18": 79, "f19": 80, "f20": 90, "f5": 96, "f6": 97, "f7": 98, "f3": 99,
    "f8": 100, "f9": 101, "f11": 103, "f13": 105, "f16": 106, "f14": 107, "f10": 109,
    "f12": 111, "f15": 113, "help": 114, "home": 115, "pageup": 116, "delete": 117,
    "f4": 118, "end": 119, "f2": 120, "pagedown": 121, "f1": 122, "left": 123,
    "right": 124, "down": 125, "up": 126,
}
for _i in range(10):
    _KEYCODES[f"kp{_i}"] = 65 + _i

# keys whose keycode we do not know get typed as text instead
_NEEDS_TEXT = {"f21", "f22", "f23", "f24", "insert", "printscreen", "menu", "numlock",
               "scrolllock", "capslock", "pause"}


class _CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


def _load_cg():
    """Load CoreGraphics through ctypes, once."""
    cg = ctypes.cdll.LoadLibrary(
        "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"
    )
    cg.CGEventCreateKeyboardEvent.restype = ctypes.c_void_p
    cg.CGEventCreateKeyboardEvent.argtypes = [ctypes.c_void_p, ctypes.c_ushort, ctypes.c_bool]
    cg.CGEventCreateMouseEvent.restype = ctypes.c_void_p
    cg.CGEventCreateMouseEvent.argtypes = [
        ctypes.c_void_p, ctypes.c_uint, _CGPoint, ctypes.c_uint
    ]
    cg.CGEventCreateScrollWheelEvent.restype = ctypes.c_void_p
    cg.CGEventCreateScrollWheelEvent.argtypes = [
        ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_int, ctypes.c_int
    ]
    cg.CGEventPost.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    cg.CGEventSetFlags.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
    cg.CGEventKeyboardSetUnicodeString.argtypes = [
        ctypes.c_void_p, ctypes.c_long, ctypes.c_wchar_p
    ]
    cg.CGEventSourceCreate.argtypes = [ctypes.c_uint]
    cg.CGEventSourceCreate.restype = ctypes.c_void_p
    return cg


class MacOSBackend(Backend):
    """macOS via CoreGraphics events and ``screencapture``."""

    name = "macos"
    display_name = "macOS (CoreGraphics + screencapture)"

    def __init__(self) -> None:
        super().__init__()
        self._cg = None
        self._notes: list[str] = []

    @classmethod
    def detect(cls) -> str:
        if os.environ.get("SCREEN_AGENT_BACKEND"):
            return ""
        if platform.system() != "Darwin":
            return "not macOS"
        if not shutil.which("screencapture"):
            return "screencapture not found"
        return ""

    def _core_graphics(self):
        if self._cg is None:
            try:
                self._cg = _load_cg()
            except OSError as exc:
                raise UnsupportedOperation(f"CoreGraphics unavailable: {exc}") from exc
        return self._cg

    # ---------------------------------------------------------------- geometry
    def _query_desktop(self) -> Rect:
        """Ask the window server for the main display size, and read Retina scaling."""
        try:
            from Quartz import CGDisplayPixelsWide  # type: ignore[import-not-found]

            return Rect(0, 0, int(CGDisplayPixelsWide(0)), int(CGDisplayPixelsHigh(0)))
        except Exception:  # noqa: BLE001
            pass
        try:
            import subprocess  # noqa: PLC0415

            out = subprocess.run(
                ["system_profiler", "SPDisplaysDataType"], capture_output=True, text=True,
                timeout=15,
            ).stdout
            m = re_search_resolution(out)
            if m:
                return Rect(0, 0, m[0], m[1])
        except Exception:  # noqa: BLE001
            pass
        return Rect(0, 0, 1920, 1080)

    async def info(self) -> BackendInfo:
        caps = {
            "screenshot": shutil.which("screencapture") is not None,
            "record": shutil.which("screencapture") is not None,
            "mouse": True,
            "keyboard": True,
            "absolute_pointer": True,
        }
        notes = list(self._notes)
        notes.append(
            "macOS needs Screen Recording and Accessibility permission for the terminal "
            "that starts this server (System Settings > Privacy & Security)"
        )
        try:
            self._core_graphics()
        except UnsupportedOperation as exc:
            notes.append(str(exc))
        return BackendInfo(
            backend=self.name,
            platform="darwin",
            session=self._env_session(),
            desktop=self.desktop,
            capabilities=caps,
            tools={"screencapture": shutil.which("screencapture")},
            notes=notes,
        )

    # ------------------------------------------------------------------ screen
    async def capture_png(self, path: Path, rect: Rect, cursor: bool = True) -> tuple[int, int]:
        from ..core import _png_size  # noqa: PLC0415

        if path.exists():
            path.unlink()
        full = self.desktop
        if (rect.w, rect.h) == (full.w, full.h) and (rect.x, rect.y) == (0, 0):
            await run("screencapture", "-x", str(path), timeout=20)
        else:
            await run(
                "screencapture", "-x", "-R", f"{rect.x},{rect.y},{rect.w},{rect.h}",
                str(path), timeout=20,
            )
        if not path.exists():
            await self._capture_pillow(path, rect)
        return _png_size(path)

    async def _capture_pillow(self, path: Path, rect: Rect) -> None:
        def grab() -> None:
            from PIL import ImageGrab  # noqa: PLC0415

            im = ImageGrab.grab(bbox=(rect.x, rect.y, rect.x + rect.w, rect.y + rect.h))
            im.save(path)

        try:
            await asyncio.to_thread(grab)
        except Exception as exc:  # noqa: BLE001
            raise ScreenAgentError(
                f"screencapture failed and Pillow fallback failed too: {exc}"
            ) from exc

    async def record_video(self, path: Path, seconds: float) -> Path:
        if not shutil.which("screencapture"):
            raise UnsupportedOperation("screencapture not found")
        if path.exists():
            path.unlink()
        with tempfile.TemporaryDirectory() as tmp:
            await run(
                "screencapture", "-x", "-V", f"{max(0.5, seconds):g}", os.path.join(tmp, "clip.mov"),
                timeout=max(10.0, seconds + 10),
            )
            produced = Path(tmp) / "clip.mov"
            if not produced.exists():
                raise ScreenAgentError("screencapture produced no file")
            if shutil.which("ffmpeg"):
                await run(
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-i", str(produced), "-pix_fmt", "yuv420p", str(path), timeout=60,
                )
            else:
                path = path.with_suffix(".mov")
                shutil.copy(produced, path)
        return path

    # ----------------------------------------------------------------- pointer
    def _post_mouse(self, event_type: int, x: float, y: float, button: int) -> None:
        cg = self._core_graphics()
        src = cg.CGEventSourceCreate(_HID_EVENT_TAP)
        ev = cg.CGEventCreateMouseEvent(src, event_type, _CGPoint(x, y), button)
        cg.CGEventSetFlags(ev, _MOVE_ABS)
        cg.CGEventPost(_HID_EVENT_TAP, ev)
        ctypes.cast(ev, ctypes.c_void_p)

    async def move(self, x: int, y: int, cursor: str = Cursor.DEFAULT) -> None:
        self._post_mouse(_MOUSE_MOVED, float(x), float(y), _LEFT_DOWN)
        await asyncio.sleep(0.01)

    async def move_by(self, dx: int, dy: int) -> None:
        raise UnsupportedOperation(
            "macOS cannot move the pointer relatively; pass absolute coordinates instead"
        )

    async def click(
        self, x: int | None = None, y: int | None = None, button: str = "left", clicks: int = 1
    ) -> None:
        b = button.strip().lower()
        if b == "left":
            down, up = _LEFT_DOWN, _LEFT_UP
        elif b == "right":
            down, up = _RIGHT_DOWN, _RIGHT_UP
        elif b == "middle":
            down, up = _MID_DOWN, _MID_UP
        else:
            raise ScreenAgentError(f"unknown button {button!r}; use left, middle or right")
        if x is not None and y is not None:
            await self.move(x, y)
        for _ in range(max(1, int(clicks))):
            self._post_mouse(_MOUSE_DOWN, float(x or 0), float(y or 0), down)
            await asyncio.sleep(0.04)
            self._post_mouse(_MOUSE_UP, float(x or 0), float(y or 0), up)
            await asyncio.sleep(0.09)

    async def drag(
        self, x1: int, y1: int, x2: int, y2: int, button: str = "left", steps: int = 24
    ) -> None:
        down, up = {
            "left": (_LEFT_DOWN, _LEFT_UP),
            "right": (_RIGHT_DOWN, _RIGHT_UP),
        }.get(button.strip().lower(), (_LEFT_DOWN, _LEFT_UP))
        await self.move(x1, y1)
        self._post_mouse(_MOUSE_DOWN, float(x1), float(y1), down)
        steps = max(2, int(steps))
        for i in range(1, steps + 1):
            t = i / steps
            self._post_mouse(
                _MOUSE_MOVED, x1 + (x2 - x1) * t, y1 + (y2 - y1) * t, down
            )
            await asyncio.sleep(0.012)
        await asyncio.sleep(0.05)
        self._post_mouse(_MOUSE_UP, float(x2), float(y2), up)

    async def scroll(self, amount: int = 3, direction: str = "down", horizontal: bool = False) -> None:
        cg = self._core_graphics()
        d = direction.strip().lower()
        n = abs(int(amount))
        if d in ("up", "left"):
            n = -n
        elif d not in ("down", "right"):
            raise ScreenAgentError("direction must be up/down (left/right when horizontal=True)")
        src = cg.CGEventSourceCreate(_HID_EVENT_TAP)
        ev = cg.CGEventCreateScrollWheelEvent(
            src, _SCROLL, 2 if horizontal else 1, int(n), 0
        )
        cg.CGEventPost(_HID_EVENT_TAP, ev)

    # ---------------------------------------------------------------- keyboard
    def _post_key(self, keycode: int, down: bool, flags: int = 0) -> None:
        cg = self._core_graphics()
        src = cg.CGEventSourceCreate(_HID_EVENT_TAP)
        ev = cg.CGEventCreateKeyboardEvent(src, keycode, down)
        if flags:
            cg.CGEventSetFlags(ev, flags)
        cg.CGEventPost(_HID_EVENT_TAP, ev)

    def _post_text(self, text: str) -> None:
        cg = self._core_graphics()
        src = cg.CGEventSourceCreate(_HID_EVENT_TAP)
        for ch in text:
            ev = cg.CGEventCreateKeyboardEvent(src, 0, True)
            cg.CGEventKeyboardSetUnicodeString(ev, 1, ch)
            cg.CGEventPost(_HID_EVENT_TAP, ev)
            up = cg.CGEventCreateKeyboardEvent(src, 0, False)
            cg.CGEventKeyboardSetUnicodeString(up, 1, ch)
            cg.CGEventPost(_HID_EVENT_TAP, up)

    async def type_text(self, text: str, interval: float = 0.008) -> int:
        for ch in text:
            if ch in ("\n", "\r"):
                self._post_key(_KEYCODES["return"], True)
                self._post_key(_KEYCODES["return"], False)
            elif ch == "\t":
                self._post_key(_KEYCODES["tab"], True)
                self._post_key(_KEYCODES["tab"], False)
            else:
                self._post_text(ch)
            count = 1
            if interval:
                await asyncio.sleep(interval)
        return len(text)

    async def key_press(self, combo: str) -> None:
        from ..keys import parse_combo  # noqa: PLC0415

        parsed = parse_combo(combo)
        flags = 0
        for mod in parsed.modifiers:
            flags |= _MODIFIER_FLAGS.get(mod, 0)
        name = parsed.key.lower()
        if parsed.is_named and name in _NEEDS_TEXT:
            # no reliable keycode (e.g. F21+): fall back to AppleScript
            await self._key_via_osascript(parsed.key, parsed.modifiers)
            return
        code = _KEYCODES.get(name)
        if code is None and not parsed.is_named:
            self._post_text(parsed.key)
            return
        if code is None:
            raise UnsupportedOperation(f"key {parsed.key!r} is not supported on macOS")
        self._post_key(code, True, flags)
        await asyncio.sleep(0.02)
        self._post_key(code, False, flags)

    async def _key_via_osascript(self, key: str, modifiers: tuple[str, ...]) -> None:
        if not shutil.which("osascript"):
            raise UnsupportedOperation(f"key {key!r} needs osascript on macOS")
        using = "{" + ", ".join(
            {"ctrl": "control down", "alt": "option down", "shift": "shift down",
             "super": "command down"}.get(m, f"{m} down") for m in modifiers
        ) + "}" if modifiers else ""
        arg = f" using {using}" if using else ""
        await run(
            "osascript", "-e", f'tell application "System Events" to key code "{key}"{arg}',
            timeout=15,
        )


def re_search_resolution(text: str) -> tuple[int, int] | None:
    import re  # noqa: PLC0415

    m = re.search(r"Resolution:\s*(\d+)\s*x\s*(\d+)", text)
    return (int(m.group(1)), int(m.group(2))) if m else None
