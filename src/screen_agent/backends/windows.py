"""Windows backend: GDI screen capture + ``user32.SendInput`` input injection.

Design notes, all of them things a screen agent gets wrong on its first try:

**No extra packages.** Capture goes ``CreateDC("DISPLAY")`` -> ``BitBlt`` into a 32-bit
DIB -> ``GetDIBits`` -> a PNG written by :mod:`screen_agent.imaging` (zlib from the
standard library). Pillow stays optional and is only used as a faster path for
``screenshot(scale=...)`` in :func:`screen_agent.core.scale_image`. Input is plain
``ctypes`` against ``user32``/``gdi32``, so ``pip install screen-agent`` is enough.

**DPI awareness is mandatory, not a nicety.** A process that is not DPI aware sees the
1536x864 *logical* image of a 1920x1080 display at 125% scaling, while ``SendInput`` and
the GDI screen DC are addressed in *physical* pixels - so every click lands ~20% away
from where the agent aimed. We switch the process to per-monitor-v2 awareness before
touching the screen, and ``doctor``/``screen_info`` report whether that worked.

**The desktop is the virtual screen, not the primary monitor.** ``SM_CXSCREEN`` only
covers the primary display; a second monitor placed to the left of it gives the desktop
a negative origin, and that is exactly the coordinate space ``GetCursorPos``,
``SetCursorPos`` and GDI all use. So does every other backend in this package.

**Batched SendInput.** Windows only serialises the *calls*, not the events: a
``MOUSEEVENTF_LEFTDOWN`` in one ``SendInput`` and its up in another can be separated by
real hardware input, and a drag silently turns into a click. Every gesture here is built
as one array and delivered in a single call.

**Recording still answers without ffmpeg.** Frames go through ffmpeg's ``rawvideo``
demuxer when it is on PATH (a real ``.webm``/``.mp4``); otherwise they are written as an
APNG, the one animated image format that needs no encoder library and plays in every
browser and image viewer.
"""

from __future__ import annotations

import asyncio
import ctypes
import ctypes.wintypes as wintypes
import math
import os
import re
import shutil
import tempfile
from pathlib import Path

from ..core import Rect, ScreenAgentError, UnsupportedOperation, run, scroll_delta
from ..imaging import bgra_to_rgb, encode_apng, encode_png
from .base import Backend, BackendInfo, Cursor

__all__ = ["WindowsBackend"]

# --------------------------------------------------------------------- constants
_INPUT_MOUSE = 0
_INPUT_KEYBOARD = 1

#: MOUSEINPUT.dwFlags
_MOUSEEVENTF_MOVE = 0x0001
_MOUSEEVENTF_LEFTDOWN = 0x0002
_MOUSEEVENTF_LEFTUP = 0x0004
_MOUSEEVENTF_RIGHTDOWN = 0x0008
_MOUSEEVENTF_RIGHTUP = 0x0010
_MOUSEEVENTF_MIDDLEDOWN = 0x0020
_MOUSEEVENTF_MIDDLEUP = 0x0040
_MOUSEEVENTF_XDOWN = 0x0080
_MOUSEEVENTF_XUP = 0x0100
_MOUSEEVENTF_WHEEL = 0x0800
_MOUSEEVENTF_HWHEEL = 0x1000
_MOUSEEVENTF_VIRTUALDESK = 0x4000  # absolute coordinates cover every monitor
_MOUSEEVENTF_ABSOLUTE = 0x8000

#: KEYBDINPUT.dwFlags
_KEYEVENTF_EXTENDEDKEY = 0x0001
_KEYEVENTF_KEYUP = 0x0002
_KEYEVENTF_UNICODE = 0x0004

#: GetSystemMetrics indices
_SM_CXSCREEN = 0
_SM_CYSCREEN = 1
_SM_CXCURSOR = 13
_SM_CYCURSOR = 14
_SM_XVIRTUALSCREEN = 76
_SM_YVIRTUALSCREEN = 77
_SM_CXVIRTUALSCREEN = 78
_SM_CYVIRTUALSCREEN = 79

#: gdi32
_SRCCOPY = 0x00CC0020
_CAPTUREBLT = 0x40000000  # also copy layered / DirectComposition windows
_DIB_RGB_COLORS = 0
_BI_RGB = 0
_HALFTONE = 4
_DI_NORMAL = 0x0003

_XBUTTON1 = 0x0001
_XBUTTON2 = 0x0002
_WHEEL_DELTA = 120

#: recording
_RECORD_FPS = 10.0
_RECORD_MAX_WIDTH = 1280
_APNG_FPS = 5.0
_APNG_MAX_WIDTH = 640

