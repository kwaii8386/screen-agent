"""Windows backend: user32/gdi32 through ctypes, screenshots via Pillow's ImageGrab.

No extra packages are needed for input; Pillow (already an optional extra) handles
capture. Runs on the interactive desktop session only - a service or RDP-disconnected
session has no visible desktop to draw into.
"""

from __future__ import annotations

import asyncio
import ctypes
import ctypes.wintypes as wintypes
import os
from pathlib import Path

from ..core import Rect, ScreenAgentError, UnsupportedOperation
from .base import Backend, BackendInfo, Cursor

__all__ = ["WindowsBackend"]

_INPUT_MOUSE = 0
_INPUT_KEYBOARD = 1
_KEYEVENTF_KEYUP = 0x0002
_KEYEVENTF_UNICODE = 0x0004
_KEYEVENTF_EXTENDEDKEY = 0x0001
_MOUSEEVENTF_LEFTDOWN = 0x0002
_MOUSEEVENTF_LEFTUP = 0x0004
_MOUSEEVENTF_RIGHTDOWN = 0x0008
_MOUSEEVENTF_RIGHTUP = 0x0010
_MOUSEEVENTF_MIDDLEDOWN = 0x0020
_MOUSEEVENTF_MIDDLEUP = 0x0040
_MOUSEEVENTF_WHEEL = 0x0800
_MOUSEEVENTF_HWHEEL = 0x01000
_WHEEL_DELTA = 120
_SM_CURSOR = 0x00A0
_WM_SETCURSOR = 0x0020

# Windows virtual key codes
_VK = {
    "backspace": 0x08, "tab": 0x09, "return": 0x0D, "enter": 0x0D, "escape": 0x1B,
    "esc": 0x1B, "space": 0x20, "pageup": 0x21, "pagedown": 0x22, "end": 0x23,
    "home": 0x24, "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "insert": 0x2D, "delete": 0x2E, "super": 0x5B, "meta": 0x5B, "win": 0x5B,
    "shift": 0x10, "ctrl": 0x11, "alt": 0x12, "capslock": 0x14, "numlock": 0x90,
    "scrolllock": 0x91, "printscreen": 0x2C, "pause": 0x13, "menu": 0x5D,
    "lshift": 0xA0, "rshift": 0xA1, "lctrl": 0xA2, "rctrl": 0xA3,
    "lalt": 0xA4, "ralt": 0xA5,
}
for _i in range(1, 25):
    _VK[f"f{_i}"] = 0x6F + _i
for _i in range(10):
    _VK[f"kp{_i}"] = 0x60 + _i
_VK.update({c: ord(c.upper()) for c in "abcdefghijklmnopqrstuvwxyz"})
_VK.update({str(d): ord(d) for d in "0123456789"})

_CURSORS = {
    Cursor.DEFAULT: None,
    Cursor.HAND: 32649, Cursor.POINTER: 32649, Cursor.TEXT: 32515,
    Cursor.CROSSHAIR: 32515, Cursor.WAIT: 32514, Cursor.NOT_ALLOWED: 32648,
    Cursor.N_RESIZE: 32645, Cursor.S_RESIZE: 32646, Cursor.E_RESIZE: 32645,
    Cursor.W_RESIZE: 32646,
}


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG)),
    ]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG)),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT)]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


def _load_user32():
    user32 = ctypes.WinDLL("user32", use_last_error=True)  # type: ignore[attr-defined]
    user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int]
    user32.SendInput.restype = wintypes.UINT
    user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
    user32.SetCursorPos.restype = wintypes.BOOL
    user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    user32.GetSystemMetrics.restype = ctypes.c_int
    user32.LoadCursorW.argtypes = [wintypes.HINSTANCE, ctypes.c_wchar_p]
    user32.LoadCursorW.restype = ctypes.c_void_p
    user32.SetCursor.argtypes = [ctypes.c_void_p]
    user32.SetCursor.restype = ctypes.c_void_p
    return user32


