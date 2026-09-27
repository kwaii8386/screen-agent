"""X11 backend: xdotool for input, and a chain of capture tools for screenshots.

Capture tries, in order: ``mss`` (pure python, fast), ``import`` (ImageMagick),
``ffmpeg`` (x11grab) and ``scrot``/``maim``.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from pathlib import Path

from ..core import Rect, ScreenAgentError, UnsupportedOperation, run, run_capture
from .base import Backend, BackendInfo, Cursor

__all__ = ["X11Backend"]

_XDOJOOL_CURSORS = {
    Cursor.DEFAULT: "default",
    Cursor.HAND: "hand2",
    Cursor.POINTER: "hand2",
    Cursor.TEXT: "xterm",
    Cursor.CROSSHAIR: "crosshair",
    Cursor.WAIT: "wait",
    Cursor.NOT_ALLOWED: "not-allowed",
    Cursor.N_RESIZE: "sb_h_double_arrow",
    Cursor.S_RESIZE: "sb_v_double_arrow",
    Cursor.E_RESIZE: "sb_h_double_arrow",
    Cursor.W_RESIZE: "sb_v_double_arrow",
}


class X11Backend(Backend):
    """Classic X11 sessions (and XWayland-only setups)."""

    name = "x11"
    display_name = "X11 (xdotool + capture tools)"

    def __init__(self) -> None:
        super().__init__()
        self._notes: list[str] = []

    @classmethod
    def detect(cls) -> str:
        if os.environ.get("SCREEN_AGENT_BACKEND"):
            return ""
        if not os.environ.get("DISPLAY"):
            return "DISPLAY is not set"
        if shutil.which("xdotool") is None:
            return "xdotool not found (apt install xdotool / brew install xdotool)"
        return ""

    # ---------------------------------------------------------------- geometry
    def _query_desktop(self) -> Rect:
        from .mutter import xrandr_desktop  # noqa: PLC0415

        rect = xrandr_desktop()
        return rect

    async def info(self) -> BackendInfo:
        capture = next(
            (t for t in ("mss", "import", "ffmpeg", "scrot", "maim") if shutil.which(t)),
            None,
        )
        caps = {
            "screenshot": capture is not None,
            "record": shutil.which("ffmpeg") is not None,
            "mouse": True,
            "keyboard": True,
            "absolute_pointer": True,
        }
        notes = list(self._notes)
        if capture is None:
            notes.append("no capture tool found: install mss (pip), ImageMagick or scrot")
        if shutil.which("ffmpeg") is None:
            notes.append("install ffmpeg to record video")
        return BackendInfo(
            backend=self.name,
            platform=os.uname().sysname.lower() if hasattr(os, "uname") else "posix",
            session=self._env_session(),
            desktop=self.desktop,
            capabilities=caps,
            tools={
                "xdotool": shutil.which("xdotool"),
                "mss": shutil.which("mss"),
                "import": shutil.which("import"),
                "ffmpeg": shutil.which("ffmpeg"),
                "scrot": shutil.which("scrot"),
            },
            notes=notes,
        )

    # ------------------------------------------------------------------ screen
    def _capture_tool(self) -> str:
        try:
            import mss  # noqa: F401,PLC0415

            return "mss"
        except ImportError:
            pass
        for tool in ("import", "ffmpeg", "scrot", "maim"):
            if shutil.which(tool):
                return tool
        raise UnsupportedOperation(
            "no screenshot tool available: pip install mss, or install ImageMagick / scrot / ffmpeg"
        )

    async def capture_png(self, path: Path, rect: Rect, cursor: bool = True) -> tuple[int, int]:
        from ..core import _png_size  # noqa: PLC0415

        tool = self._capture_tool()
        if path.exists():
            path.unlink()
        if tool == "mss":
            await self._capture_mss(path, rect)
        elif tool == "import":
            await run(
                "import", "-window", "root",
                "-crop", f"{rect.w}x{rect.h}+{rect.x}+{rect.y}",
                f"+repage", str(path),
                timeout=20,
            )
        elif tool == "ffmpeg":
            # x11grab wants a display geometry string, not a crop rect
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                tmp_path = tmp.name
            try:
                await run(
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "x11grab", "-video_size", f"{self.desktop.w}x{self.desktop.h}",
                    "-i", os.environ.get("DISPLAY", ":0"),
                    "-frames:v", "1", tmp_path,
                    timeout=20,
                )
                await _crop_with_pillow(tmp_path, path, rect)
            finally:
                Path(tmp_path).unlink(missing_ok=True)
        else:  # scrot / maim
            args = [tool, "-z"]
            if tool == "scrot":
                args += ["-o", str(path)]
            else:  # maim
                args += [f"-g{rect.w}x{rect.h}+{rect.x}+{rect.y}", str(path)]
            if tool == "scrot" and (rect.w, rect.h) != (self.desktop.w, self.desktop.h):
                # scrot cannot crop, so grab everything then crop
                with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                    tmp_path = tmp.name
                try:
                    await run("scrot", "-o", tmp_path, timeout=20)
                    await _crop_with_pillow(tmp_path, path, rect)
                finally:
                    Path(tmp_path).unlink(missing_ok=True)
            else:
                await run(*args, timeout=20)
        return _png_size(path)

    async def _capture_mss(self, path: Path, rect: Rect) -> None:
        def grab() -> None:
            import mss  # noqa: PLC0415

            with mss.mss() as sct:
                shot = sct.grab({"left": rect.x, "top": rect.y, "width": rect.w, "height": rect.h})
                mss.tools.to_png(shot.rgb, shot.size, output=str(path))

        import asyncio  # noqa: PLC0415

        await asyncio.to_thread(grab)

    async def record_video(self, path: Path, seconds: float) -> Path:
        if not shutil.which("ffmpeg"):
            raise UnsupportedOperation("install ffmpeg to record video")
        if path.exists():
            path.unlink()
        d = self.desktop
        await run_capture(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "x11grab", "-framerate", "20",
                "-video_size", f"{d.w}x{d.h}",
                "-i", os.environ.get("DISPLAY", ":0"),
                "-t", f"{max(0.5, seconds):g}",
                "-pix_fmt", "yuv420p", str(path),
            ],
            deadline=max(1.0, seconds) + 2.0,
        )
        if not path.exists() or path.stat().st_size == 0:
            raise ScreenAgentError("ffmpeg produced no output")
        return path

    # ----------------------------------------------------------------- pointer
    async def move(self, x: int, y: int, cursor: str = Cursor.DEFAULT) -> None:
        await run("xdotool", "mousemove", "--sync", str(x), str(y))
        shape = _XDOJOOL_CURSORS.get(cursor)
        if shape and cursor != Cursor.DEFAULT:
            await run("xdotool", "mousemove", "--sync", str(x), str(y))
            # xdotool has no cursor-shape command; use xsetroot if available
            if shutil.which("xsetroot"):
                await run("xsetroot", "-cursor_name", shape)

    async def move_by(self, dx: int, dy: int) -> None:
        await run("xdotool", "mousemove_relative", "--sync", "--", str(dx), str(dy))

    async def click(
        self, x: int | None = None, y: int | None = None, button: str = "left", clicks: int = 1
    ) -> None:
        if x is not None and y is not None:
            await self.move(x, y)
        number = {"left": 1, "middle": 2, "right": 3}.get(button.strip().lower())
        if number is None:
            raise ScreenAgentError(f"unknown button {button!r}; use left, middle or right")
        for _ in range(max(1, int(clicks))):
            await run("xdotool", "click", "--repeat", "1", str(number))
            await asyncio_sleep(0.06)

    async def drag(
        self, x1: int, y1: int, x2: int, y2: int, button: str = "left", steps: int = 24
    ) -> None:
        number = {"left": 1, "middle": 2, "right": 3}.get(button.strip().lower(), 1)
        await run("xdotool", "mousemove", "--sync", str(x1), str(y1))
        await run("xdotool", "mousedown", str(number))
        await run(
            "xdotool", "mousemove_relative", "--sync", "--",
            str(int((x2 - x1) / max(2, steps))),
            str(int((y2 - y1) / max(2, steps))),
        )
        await run("xdotool", "mousemove", "--sync", str(x2), str(y2))
        await run("xdotool", "mouseup", str(number))

    async def scroll(self, amount: int = 3, direction: str = "down", horizontal: bool = False) -> None:
        button = "6" if horizontal else "5"  # 4/5 vertical, 6/7 horizontal
        d = direction.strip().lower()
        n = abs(int(amount))
        if d in ("up", "left"):
            button = "4" if not horizontal else "7"
        elif d not in ("down", "right"):
            raise ScreenAgentError("direction must be up/down (left/right when horizontal=True)")
        for _ in range(n):
            await run("xdotool", "click", "--repeat", "1", button)
            await asyncio_sleep(0.05)

    # ---------------------------------------------------------------- keyboard
    async def type_text(self, text: str, interval: float = 0.008) -> int:
        rc, _, err = await run("xdotool", "type", "--delay", str(int(interval * 1000) or 1), "--", text, timeout=60)
        if rc != 0:
            raise ScreenAgentError(f"xdotool type failed: {err.strip()[:200]}")
        return len(text)

    async def key_press(self, combo: str) -> None:
        from ..keys import parse_combo  # noqa: PLC0415

        parsed = parse_combo(combo)
        mapping = {
            "return": "Return", "tab": "Tab", "escape": "Escape", "space": "space",
            "backspace": "BackSpace", "delete": "Delete", "insert": "Insert",
            "home": "Home", "end": "End", "pageup": "Prior", "pagedown": "Next",
            "up": "Up", "down": "Down", "left": "Left", "right": "Right",
            "capslock": "Caps_Lock", "numlock": "Num_Lock", "scrolllock": "Scroll_Lock",
            "printscreen": "Print", "pause": "Pause", "menu": "Menu",
            "super": "super", "meta": "super", "ctrl": "ctrl", "alt": "alt", "shift": "shift",
        }
        parts = [mapping.get(m, m) for m in parsed.modifiers]
        if parsed.is_named:
            parts.append(mapping.get(parsed.key, parsed.key))
        else:
            parts.append(parsed.key)
        await run("xdotool", "key", "--clearmodifiers", "--", "+".join(parts))


async def asyncio_sleep(seconds: float) -> None:
    import asyncio  # noqa: PLC0415

    await asyncio.sleep(seconds)


async def _crop_with_pillow(src: str, dst: Path, rect: Rect) -> None:
    import asyncio  # noqa: PLC0415

    def crop() -> None:
        from PIL import Image  # noqa: PLC0415

        with Image.open(src) as im:
            im.crop((rect.x, rect.y, rect.x + rect.w, rect.y + rect.h)).convert("RGB").save(dst)

    await asyncio.to_thread(crop)
