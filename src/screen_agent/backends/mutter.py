"""GNOME / Wayland backend built on Mutter's own D-Bus API (no root, no portal dialogs).

Mutter ships two session-bus services that together give full screen access:

``org.gnome.Mutter.ScreenCast``
    ``CreateSession`` -> ``Session.Start`` -> ``Session.RecordArea`` -> ``Stream.Start``,
    then the ``PipeWireStreamAdded(node_id)`` signal tells us which PipeWire node to read.
    The node id goes straight to GStreamer's ``pipewiresrc`` and we encode a PNG or VP8.

``org.gnome.Mutter.RemoteDesktop``
    ``CreateSession`` -> ``Session.Start``, then ``NotifyPointerMotionRelative``,
    ``NotifyPointerButton``, ``NotifyPointerAxisDiscrete`` and ``NotifyKeyboardKeysym``
    inject genuine input events.

This is the only capture path on GNOME/Wayland that needs no privilege and shows no
permission dialog. The alternatives all fail on a real desktop:

* ``org.gnome.Shell.Screenshot`` -> ``AccessDenied: Screenshot is not allowed``
* XDG portal ``Screenshot`` -> consent dialog, and hangs for a plain CLI caller
* ``ffmpeg -f x11grab`` / ``import -window root`` -> see only XWayland clients (black)
* ``grim`` / ``sct`` -> wlroots protocols that Mutter does not implement

Implemented with ``dbus-next`` (pure python, asyncio native) so the D-Bus calls share the
MCP server's event loop instead of blocking a thread.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
from pathlib import Path

from ..core import Rect, ScreenAgentError, UnsupportedOperation, run_capture
from .base import Backend, BackendInfo, Cursor

__all__ = ["MutterBackend"]

SC_NAME = "org.gnome.Mutter.ScreenCast"
SC_ROOT = "/org/gnome/Mutter/ScreenCast"
SC_SESSION = "org.gnome.Mutter.ScreenCast.Session"
SC_STREAM = "org.gnome.Mutter.ScreenCast.Stream"
RD_NAME = "org.gnome.Mutter.RemoteDesktop"
RD_ROOT = "/org/gnome/Mutter/RemoteDesktop"
RD_SESSION = "org.gnome.Mutter.RemoteDesktop.Session"

_BTN = {"left": 0, "middle": 1, "right": 2, "back": 3, "forward": 4}

# X11 keysyms, which is what NotifyKeyboardKeysym expects
_KEYSYMS = {
    " ": 0x0020, "!": 0x0021, '"': 0x0022, "#": 0x0023, "$": 0x0024, "%": 0x0025,
    "&": 0x0026, "'": 0x0027, "(": 0x0028, ")": 0x0029, "*": 0x002A, "+": 0x002B,
    ",": 0x002C, "-": 0x002D, ".": 0x002E, "/": 0x002F, ":": 0x003A, ";": 0x003B,
    "<": 0x003C, "=": 0x003D, ">": 0x003E, "?": 0x003F, "@": 0x0040, "[": 0x005B,
    "\\": 0x005C, "]": 0x005D, "^": 0x005E, "_": 0x005F, "`": 0x0060, "{": 0x007B,
    "|": 0x007C, "}": 0x007D, "~": 0x007E,
    "minus": 0x002D, "equal": 0x003D, "plus": 0x002B, "comma": 0x002C, "period": 0x002E,
    "slash": 0x002F, "semicolon": 0x003B, "apostrophe": 0x0027, "grave": 0x0060,
    "backslash": 0x005C, "lbracket": 0x005B, "rbracket": 0x005D,
    "return": 0xFF0D, "tab": 0xFF09, "escape": 0xFF1B, "space": 0x0020,
    "backspace": 0xFF08, "delete": 0xFFFF, "insert": 0xFF63, "home": 0xFF50, "end": 0xFF57,
    "pageup": 0xFF55, "pagedown": 0xFF56, "up": 0xFF52, "down": 0xFF54, "left": 0xFF51,
    "right": 0xFF53, "capslock": 0xFFE5, "numlock": 0xFF7F, "scrolllock": 0xFF14,
    "printscreen": 0xFF61, "pause": 0xFF13, "menu": 0xFF67,
}
for _c in "0123456789":
    _KEYSYMS[_c] = ord(_c)
for _c in "abcdefghijklmnopqrstuvwxyz":
    _KEYSYMS[_c] = ord(_c)
    _KEYSYMS[_c.upper()] = ord(_c.upper())
for _i in range(1, 25):
    _KEYSYMS[f"f{_i}"] = 0xFFBD + _i
for _i in range(10):
    _KEYSYMS[f"kp{_i}"] = 0xFFB0 + _i

_MODS = {
    "ctrl": 0xFFE3, "alt": 0xFFE9, "shift": 0xFFE1, "super": 0xFFEB,
    "meta": 0xFFE7, "fn": 0xFFBE, "hyper": 0xFFED,
}

#: keysyms that are only reachable with Shift held on a US layout
_NEEDS_SHIFT = frozenset(
    {ord(c) for c in "~!@#$%^&*()_+{}|:\"<>?"} | {ord(c) for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"}
)

_CURSOR_SHAPES = {
    Cursor.DEFAULT: "default", Cursor.POINTER: "pointer", Cursor.HAND: "pointer",
    Cursor.TEXT: "text", Cursor.CROSSHAIR: "crosshair", Cursor.WAIT: "wait",
    Cursor.NOT_ALLOWED: "not-allowed", Cursor.N_RESIZE: "n-resize",
    Cursor.S_RESIZE: "s-resize", Cursor.E_RESIZE: "e-resize", Cursor.W_RESIZE: "w-resize",
}


def _has_gst() -> bool:
    return shutil.which("gst-launch-1.0") is not None


def xrandr_desktop() -> Rect:
    """Desktop geometry from XRandR (M Mutter also syncs the X server to the monitors)."""
    import subprocess  # noqa: PLC0415

    out = ""
    try:
        out = subprocess.run(
            ["xrandr", "--listmonitors"], capture_output=True, text=True, timeout=5
        ).stdout
    except (OSError, subprocess.SubprocessError):
        pass
    rects: list[Rect] = []
    for m in re.finditer(
        r"^\s*\d+:\s+\+?\*?\S+\s+(\d+)/\S+\s*x(\d+)/\S+\+(-?\d+)\+(-?\d+)", out, re.M
    ):
        w, h, x, y = (int(g) for g in m.groups())
        rects.append(Rect(x, y, w, h))
    if not rects:
        try:
            out = subprocess.run(["xrandr"], capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            out = ""
        m = re.search(r"current (\d+) x (\d+)", out)
        if m:
            return Rect(0, 0, int(m.group(1)), int(m.group(2)))
        return Rect(0, 0, 1920, 1080)
    return Rect(
        min(r.x for r in rects),
        min(r.y for r in rects),
        max(r.x + r.w for r in rects) - min(r.x for r in rects),
        max(r.y + r.h for r in rects) - min(r.y for r in rects),
    )


class MutterBackend(Backend):
    """GNOME on Wayland (or X11) through Mutter's D-Bus API."""

    name = "mutter"
    display_name = "GNOME / Mutter (D-Bus)"

    def __init__(self) -> None:
        super().__init__()
        self._bus = None
        self._rd = None  # (session_path, proxy_interface)
        self._lock = asyncio.Lock()
        self._notes: list[str] = []

    # ------------------------------------------------------------------ detect
    @classmethod
    def detect(cls) -> str:
        if os.environ.get("SCREEN_AGENT_BACKEND"):
            return ""
        if not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
            return "DBUS_SESSION_BUS_ADDRESS is not set (no session bus)"
        try:
            from dbus_next.aio import MessageBus  # noqa: F401,PLC0415
        except ImportError:
            return "dbus-next is not installed (pip install 'screen-agent[mutter]')"
        if not _has_gst():
            return "gst-launch-1.0 not found (apt install gstreamer1.0-tools)"
        if not os.environ.get("WAYLAND_DISPLAY") and not os.environ.get("DISPLAY"):
            return "neither WAYLAND_DISPLAY nor DISPLAY is set"
        return ""

    async def _connect(self):
        if self._bus is None:
            from dbus_next.aio import MessageBus  # noqa: PLC0415
            from dbus_next import BusType  # noqa: PLC0415

            self._bus = await MessageBus(bus_type=BusType.SESSION).connect()
        return self._bus

    async def _proxy(self, name: str, path: str, interface: str):
        bus = await self._connect()
        introspection = await bus.introspect(name, path)
        obj = bus.get_proxy_object(name, path, introspection)
        return obj.get_interface(interface)

    # ---------------------------------------------------------------- geometry
    def _query_desktop(self) -> Rect:
        return xrandr_desktop()

    async def info(self) -> BackendInfo:
        await self._ensure_rd()
        gst_ok = _has_gst()
        notes = list(self._notes)
        if not gst_ok:
            notes.append("install gstreamer1.0-tools for screenshots and recording")
        notes.append("absolute pointer motion is emulated: home the pointer, then move by delta")
        return BackendInfo(
            backend=self.name,
            platform=os.uname().sysname.lower() if hasattr(os, "uname") else "posix",
            session=self._env_session(),
            desktop=self.desktop,
            capabilities={
                "screenshot": gst_ok,
                "record": gst_ok,
                "mouse": True,
                "keyboard": True,
                "absolute_pointer": False,  # Mutter refuses the absolute variant
            },
            tools={"gst-launch-1.0": shutil.which("gst-launch-1.0")},
            notes=notes,
        )

    # ------------------------------------------------------------------ screen
    async def _open_stream(self, rect: Rect, cursor: bool):
        """Open a ScreenCast session + stream; return (session_iface, stream_iface, node_id)."""
        from dbus_next import Variant  # noqa: PLC0415

        iface = await self._proxy(SC_NAME, SC_ROOT, SC_NAME)
        session_path = await iface.call_create_session(
            {"session-handle-token": Variant("s", "screen-agent")}
        )
        session = await self._proxy(SC_NAME, session_path, SC_SESSION)
        await session.call_start()
        stream_path = await session.call_record_area(
            rect.x, rect.y, rect.w, rect.h,
            {"cursor-mode": Variant("s", "embedded" if cursor else "hidden")},
        )
        stream = await self._proxy(SC_NAME, stream_path, SC_STREAM)
        loop = asyncio.get_running_loop()
        node: asyncio.Future[int] = loop.create_future()
        stream.on_pipe_wire_stream_added(
            lambda nid: node.done() or node.set_result(int(nid))
        )
        await stream.call_start()
        try:
            node_id = await asyncio.wait_for(node, timeout=8)
        except asyncio.TimeoutError:
            await _safe_stop(stream, session)
            raise ScreenAgentError(
                "Mutter never reported a PipeWire stream (the compositor may be busy)"
            ) from None
        return session, stream, node_id

    async def capture_png(self, path: Path, rect: Rect, cursor: bool = True) -> tuple[int, int]:
        from ..core import _png_size  # noqa: PLC0415

        if not _has_gst():
            raise UnsupportedOperation("gst-launch-1.0 is required for screenshots")
        async with self._lock:
            if path.exists():
                path.unlink()
            session, stream, node_id = await self._open_stream(rect, cursor)
            try:
                await _gst_png(path, node_id)
            finally:
                await _safe_stop(stream, session)
        return _png_size(path)

    async def record_video(self, path: Path, seconds: float) -> Path:
        if not _has_gst():
            raise UnsupportedOperation("gst-launch-1.0 is required for recording")
        async with self._lock:
            if path.exists():
                path.unlink()
            session, stream, node_id = await self._open_stream(self.desktop, True)
            try:
                await _gst_webm(path, node_id, seconds)
            finally:
                await _safe_stop(stream, session)
        if not path.exists() or path.stat().st_size == 0:
            raise ScreenAgentError("the recorder produced no output")
        return path

    # ----------------------------------------------------------------- pointer
    async def _ensure_rd(self):
        if self._rd is None:
            iface = await self._proxy(RD_NAME, RD_ROOT, RD_NAME)
            path = await iface.call_create_session()
            session = await self._proxy(RD_NAME, path, RD_SESSION)
            await session.call_start()
            self._rd = (path, session)
        return self._rd[1]

    async def move(self, x: int, y: int, cursor: str = Cursor.DEFAULT) -> None:
        rd = await self._ensure_rd()
        bx, by, bw, bh = self.desktop
        x = self._clamp(x, bx, bx + bw - 1)
        y = self._clamp(y, by, by + bh - 1)
        if cursor and cursor != Cursor.DEFAULT:
            try:
                await rd.call_set_cursor_shape(_CURSOR_SHAPES.get(cursor, "default"))
            except Exception:  # noqa: BLE001, S110 - the shape is cosmetic
                pass
        # Mutter 46 refuses NotifyPointerMotionAbsolute ("No screen cast active"), so
        # home first: a huge negative delta is clamped to the desktop origin, which
        # makes the current pointer position known exactly.
        await rd.call_notify_pointer_motion_relative(-100000.0, -100000.0)
        await asyncio.sleep(0.03)
        await rd.call_notify_pointer_motion_relative(float(x - bx), float(y - by))
        await asyncio.sleep(0.02)

    async def move_by(self, dx: int, dy: int) -> None:
        rd = await self._ensure_rd()
        await rd.call_notify_pointer_motion_relative(float(dx), float(dy))
        await asyncio.sleep(0.02)

    async def click(
        self, x: int | None = None, y: int | None = None, button: str = "left", clicks: int = 1
    ) -> None:
        if x is not None and y is not None:
            await self.move(x, y)
        code = _BTN.get(button.strip().lower())
        if code is None:
            raise ScreenAgentError(f"unknown button {button!r}; use left, middle or right")
        rd = await self._ensure_rd()
        for i in range(max(1, int(clicks))):
            await rd.call_notify_pointer_button(code, True)
            await asyncio.sleep(0.035)
            await rd.call_notify_pointer_button(code, False)
            if i + 1 < max(1, int(clicks)):
                await asyncio.sleep(0.08)

    async def drag(
        self, x1: int, y1: int, x2: int, y2: int, button: str = "left", steps: int = 24
    ) -> None:
        code = _BTN.get(button.strip().lower(), 0)
        rd = await self._ensure_rd()
        await self.move(x1, y1)
        await rd.call_notify_pointer_button(code, True)
        steps = max(2, int(steps))
        dx, dy = (x2 - x1) / steps, (y2 - y1) / steps
        for _ in range(steps):
            await rd.call_notify_pointer_motion_relative(float(dx), float(dy))
            await asyncio.sleep(0.012)
        await asyncio.sleep(0.05)
        await rd.call_notify_pointer_button(code, False)

    async def scroll(self, amount: int = 3, direction: str = "down", horizontal: bool = False) -> None:
        rd = await self._ensure_rd()
        axis = 1 if horizontal else 0
        d = direction.strip().lower()
        n = abs(int(amount))
        if d in ("down", "right", "bottom"):
            n = -n
        elif d not in ("up", "left", "top"):
            raise ScreenAgentError("direction must be up/down (left/right when horizontal=True)")
        # Mutter follows the X11 convention of 120 units per wheel notch
        remaining, notch = n * 120, 120
        while abs(remaining) >= notch:
            await rd.call_notify_pointer_axis_discrete(axis, notch if remaining > 0 else -notch)
            remaining -= notch if remaining > 0 else -notch
            await asyncio.sleep(0.02)
        if remaining:
            await rd.call_notify_pointer_axis_discrete(axis, remaining)

    # ---------------------------------------------------------------- keyboard
    async def type_text(self, text: str, interval: float = 0.008) -> int:
        rd = await self._ensure_rd()
        typed = 0
        for ch in text:
            keysym = _KEYSYMS.get(ch)
            if keysym is None and ch in ("\n", "\r"):
                keysym = _KEYSYMS["return"]
            elif keysym is None and ch == "\t":
                keysym = _KEYSYMS["tab"]
            if keysym is None:
                continue
            # Upper-case letters and shifted symbols need Shift held; Mutter derives
            # the keycode from the keysym, so the modifier has to be explicit.
            shift = keysym in _NEEDS_SHIFT
            if shift:
                await rd.call_notify_keyboard_keysym(_MODS["shift"], True)
                await asyncio.sleep(0.01)
            await rd.call_notify_keyboard_keysym(keysym, True)
            await asyncio.sleep(0.01)
            await rd.call_notify_keyboard_keysym(keysym, False)
            if shift:
                await rd.call_notify_keyboard_keysym(_MODS["shift"], False)
            typed += 1
            if interval:
                await asyncio.sleep(interval)
        return typed

    async def key_press(self, combo: str) -> None:
        from ..keys import parse_combo  # noqa: PLC0415

        parsed = parse_combo(combo)
        rd = await self._ensure_rd()
        held: list[int] = []
        try:
            for mod in parsed.modifiers:
                keysym = _MODS.get(mod)
                if keysym is None:
                    raise UnsupportedOperation(f"modifier {mod!r} is unavailable on GNOME")
                await rd.call_notify_keyboard_keysym(keysym, True)
                held.append(keysym)
                await asyncio.sleep(0.015)
            # A main key may itself be a modifier (key_press("super") opens the
            # Activities overview), so look in both tables.
            keysym = _KEYSYMS.get(parsed.key) or _MODS.get(parsed.key)
            if keysym is None:
                raise ScreenAgentError(
                    f"no keysym for {parsed.key!r} on this keyboard layout"
                )
            await rd.call_notify_keyboard_keysym(keysym, True)
            await asyncio.sleep(0.02)
            await rd.call_notify_keyboard_keysym(keysym, False)
        finally:
            for keysym in reversed(held):
                await rd.call_notify_keyboard_keysym(keysym, False)
                await asyncio.sleep(0.015)

    # ----------------------------------------------------------------- teardown
    async def close(self) -> None:
        if self._rd is not None:
            try:
                await self._rd[1].call_stop()
            except Exception:  # noqa: BLE001, S110
                pass
            self._rd = None
        if self._bus is not None:
            try:
                self._bus.disconnect()
            except Exception:  # noqa: BLE001, S110
                pass
            self._bus = None