_VK: dict[str, int] = {
    # control keys
    "backspace": 0x08, "tab": 0x09, "clear": 0x0C, "return": 0x0D, "enter": 0x0D,
    "pause": 0x13, "capslock": 0x14, "kana": 0x15, "hangul": 0x15, "hanja": 0x19,
    "kanji": 0x19, "escape": 0x1B, "esc": 0x1B, "ime_convert": 0x1C,
    "ime_nonconvert": 0x1D, "ime_accept": 0x1E, "ime_mode": 0x1F, "space": 0x20,
    "pageup": 0x21, "pagedown": 0x22, "end": 0x23, "home": 0x24, "left": 0x25,
    "up": 0x26, "right": 0x27, "down": 0x28, "select": 0x29, "print": 0x2A,
    "execute": 0x2B, "printscreen": 0x2C, "insert": 0x2D, "delete": 0x2E, "help": 0x2F,
    # modifiers: the bare name is the left-hand key, as Windows itself reports it
    "shift": 0x10, "ctrl": 0x11, "control": 0x11, "alt": 0x12,
    "lshift": 0xA0, "rshift": 0xA1, "lctrl": 0xA2, "rctrl": 0xA3,
    "lalt": 0xA4, "ralt": 0xA5,
    "super": 0x5B, "meta": 0x5B, "win": 0x5B, "lwin": 0x5B, "rwin": 0x5C,
    "apps": 0x5D, "menu": 0x5D, "numlock": 0x90, "scrolllock": 0x91,
    # OEM punctuation, in US layout positions: key_press("minus") finds the -/_ key
    "semicolon": 0xBA, "plus": 0xBB, "equal": 0xBB, "comma": 0xBC, "minus": 0xBD,
    "period": 0xBE, "slash": 0xBF, "grave": 0xC0, "lbracket": 0xDB,
    "backslash": 0xDC, "rbracket": 0xDD, "apostrophe": 0xDE,
    # keypad
    "multiply": 0x6A, "add": 0x6B, "separator": 0x6C, "subtract": 0x6D,
    "decimal": 0x6E, "divide": 0x6F, "kpenter": 0x0D,
    # media and browser keys
    "volume_mute": 0xAD, "volume_down": 0xAE, "volume_up": 0xAF, "media_next": 0xB0,
    "media_prev": 0xB1, "media_stop": 0xB2, "media_play_pause": 0xB3,
    "launch_mail": 0xB4, "launch_media": 0xB5, "launch_app1": 0xB6, "launch_app2": 0xB7,
    "browser_back": 0xA6, "browser_forward": 0xA7, "browser_refresh": 0xA8,
    "browser_stop": 0xA9, "browser_search": 0xAA, "browser_favorites": 0xAB,
    "browser_home": 0xAC,
}
for _i in range(1, 25):
    _VK[f"f{_i}"] = 0x6F + _i
for _i in range(10):
    _VK[f"kp{_i}"] = 0x60 + _i

#: keys that need KEYEVENTF_EXTENDEDKEY, otherwise the numpad or the arrow cluster
#: next to the numeric keypad answers instead of the one next to the space bar
_EXTENDED_KEYS = frozenset(
    {
        "left", "right", "up", "down", "home", "end", "pageup", "pagedown",
        "insert", "delete", "numlock", "divide", "kpenter", "super", "meta", "win",
        "lwin", "rwin", "apps", "menu", "rctrl", "ralt", "browser_back",
        "browser_forward", "browser_refresh", "browser_stop", "browser_search",
        "browser_favorites", "browser_home",
    }
)

#: named keys that are really a character on the current keyboard layout. Resolving
#: them through ``VkKeyScanW`` means ``plus`` finds shift+= on a US layout and its own
#: key on a German one, instead of assuming US positions for the whole world.
_KEY_CHARS = {
    "minus": "-", "equal": "=", "plus": "+", "comma": ",", "period": ".",
    "slash": "/", "backslash": "\\", "semicolon": ";", "apostrophe": "'",
    "grave": "`", "lbracket": "[", "rbracket": "]",
}

#: name -> (down flag, up flag, mouseData): back/forward are the X buttons
_MOUSE_BUTTONS: dict[str, tuple[int, int, int]] = {
    "left": (_MOUSEEVENTF_LEFTDOWN, _MOUSEEVENTF_LEFTUP, 0),
    "right": (_MOUSEEVENTF_RIGHTDOWN, _MOUSEEVENTF_RIGHTUP, 0),
    "middle": (_MOUSEEVENTF_MIDDLEDOWN, _MOUSEEVENTF_MIDDLEUP, 0),
    "back": (_MOUSEEVENTF_XDOWN, _MOUSEEVENTF_XUP, _XBUTTON1),
    "forward": (_MOUSEEVENTF_XDOWN, _MOUSEEVENTF_XUP, _XBUTTON2),
}

#: shape -> (OCR_* slot, IDC_* resource). Every OCR_* slot's stock cursor has the same
#: numeric id as the slot, which is what :meth:`WindowsBackend._reset_cursors` uses.
_CURSOR_IDS: dict[str, tuple[int, int]] = {
    Cursor.DEFAULT: (0x0000, 32512),  # OCR_NORMAL   / IDC_ARROW
    Cursor.POINTER: (0x0000, 32512),
    Cursor.TEXT: (0x0001, 32513),  # OCR_IBEAM     / IDC_IBEAM
    Cursor.CROSSHAIR: (0x0002, 32515),  # OCR_CROSS    / IDC_CROSS
    Cursor.WAIT: (0x0003, 32514),  # OCR_WAIT     / IDC_WAIT
    Cursor.NOT_ALLOWED: (0x0008, 32648),  # OCR_NO        / IDC_NO
    Cursor.HAND: (0x000B, 32649),  # OCR_HAND     / IDC_HAND
    Cursor.N_RESIZE: (0x0004, 32642),  # OCR_SIZENWSE / IDC_SIZENWSE
    Cursor.S_RESIZE: (0x0005, 32645),  # OCR_SIZENS   / IDC_SIZENS
    Cursor.E_RESIZE: (0x000C, 32644),  # OCR_SIZEWE   / IDC_SIZEWE
    Cursor.W_RESIZE: (0x000C, 32644),
}

#: SetProcessDpiAwarenessContext contexts (negative handles, DPI_AWARENESS_CONTEXT_*)
_DPI_CONTEXTS = {4: "per-monitor-v2", 3: "per-monitor-v1", 2: "system"}
_dpi_mode: str | None = None  # a process can only be made aware once


# ------------------------------------------------------------------------ structs
class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),  # ULONG_PTR
    ]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT)]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


class _CURSORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("hCursor", wintypes.HANDLE),
        ("ptScreenPos", wintypes.POINT),
    ]


class _ICONINFO(ctypes.Structure):
    _fields_ = [
        ("fIcon", wintypes.BOOL),
        ("xHotspot", wintypes.DWORD),
        ("yHotspot", wintypes.DWORD),
        ("hbmMask", wintypes.HBITMAP),
        ("hbmColor", wintypes.HBITMAP),
    ]