class WindowsBackend(Backend):
    """Windows 10/11 desktop session."""

    name = "windows"
    display_name = "Windows (user32 SendInput + Pillow)"

    def __init__(self) -> None:
        super().__init__()
        self._user32 = None
        self._notes: list[str] = []

    @classmethod
    def detect(cls) -> str:
        if os.environ.get("SCREEN_AGENT_BACKEND"):
            return ""
        if os.name != "nt":
            return "not Windows"
        return ""

    def _u32(self):
        if self._user32 is None:
            self._user32 = _load_user32()
        return self._user32

    # ---------------------------------------------------------------- geometry
    def _query_desktop(self) -> Rect:
        u32 = self._u32()
        w = u32.GetSystemMetrics(0) or 1920  # SM_CXSCREEN
        h = u32.GetSystemMetrics(1) or 1080  # SM_CYSCREEN
        return Rect(0, 0, int(w), int(h))

    async def info(self) -> BackendInfo:
        try:
            import PIL  # noqa: F401,PLC0415

            capture = True
        except ImportError:
            capture = False
        notes = list(self._notes)
        if not capture:
            notes.append("pip install pillow (needed for screenshots on Windows)")
        notes.append("must run in the interactive desktop session, not as a service")
        return BackendInfo(
            backend=self.name,
            platform="win32",
            session=self._env_session(),
            desktop=self.desktop,
            capabilities={
                "screenshot": capture, "record": False, "mouse": True,
                "keyboard": True, "absolute_pointer": True,
            },
            tools={"Pillow": "yes" if capture else "MISSING"},
            notes=notes,
        )

    # ------------------------------------------------------------------ screen
    async def capture_png(self, path: Path, rect: Rect, cursor: bool = True) -> tuple[int, int]:
        from ..core import _png_size  # noqa: PLC0415

        if path.exists():
            path.unlink()

        def grab() -> None:
            from PIL import ImageGrab  # noqa: PLC0415

            img = ImageGrab.grab(
                bbox=(rect.x, rect.y, rect.x + rect.w, rect.y + rect.h), all_screens=True
            )
            img.save(path)

        try:
            await asyncio.to_thread(grab)
        except Exception as exc:  # noqa: BLE001
            raise ScreenAgentError(f"ImageGrab failed: {exc}") from exc
        return _png_size(path)

    async def record_video(self, path: Path, seconds: float) -> Path:
        raise UnsupportedOperation(
            "screen recording is not implemented for Windows yet; "
            "use OBS or ffmpeg gdigrab and share the file path instead"
        )

    # ----------------------------------------------------------------- pointer
    def _send(self, items: list[_INPUT]) -> None:
        u32 = self._u32()
        array = (_INPUT * len(items))(*items)
        sent = u32.SendInput(len(items), array, ctypes.sizeof(_INPUT))
        if sent != len(items):
            raise ScreenAgentError(
                f"SendInput delivered {sent}/{len(items)} events (err {ctypes.get_last_error()})"
            )

    def _mouse_event(self, flags: int, data: int = 0) -> None:
        item = _INPUT(type=_INPUT_MOUSE)
        item.mi = _MOUSEINPUT(0, 0, data, flags, 0, None)
        self._send([item])

    async def move(self, x: int, y: int, cursor: str = Cursor.DEFAULT) -> None:
        self._u32().SetCursorPos(int(x), int(y))
        oid = _CURSORS.get(cursor)
        if oid:
            handle = self._u32().LoadCursorW(None, ctypes.c_wchar_p(oid))
            if handle:
                self._u32().SetCursor(handle)
        await asyncio.sleep(0.01)

    async def move_by(self, dx: int, dy: int) -> None:
        u32 = self._u32()
        from ctypes import wintypes  # noqa: PLC0415

        class _POINT(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

        pt = _POINT()
        u32.GetCursorPos(ctypes.byref(pt))  # type: ignore[attr-defined]
        del wintypes
        self._u32().SetCursorPos(pt.x + int(dx), pt.y + int(dy))
        await asyncio.sleep(0.01)

    async def click(
        self, x: int | None = None, y: int | None = None, button: str = "left", clicks: int = 1
    ) -> None:
        if x is not None and y is not None:
            await self.move(x, y)
        b = button.strip().lower()
        flags = {
            "left": (_MOUSEEVENTF_LEFTDOWN, _MOUSEEVENTF_LEFTUP),
            "right": (_MOUSEEVENTF_RIGHTDOWN, _MOUSEEVENTF_RIGHTUP),
            "middle": (_MOUSEEVENTF_MIDDLEDOWN, _MOUSEEVENTF_MIDDLEUP),
        }.get(b)
        if flags is None:
            raise ScreenAgentError(f"unknown button {button!r}; use left, middle or right")
        for i in range(max(1, int(clicks))):
            self._mouse_event(flags[0])
            await asyncio.sleep(0.04)
            self._mouse_event(flags[1])
            if i + 1 < max(1, int(clicks)):
                await asyncio.sleep(0.08)

    async def drag(
        self, x1: int, y1: int, x2: int, y2: int, button: str = "left", steps: int = 24
    ) -> None:
        b = button.strip().lower()
        down, up = {
            "left": (_MOUSEEVENTF_LEFTDOWN, _MOUSEEVENTF_LEFTUP),
            "right": (_MOUSEEVENTF_RIGHTDOWN, _MOUSEEVENTF_RIGHTUP),
        }.get(b, (_MOUSEEVENTF_LEFTDOWN, _MOUSEEVENTF_LEFTUP))
        await self.move(x1, y1)
        self._mouse_event(down)
        steps = max(2, int(steps))
        for i in range(1, steps + 1):
            t = i / steps
            await self.move(int(x1 + (x2 - x1) * t), int(y1 + (y2 - y1) * t))
            await asyncio.sleep(0.012)
        await asyncio.sleep(0.05)
        self._mouse_event(up)

    async def scroll(self, amount: int = 3, direction: str = "down", horizontal: bool = False) -> None:
        d = direction.strip().lower()
        n = abs(int(amount))
        if d in ("up", "left"):
            n = -n
        elif d not in ("down", "right"):
            raise ScreenAgentError("direction must be up/down (left/right when horizontal=True)")
        flag = _MOUSEEVENTF_HWHEEL if horizontal else _MOUSEEVENTF_WHEEL
        self._mouse_event(flag, data=n * _WHEEL_DELTA)
        await asyncio.sleep(0.05)

    # ---------------------------------------------------------------- keyboard
    def _key_event(self, vk: int, up: bool = False, extended: bool = False) -> None:
        flags = _KEYEVENTF_KEYUP if up else 0
        if extended:
            flags |= _KEYEVENTF_EXTENDEDKEY
        item = _INPUT(type=_INPUT_KEYBOARD)
        item.ki = _KEYBDINPUT(vk, 0, flags, 0, None)
        self._send([item])

    def _char_event(self, ch: str, up: bool) -> None:
        flags = _KEYEVENTF_UNICODE | (_KEYEVENTF_KEYUP if up else 0)
        item = _INPUT(type=_INPUT_KEYBOARD)
        item.ki = _KEYBDINPUT(0, ord(ch), flags, 0, None)
        self._send([item])

    async def type_text(self, text: str, interval: float = 0.008) -> int:
        for ch in text:
            self._char_event(ch, up=False)
            self._char_event(ch, up=True)
            if interval:
                await asyncio.sleep(interval)
        return len(text)

    async def key_press(self, combo: str) -> None:
        from ..keys import parse_combo  # noqa: PLC0415

        parsed = parse_combo(combo)
        held: list[int] = []
        extended = {"left", "up", "right", "down", "home", "end", "pageup", "pagedown",
                    "insert", "delete", "printscreen"}
        for mod in parsed.modifiers:
            vk = _VK.get(mod)
            if vk is None:
                raise UnsupportedOperation(f"modifier {mod!r} is not available on Windows")
            self._key_event(vk)
            held.append(vk)
            await asyncio.sleep(0.015)
        try:
            name = parsed.key.lower()
            if parsed.is_named and name in _VK:
                vk = _VK[name]
                is_ext = name in extended
                self._key_event(vk, extended=is_ext)
                await asyncio.sleep(0.02)
                self._key_event(vk, up=True, extended=is_ext)
            elif not parsed.is_named:
                self._char_event(parsed.key, up=False)
                self._char_event(parsed.key, up=True)
            else:
                raise UnsupportedOperation(f"key {parsed.key!r} is not supported on Windows")
        finally:
            for vk in reversed(held):
                self._key_event(vk, up=True)
                await asyncio.sleep(0.015)