# --------------------------------------------------------------------- helpers
async def _safe_stop(stream, session) -> None:
    for iface in (stream, session):
        try:
            await iface.call_stop()
        except Exception:  # noqa: BLE001, S110 - best effort teardown
            pass


async def _gst_png(path: Path, node_id: int) -> None:
    # NOTE: the pipeline must be separate argv items with bare '!' separators.
    # Passing it as one quoted string makes gst-launch report a syntax error.
    argv = [
        "gst-launch-1.0", "-q",
        "pipewiresrc", f"path={node_id}", "do-timestamp=true", "always-copy=true",
        "!", "videoconvert", "!", "pngenc", "snapshot=true", "!",
        "filesink", f"location={path}",
    ]
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=25)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise ScreenAgentError("screenshot timed out after 25s") from None
    if not path.exists() or path.stat().st_size == 0:
        raise ScreenAgentError(
            f"screenshot failed (rc={proc.returncode}): "
            f"{out.decode(errors='replace').strip()[:400]}"
        )


async def _gst_webm(path: Path, node_id: int, seconds: float) -> None:
    argv = [
        "gst-launch-1.0", "-q",
        "pipewiresrc", f"path={node_id}", "do-timestamp=true", "always-copy=true",
        "!", "videoconvert", "!", "vp8enc", "deadline=1", "cpu-used=8",
        "!", "webmmux", "streamable=true", "!", "filesink", f"location={path}",
    ]
    await run_capture(argv, deadline=max(0.5, seconds))