class _suppress_os_error:
    """``contextlib.suppress(OSError)`` for the GDI teardown calls that may fail."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: object) -> bool:
        return True


# --------------------------------------------------------------- library loading
def _load_user32():
    user32 = ctypes.WinDLL("user32", use_last_error=True)  # type: ignore[attr-defined]
    user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int]
    user32.SendInput.restype = wintypes.UINT
    user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
    user32.SetCursorPos.restype = wintypes.BOOL
    user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    user32.GetSystemMetrics.restype = ctypes.c_int
    user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    user32.GetCursorPos.restype = wintypes.BOOL
    user32.GetCursorInfo.argtypes = [ctypes.POINTER(_CURSORINFO)]
    user32.GetCursorInfo.restype = wintypes.BOOL
    user32.GetIconInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ICONINFO)]
    user32.GetIconInfo.restype = wintypes.BOOL
    user32.DrawIconEx.argtypes = [
        wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.HANDLE, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
    ]
    user32.DrawIconEx.restype = wintypes.BOOL
    # the second argument is a MAKEINTRESOURCE id - a pointer *value*, not a string
    user32.LoadCursorW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
    user32.LoadCursorW.restype = wintypes.HANDLE
    user32.SetSystemCursor.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    user32.SetSystemCursor.restype = wintypes.HANDLE
    user32.SetCursor.argtypes = [wintypes.HANDLE]
    user32.SetCursor.restype = wintypes.HANDLE
    user32.VkKeyScanW.argtypes = [wintypes.WCHAR]
    user32.VkKeyScanW.restype = ctypes.c_short
    user32.GetDesktopWindow.restype = wintypes.HWND
    return user32


def _load_gdi32():
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)  # type: ignore[attr-defined]
    gdi32.CreateDCW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                ctypes.c_void_p]
    gdi32.CreateDCW.restype = wintypes.HDC
    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.DeleteDC.argtypes = [wintypes.HDC]
    gdi32.BitBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                             ctypes.c_int, wintypes.HDC, ctypes.c_int, ctypes.c_int,
                             wintypes.DWORD]
    gdi32.BitBlt.restype = wintypes.BOOL
    gdi32.StretchBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                 ctypes.c_int, wintypes.HDC, ctypes.c_int, ctypes.c_int,
                                 ctypes.c_int, ctypes.c_int, wintypes.DWORD]
    gdi32.StretchBlt.restype = wintypes.BOOL
    gdi32.SetStretchBltMode.argtypes = [wintypes.HDC, ctypes.c_int]
    gdi32.SetStretchBltMode.restype = ctypes.c_int
    gdi32.SetBrushOrgEx.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_void_p]
    gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT,
                                wintypes.UINT, ctypes.c_void_p,
                                ctypes.POINTER(_BITMAPINFO), wintypes.UINT]
    gdi32.GetDIBits.restype = ctypes.c_int
    return gdi32


def _enable_dpi_awareness() -> str:
    """Make the process per-monitor DPI aware, once, and report what we got."""
    global _dpi_mode
    if _dpi_mode is not None:
        return _dpi_mode
    _dpi_mode = "unaware"
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)  # type: ignore[attr-defined]
    except OSError:  # pragma: no cover - Windows only
        return _dpi_mode
    try:
        for value, name in _DPI_CONTEXTS.items():
            # the values are pseudo-handles: DPI_AWARENESS_CONTEXT_* are negative ints
            if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-value)):
                _dpi_mode = name
                return _dpi_mode
    except AttributeError:  # pragma: no cover - Windows 8.0 and older
        pass
    try:  # Windows 8.1
        shcore = ctypes.WinDLL("shcore", use_last_error=True)  # type: ignore[attr-defined]
        if shcore.SetProcessDpiAwareness(2) == 0:  # PER_MONITOR_DPI_AWARE
            _dpi_mode = "per-monitor"
            return _dpi_mode
        if shcore.SetProcessDpiAwareness(1) == 0:  # SYSTEM_DPI_AWARE
            _dpi_mode = "system"
            return _dpi_mode
    except (AttributeError, OSError):  # pragma: no cover
        pass
    try:  # Vista through Windows 7
        if user32.SetProcessDPIAware():
            _dpi_mode = "system"
    except AttributeError:  # pragma: no cover
        pass
    return _dpi_mode


# ------------------------------------------------------------- input event builders
def _mouse_event(flags: int, data: int = 0, dx: int = 0, dy: int = 0) -> _INPUT:
    item = _INPUT(type=_INPUT_MOUSE)
    item.mi = _MOUSEINPUT(dx, dy, data & 0xFFFFFFFF, flags, 0, 0)
    return item


def _absolute_move(x: int, y: int, rect: Rect) -> _INPUT:
    """A pointer move to exact pixel *x*, *y* inside the virtual desktop *rect*.

    Absolute motion is used everywhere instead of ``MOUSEEVENTF_MOVE`` deltas: injected
    relative moves go through the pointer acceleration curve, so a 18 pixel step can
    arrive as 74 and a drag lands nowhere near where it was aimed. Absolute coordinates
    are normalised to 0..65535 over the virtual desktop, which is also what makes a
    monitor left of the primary one work.
    """
    span_x = max(1, rect.w - 1)
    span_y = max(1, rect.h - 1)
    nx = round((x - rect.x) * 65535 / span_x)
    ny = round((y - rect.y) * 65535 / span_y)
    flags = _MOUSEEVENTF_MOVE | _MOUSEEVENTF_ABSOLUTE | _MOUSEEVENTF_VIRTUALDESK
    return _mouse_event(flags, 0, dx=max(0, min(65535, nx)), dy=max(0, min(65535, ny)))


def _key_event(vk: int, *, up: bool = False, extended: bool = False) -> _INPUT:
    flags = _KEYEVENTF_KEYUP if up else 0
    if extended:
        flags |= _KEYEVENTF_EXTENDEDKEY
    item = _INPUT(type=_INPUT_KEYBOARD)
    item.ki = _KEYBDINPUT(vk, 0, flags, 0, 0)
    return item


def _char_event(ch: str, *, up: bool = False) -> _INPUT:
    """A literal character, delivered as a UTF-16 code unit (layout independent)."""
    item = _INPUT(type=_INPUT_KEYBOARD)
    item.ki = _KEYBDINPUT(
        0, ord(ch) & 0xFFFF, _KEYEVENTF_UNICODE | (_KEYEVENTF_KEYUP if up else 0), 0, 0
    )
    return item


def _utf16_units(ch: str) -> tuple[str, ...]:
    """UTF-16 code units of *ch*: one below U+10000, a surrogate pair above."""
    code = ord(ch)
    if code <= 0xFFFF:
        return (ch,)
    code -= 0x10000
    return chr(0xD800 + (code >> 10)), chr(0xDC00 + (code & 0x3FF))


class WindowsBackend(Backend):
    """Windows 10/11 interactive desktop session."""

    name = "windows"
    display_name = "Windows (GDI capture + user32 SendInput)"

    def __init__(self) -> None:
        super().__init__()
        self._user32 = None
        self._gdi32 = None
        self._notes: list[str] = []
        self._cursor_shape: str | None = None
        self._cursor_slots: set[int] = set()
        self._encoders: set[str] | None = None
        # must happen before the first metric or GDI call, not lazily on first capture
        self._dpi = _enable_dpi_awareness()

    # -------------------------------------------------------------------- detect
    @classmethod
    def detect(cls) -> str:
        if os.environ.get("SCREEN_AGENT_BACKEND"):
            return ""
        if os.name != "nt":
            return "not Windows"
        try:
            user32 = _load_user32()
        except (OSError, AttributeError) as exc:
            return f"user32 unavailable: {exc}"
        # session 0 (a service) has a window station with no visible desktop: GDI would
        # return black and injected input would go nowhere
        if not user32.GetDesktopWindow():
            return "no interactive desktop (session 0, or the RDP session is disconnected)"
        return ""

    def _u32(self):
        if self._user32 is None:
            self._user32 = _load_user32()
        return self._user32

    def _gdi(self):
        if self._gdi32 is None:
            self._gdi32 = _load_gdi32()
        return self._gdi32

    # ------------------------------------------------------------------ geometry
    def _query_desktop(self) -> Rect:
        u32 = self._u32()
        # the virtual screen, not SM_CXSCREEN: a monitor left of the primary one makes
        # the desktop origin negative, and that is the space GDI and SetCursorPos use
        x = int(u32.GetSystemMetrics(_SM_XVIRTUALSCREEN))
        y = int(u32.GetSystemMetrics(_SM_YVIRTUALSCREEN))
        w = int(u32.GetSystemMetrics(_SM_CXVIRTUALSCREEN))
        h = int(u32.GetSystemMetrics(_SM_CYVIRTUALSCREEN))
        if w <= 0 or h <= 0:  # some RDP/VM configurations report only the primary screen
            w = int(u32.GetSystemMetrics(_SM_CXSCREEN))
            h = int(u32.GetSystemMetrics(_SM_CYSCREEN))
            x = y = 0
        if w <= 0 or h <= 0:
            w, h = 1920, 1080
            self._notes.append("GetSystemMetrics reported no screen; assuming 1920x1080")
        return Rect(x, y, w, h)

    async def info(self) -> BackendInfo:
        try:
            import PIL  # noqa: F401, PLC0415

            pillow = True
        except ImportError:
            pillow = False
        ffmpeg = shutil.which("ffmpeg")
        notes = list(self._notes)
        if self._dpi in ("unaware", "unavailable"):
            notes.append(
                "this process could not become DPI aware: reported coordinates are "
                "logical pixels while injected input uses physical ones, so clicks may "
                "land off target on a scaled display"
            )
        else:
            notes.append(f"DPI awareness: {self._dpi} (coordinates are physical pixels)")
        notes.append(
            "screen_record uses ffmpeg to write a video file"
            if ffmpeg
            else "ffmpeg is not on PATH, so screen_record writes an animated PNG"
        )
        notes.append(
            "must run in the interactive desktop session: the UAC secure desktop and "
            "windows elevated above this process ignore injected input"
        )
        notes.append(
            "a cursor shape set by mouse_move stays until it is changed again, unless "
            "the session refuses to change cursors at all"
        )
        return BackendInfo(
            backend=self.name,
            platform="win32",
            session=self._env_session(),
            desktop=self.desktop,
            capabilities={
                "screenshot": True,
                "record": True,
                "mouse": True,
                "keyboard": True,
                "absolute_pointer": True,
            },
            tools={"ffmpeg": ffmpeg, "Pillow": "yes" if pillow else "MISSING"},
            notes=notes,
        )

    # ------------------------------------------------------------------- capture
    async def capture_png(self, path: Path, rect: Rect, cursor: bool = True) -> tuple[int, int]:
        def grab() -> tuple[int, int]:
            # a stale file from an interrupted capture would otherwise end up in the PNG
            with _suppress_os_error():
                path.unlink()
            width, height, rgb = self._grab_rgb(rect, cursor=cursor)
            path.write_bytes(encode_png(width, height, rgb))
            return width, height

        try:
            return await asyncio.to_thread(grab)
        except OSError as exc:
            raise ScreenAgentError(
                f"screen capture failed: {exc} (a locked or disconnected session "
                f"fails outright or returns black)"
            ) from exc

    def _grab_rgb(
        self, rect: Rect, *, cursor: bool = False, max_width: int = 0
    ) -> tuple[int, int, bytes]:
        """Capture *rect* and return top-down ``(width, height, rgb)``."""
        out_w, out_h = self._fit(rect, max_width)
        bgra = self._grab_bgra(rect, out_w, out_h, cursor=cursor)
        return out_w, out_h, bgra_to_rgb(bgra)

    @staticmethod
    def _fit(rect: Rect, max_width: int) -> tuple[int, int]:
        """Target size for a capture of *rect*, downscaled in GDI when asked."""
        width, height = max(1, int(rect.w)), max(1, int(rect.h))
        if 0 < max_width < width:
            height = max(1, round(height * max_width / width))
            width = max_width
        return width, height

    def _clip(self, rect: Rect, out_w: int, out_h: int) -> tuple[Rect, int, int]:
        """Cut *rect* down to the desktop, shrinking the target size to match.

        The region parser already clamps, but a caller can pass anything, and a
        ``BitBlt`` from off-screen coordinates either fails or copies garbage.
        """
        desktop = self.desktop
        left = max(int(rect.x), desktop.x)
        top = max(int(rect.y), desktop.y)
        right = min(int(rect.x) + int(rect.w), desktop.x + desktop.w)
        bottom = min(int(rect.y) + int(rect.h), desktop.y + desktop.h)
        if right <= left or bottom <= top:
            raise ScreenAgentError(f"{rect} is entirely outside the desktop {desktop}")
        if (left, top) == (rect.x, rect.y) and (right - left, bottom - top) == (rect.w, rect.h):
            return rect, out_w, out_h
        scale_x = out_w / max(1, int(rect.w))
        scale_y = out_h / max(1, int(rect.h))
        return (
            Rect(left, top, right - left, bottom - top),
            max(1, round((right - left) * scale_x)),
            max(1, round((bottom - top) * scale_y)),
        )

    def _grab_bgra(self, rect: Rect, out_w: int, out_h: int, *, cursor: bool = False) -> bytes:
        """``BitBlt`` (or ``StretchBlt``) a region out of the screen DC as BGRA bytes."""
        rect, out_w, out_h = self._clip(rect, out_w, out_h)
        gdi = self._gdi()
        src_dc = gdi.CreateDCW("DISPLAY", None, None, None)
        if not src_dc:
            raise ScreenAgentError(
                "could not open the display device context; the session may have no "
                "visible desktop (service, locked screen, disconnected RDP)"
            )
        try:
            mem_dc = gdi.CreateCompatibleDC(src_dc)
            if not mem_dc:
                raise ScreenAgentError("CreateCompatibleDC failed (out of GDI handles?)")
            try:
                bitmap = gdi.CreateCompatibleBitmap(src_dc, out_w, out_h)
                if not bitmap:
                    raise ScreenAgentError(
                        f"CreateCompatibleBitmap failed for {out_w}x{out_h}"
                    )
                previous = gdi.SelectObject(mem_dc, bitmap)
                try:
                    scaled = (out_w, out_h) != (int(rect.w), int(rect.h))
                    if scaled:
                        # HALFTONE keeps text legible in a downscaled recording
                        gdi.SetStretchBltMode(mem_dc, _HALFTONE)
                        gdi.SetBrushOrgEx(mem_dc, 0, 0, None)
                        ok = gdi.StretchBlt(
                            mem_dc, 0, 0, out_w, out_h, src_dc,
                            rect.x, rect.y, rect.w, rect.h, _SRCCOPY | _CAPTUREBLT,
                        )
                    else:
                        ok = gdi.BitBlt(
                            mem_dc, 0, 0, out_w, out_h, src_dc, rect.x, rect.y,
                            _SRCCOPY | _CAPTUREBLT,
                        )
                    if not ok:
                        raise ScreenAgentError(
                            f"{'StretchBlt' if scaled else 'BitBlt'} failed for {rect} "
                            f"(error {ctypes.get_last_error()})"
                        )
                    if cursor:
                        self._draw_cursor(mem_dc, rect, out_w, out_h)
                    header = _BITMAPINFOHEADER()
                    header.biSize = ctypes.sizeof(header)
                    header.biWidth = out_w
                    header.biHeight = -out_h  # negative height: rows come out top-down
                    header.biPlanes = 1
                    header.biBitCount = 32
                    header.biCompression = _BI_RGB
                    info = _BITMAPINFO()
                    info.bmiHeader = header
                    buffer = (ctypes.c_ubyte * (out_w * out_h * 4))()
                    lines = gdi.GetDIBits(
                        mem_dc, bitmap, 0, out_h, buffer, ctypes.byref(info),
                        _DIB_RGB_COLORS,
                    )
                    if lines == 0:
                        raise ScreenAgentError(
                            f"GetDIBits returned nothing (error {ctypes.get_last_error()})"
                        )
                    return bytes(memoryview(buffer).cast("B"))
                finally:
                    gdi.SelectObject(mem_dc, previous)
                    gdi.DeleteObject(bitmap)
            finally:
                gdi.DeleteDC(mem_dc)
        finally:
            gdi.DeleteDC(src_dc)

    def _draw_cursor(self, mem_dc, rect: Rect, out_w: int, out_h: int) -> None:
        """Stamp the pointer onto the capture: GDI never draws it by itself.

        The pointer is a cosmetic extra, so every failure path here is silent: a capture
        without a visible pointer is still a correct capture.
        """
        u32 = self._u32()
        gdi = self._gdi()
        cursor_info = _CURSORINFO()
        cursor_info.cbSize = ctypes.sizeof(cursor_info)
        if not u32.GetCursorInfo(ctypes.byref(cursor_info)) or not cursor_info.hCursor:
            return
        info = _ICONINFO()
        if not u32.GetIconInfo(cursor_info.hCursor, ctypes.byref(info)):
            return
        try:
            width = int(u32.GetSystemMetrics(_SM_CXCURSOR)) or 32
            height = int(u32.GetSystemMetrics(_SM_CYCURSOR)) or 32
            if info.hbmColor:
                hot_x, hot_y = int(info.xHotspot), int(info.yHotspot)
            else:  # a monochrome cursor reports no hotspot: it is centred
                hot_x, hot_y = width // 2, height // 2
            x = int(round((cursor_info.ptScreenPos.x - rect.x - hot_x) * out_w / rect.w))
            y = int(round((cursor_info.ptScreenPos.y - rect.y - hot_y) * out_h / rect.h))
            # zero width/height means "use SM_CXCURSOR/SM_CYCURSOR", and a null mask DC
            # means the icon is drawn as-is, which is what a colour cursor needs
            u32.DrawIconEx(mem_dc, x, y, cursor_info.hCursor, 0, 0, 0, None, _DI_NORMAL)
        except OSError:  # pragma: no cover - the pointer is never worth an exception
            pass
        finally:
            for handle in (info.hbmColor, info.hbmMask):
                if handle:
                    with _suppress_os_error():
                        gdi.DeleteObject(handle)

    # ----------------------------------------------------------------- recording
    async def record_video(self, path: Path, seconds: float) -> Path:
        seconds = max(0.5, float(seconds))
        argv = await self._ffmpeg_argv(path)
        if argv is not None:
            try:
                return await self._record_ffmpeg(argv, seconds)
            except ScreenAgentError as exc:
                self._note_once(f"ffmpeg recording failed ({exc}); using APNG instead")
        return await self._record_apng(path.with_suffix(".apng"), seconds)

    async def _ffmpeg_argv(self, path: Path) -> list[str] | None:
        """The ffmpeg command for *path*, or ``None`` when ffmpeg cannot encode it."""
        if shutil.which("ffmpeg") is None:
            return None
        encoders = await self._ffmpeg_encoders()
        if not encoders:
            return None
        wanted = (
            ("", ".webm", ".mkv")
            if path.suffix.lower() in ("", ".webm", ".mkv")
            else (path.suffix.lower(),)
        )
        choice = next(
            (
                (codec, suffix)
                for codec, suffix in (
                    ("libvpx-vp9", ".webm"), ("libvpx", ".webm"), ("libx264", ".mp4"),
                    ("h264", ".mp4"), ("mpeg4", ".mp4"), ("libvpx", ".webm"),
                )
                if codec in encoders and suffix in wanted
            ),
            None,
        )
        if choice is None:
            return None
        codec, suffix = choice
        width, height = self._record_size(_RECORD_MAX_WIDTH)
        argv = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "rawvideo", "-pix_fmt", "bgra",  # what GDI hands us
            "-video_size", f"{width}x{height}",
            "-framerate", f"{_RECORD_FPS:g}",
            "-i", "-", "-an",
            "-vcodec", codec,
        ]
        if codec.startswith("libvpx"):
            argv += ["-b:v", "2500k", "-deadline", "realtime", "-cpu-used", "8"]
        else:
            argv += ["-preset", "ultrafast"]
        # yuv420p is what every browser and player wants, and it needs even dimensions
        argv += ["-pix_fmt", "yuv420p", str(path.with_suffix(suffix))]
        return argv

    async def _ffmpeg_encoders(self) -> set[str]:
        """Encoder names from ``ffmpeg -encoders`` (banner included, it goes to stderr)."""
        if self._encoders is None:
            try:
                rc, out, err = await run("ffmpeg", "-encoders", timeout=30)
            except ScreenAgentError:
                self._encoders = set()
                return self._encoders
            text = f"{out}\n{err}" if rc == 0 else ""
            # lines look like " V....D libvpx              libvpx VP8 (codec vp8)"
            self._encoders = set(re.findall(r"^\s*[A-Z.]{6}\s+([A-Za-z0-9_]+)", text, re.M))
        return self._encoders

    def _record_size(self, max_width: int) -> tuple[int, int]:
        """Even capture size for recording (H.264 and VP8 want even dimensions)."""
        desktop = self.desktop
        width = min(int(desktop.w), int(max_width))
        height = max(2, round(int(desktop.h) * width / max(1, int(desktop.w))))
        return max(2, width - width % 2), max(2, height - height % 2)

    async def _record_ffmpeg(self, argv: list[str], seconds: float) -> Path:
        """Feed GDI frames into ffmpeg's stdin as raw BGRA."""
        target = Path(argv[-1])
        width, height = self._record_size(_RECORD_MAX_WIDTH)
        frames = max(1, int(math.ceil(seconds * _RECORD_FPS)))
        loop = asyncio.get_running_loop()
        with tempfile.TemporaryFile() as log:
            proc = await asyncio.create_subprocess_exec(
                *argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL,
                stderr=log,
            )
            try:
                for index in range(frames):
                    started = loop.time()
                    # the GDI copy runs off the event loop, only the pipe is awaited
                    bgra = await asyncio.to_thread(
                        self._grab_bgra, self.desktop, width, height, cursor=True
                    )
                    if proc.stdin is None:  # pragma: no cover - defensive
                        raise ScreenAgentError("ffmpeg stdin is not a pipe")
                    proc.stdin.write(bgra)
                    await proc.stdin.drain()
                    delay = (1.0 / _RECORD_FPS) - (loop.time() - started)
                    if delay > 0 and index + 1 < frames:
                        await asyncio.sleep(delay)
                proc.stdin.close()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=20)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
                    raise ScreenAgentError(
                        "ffmpeg never finished writing the clip"
                    ) from None
            except (BrokenPipeError, ConnectionResetError) as exc:
                proc.kill()
                await proc.wait()
                raise ScreenAgentError(
                    _read_log(log) or f"ffmpeg stopped early: {exc}"
                ) from exc
            except BaseException:
                if proc.returncode is None:
                    with _suppress_os_error():
                        proc.kill()
                raise
            if not target.exists() or target.stat().st_size == 0:
                raise ScreenAgentError(_read_log(log) or "ffmpeg produced no output")
        return target

    async def _record_apng(self, target: Path, seconds: float) -> Path:
        """Fallback recorder: GDI frames straight into an animated PNG."""
        frames_count = max(1, int(math.ceil(seconds * _APNG_FPS)))
        width, height = self._record_size(_APNG_MAX_WIDTH)
        loop = asyncio.get_running_loop()
        frames: list[tuple[int, int, bytes]] = []
        for index in range(frames_count):
            started = loop.time()
            bgra = await asyncio.to_thread(
                self._grab_bgra, self.desktop, width, height, cursor=True
            )
            frames.append((width, height, bgra_to_rgb(bgra)))
            delay = (1.0 / _APNG_FPS) - (loop.time() - started)
            if delay > 0 and index + 1 < frames_count:
                await asyncio.sleep(delay)
        payload = await asyncio.to_thread(encode_apng, frames, _APNG_FPS)
        if target.exists():
            target.unlink()
        await asyncio.to_thread(target.write_bytes, payload)
        return target

    # ------------------------------------------------------------------- pointer
    def _send(self, items: list[_INPUT]) -> None:
        """Deliver one gesture in a single ``SendInput`` call - see the module docs."""
        if not items:
            return
        u32 = self._u32()
        array = (_INPUT * len(items))(*items)
        sent = u32.SendInput(len(items), array, ctypes.sizeof(_INPUT))
        if sent != len(items):
            raise ScreenAgentError(
                f"SendInput delivered {sent} of {len(items)} events "
                f"(error {ctypes.get_last_error()}): the target window is probably "
                f"elevated, on the UAC secure desktop, or in a session this process "
                f"cannot reach"
            )

    async def move(self, x: int, y: int, cursor: str = Cursor.DEFAULT) -> None:
        desktop = self.desktop
        x = self._clamp(int(x), desktop.x, desktop.x + desktop.w - 1)
        y = self._clamp(int(y), desktop.y, desktop.y + desktop.h - 1)
        if not self._u32().SetCursorPos(x, y):
            raise ScreenAgentError(
                f"SetCursorPos({x}, {y}) failed (error {ctypes.get_last_error()})"
            )
        self._apply_cursor_shape(cursor)
        await asyncio.sleep(0.01)

    async def move_by(self, dx: int, dy: int) -> None:
        """Relative move, expressed as an absolute one: injected deltas get accelerated."""
        point = wintypes.POINT()
        if not self._u32().GetCursorPos(ctypes.byref(point)):
            raise ScreenAgentError(f"GetCursorPos failed (error {ctypes.get_last_error()})")
        desktop = self.desktop
        x = self._clamp(point.x + int(dx), desktop.x, desktop.x + desktop.w - 1)
        y = self._clamp(point.y + int(dy), desktop.y, desktop.y + desktop.h - 1)
        self._send([_absolute_move(x, y, desktop)])
        await asyncio.sleep(0.01)

    async def click(
        self, x: int | None = None, y: int | None = None, button: str = "left", clicks: int = 1
    ) -> None:
        if x is not None and y is not None:
            await self.move(x, y)
        entry = _MOUSE_BUTTONS.get(button.strip().lower())
        if entry is None:
            raise ScreenAgentError(
                f"unknown button {button!r}; use left, middle, right, back or forward"
            )
        down, up, data = entry
        count = max(1, int(clicks))
        for index in range(count):
            # one batch per click, so a double click cannot straddle another gesture
            self._send([_mouse_event(down, data), _mouse_event(up, data)])
            if index + 1 < count:
                await asyncio.sleep(0.08)

    async def drag(
        self, x1: int, y1: int, x2: int, y2: int, button: str = "left", steps: int = 24
    ) -> None:
        entry = _MOUSE_BUTTONS.get(button.strip().lower())
        if entry is None:
            raise ScreenAgentError(f"unknown button {button!r}")
        down, up, data = entry
        desktop = self.desktop
        # home with SetCursorPos, then drag with absolute moves: an injected relative
        # move is run through the pointer acceleration curve and overshoots
        await self.move(x1, y1)
        self._send([_mouse_event(down, data)])
        steps = max(2, int(steps))
        for index in range(1, steps + 1):
            t = index / steps
            self._send([
                _absolute_move(
                    round(x1 + (x2 - x1) * t), round(y1 + (y2 - y1) * t), desktop
                )
            ])
            await asyncio.sleep(0.012)  # let the target process every WM_MOUSEMOVE
        self._send([_mouse_event(up, data)])

    async def scroll(self, amount: int = 3, direction: str = "down", horizontal: bool = False) -> None:
        notches = scroll_delta(int(amount), direction, horizontal)
        flag = _MOUSEEVENTF_HWHEEL if horizontal else _MOUSEEVENTF_WHEEL
        # one event per notch: Windows coalesces back-to-back wheel messages into a
        # single one with a bigger delta, and an application asked to scroll three notches
        # should see three notches
        for index in range(abs(notches)):
            delta = (1 if notches > 0 else -1) * _WHEEL_DELTA
            self._send([_mouse_event(flag, delta)])
            if index + 1 < abs(notches):
                await asyncio.sleep(0.02)
        await asyncio.sleep(0.05)

    # ------------------------------------------------------------------ keyboard
    @staticmethod
    def _modifier_vk(modifier: str) -> int:
        vk = _VK.get(modifier)
        if vk is None:
            raise UnsupportedOperation(
                f"modifier {modifier!r} has no Windows virtual key: fn and hyper are "
                f"handled by the firmware and never reach applications"
            )
        return vk

    async def type_text(self, text: str, interval: float = 0.008) -> int:
        """Type literal text, with real key presses for the characters that need one."""
        typed = 0
        for ch in text:
            if ch in ("\n", "\r"):
                self._send(_tap(_VK["return"]))
            elif ch == "\t":
                self._send(_tap(_VK["tab"]))
            elif ch == "\b":
                self._send(_tap(_VK["backspace"]))
            elif ord(ch) < 0x20:
                continue  # no other control character is typeable
            else:
                # KEYEVENTF_UNICODE carries UTF-16 units, so an emoji or any other
                # astral character has to go out as its surrogate pair
                for unit in _utf16_units(ch):
                    self._send([_char_event(unit), _char_event(unit, up=True)])
            typed += 1
            if interval:
                await asyncio.sleep(interval)
        return typed

    async def key_press(self, combo: str) -> None:
        from ..keys import parse_combo  # noqa: PLC0415

        parsed = parse_combo(combo)
        press: list[_INPUT] = []
        release: list[_INPUT] = []
        for modifier in parsed.modifiers:
            vk = self._modifier_vk(modifier)
            press.append(_key_event(vk, extended=modifier in _EXTENDED_KEYS))
        for modifier in reversed(parsed.modifiers):
            vk = self._modifier_vk(modifier)
            release.append(_key_event(vk, up=True, extended=modifier in _EXTENDED_KEYS))
        # modifiers down, main key, then everything back up - in one SendInput call so
        # nothing can be interleaved between them
        self._send([*press, *self._main_key_events(parsed), *release])
        await asyncio.sleep(0.02)

    def _main_key_events(self, parsed) -> list[_INPUT]:
        """Down/up events for the main key of a parsed combo."""
        from ..keys import MODIFIERS  # noqa: PLC0415

        name = parsed.key.lower()
        if not parsed.is_named:
            return self._character_events(parsed.key, parsed)
        char = _KEY_CHARS.get(name)
        if char is not None:
            # a named punctuation key means "type this character", so it is sent
            # literally: the physical bracket key is a dead key for a Vietnamese
            # layout and an accented letter for a French one
            return self._character_events(char, parsed, layout=False)
        if name in MODIFIERS:
            # a modifier as the main key: key_press("super") on its own
            return _tap(self._modifier_vk(name), extended=name in _EXTENDED_KEYS)
        vk = _VK.get(name)
        if vk is None:
            raise UnsupportedOperation(f"key {parsed.key!r} is not available on Windows")
        extended = name in _EXTENDED_KEYS
        return _tap(vk, extended=extended)

    def _character_events(self, char: str, parsed, *, layout: bool = True) -> list[_INPUT]:
        """Down/up events that type *char*.

        With *layout* the keyboard layout is asked which key produces the character, so
        ``ctrl+c`` becomes a real C key event. Without it the character is delivered
        literally as Unicode, which no keyboard layout can reinterpret.
        """
        scanned = self._u32().VkKeyScanW(char) if layout else -1
        if scanned != -1:
            vk = scanned & 0xFF
            state = (scanned >> 8) & 0xFF
            # With Alt held, "A" is a menu mnemonic rather than shift+A, so only honour
            # the shift the layout asked for when no Alt is in the combo.
            wanted: list[int] = []
            if state & 1 and "shift" not in parsed.modifiers and "alt" not in parsed.modifiers:
                wanted.append(_VK["shift"])
            if state & 2 and "ctrl" not in parsed.modifiers:
                wanted.append(_VK["ctrl"])
            if state & 4 and "alt" not in parsed.modifiers:
                # the layout wants AltGr, which is the *right* Alt key: ctrl+left-alt
                # is a different chord and produces a different character
                wanted.append(_VK["ralt"])
            return [
                *(_key_event(v, extended=v == _VK["ralt"]) for v in wanted),
                _key_event(vk),
                _key_event(vk, up=True),
                *(_key_event(v, up=True, extended=v == _VK["ralt"]) for v in reversed(wanted)),
            ]
        # not on this keyboard layout at all: deliver the character literally
        return [
            event
            for unit in _utf16_units(char)
            for event in (_char_event(unit), _char_event(unit, up=True))
        ]

    # -------------------------------------------------------------------- cursor
    def _apply_cursor_shape(self, shape: str) -> None:
        """Change the pointer shape, system-wide if the session allows it.

        ``SetSystemCursor`` is the only way to make a shape stick, but it is refused on
        some window stations and desktops, and it is never a good idea to point it at the
        stock arrow: Windows is free to consider that a replacement of the system
        cursor. So the default shape is only ever reached by *restoring* what was changed
        before, and ``SetCursor`` is the per-thread fallback when the system call is
        refused. The cursor argument is cosmetic, so a refusal is a note, not an error.
        """
        entry = _CURSOR_IDS.get(shape)
        if entry is None or shape == self._cursor_shape:
            return
        if shape in (Cursor.DEFAULT, Cursor.POINTER) and not self._cursor_slots:
            return  # already the system arrow, and nothing to put back
        u32 = self._u32()
        ocr, resource = entry
        if shape in (Cursor.DEFAULT, Cursor.POINTER) and self._cursor_slots:
            self._reset_cursors()
            return
        handle = u32.LoadCursorW(None, ctypes.c_void_p(resource))
        if not handle:
            self._note_once(f"LoadCursorW found no cursor for {shape!r}")
            return
        if u32.SetSystemCursor(handle, ocr):
            self._cursor_slots.add(ocr)
            self._cursor_shape = shape
            return
        applied = False
        with _suppress_os_error():
            applied = bool(u32.SetCursor(handle))
        if applied:
            self._cursor_shape = shape
            self._note_once(
                "the cursor shape only lasts until the next mouse move: this session "
                "refused SetSystemCursor"
            )
        else:
            self._note_once(
                "this session refused both SetSystemCursor and SetCursor, so cursor "
                "shapes are ignored"
            )

    def _note_once(self, message: str) -> None:
        if message not in self._notes:
            self._notes.append(message)

    def _reset_cursors(self) -> None:
        """Undo the system-wide cursor changes this backend made."""
        u32 = self._u32()
        for ocr in sorted(self._cursor_slots):
            # every OCR_* slot's stock cursor carries the same numeric id as the slot
            handle = u32.LoadCursorW(None, ctypes.c_void_p(ocr))
            if handle:
                with _suppress_os_error():
                    u32.SetSystemCursor(handle, ocr)
        self._cursor_slots.clear()
        self._cursor_shape = None

    async def close(self) -> None:
        self._reset_cursors()


def _tap(vk: int, *, extended: bool = False) -> list[_INPUT]:
    return [_key_event(vk, extended=extended), _key_event(vk, up=True, extended=extended)]


def _read_log(log) -> str:
    try:
        log.seek(0)
        return log.read().decode(errors="replace").strip()[:400]
    except OSError:  # pragma: no cover
        return ""
