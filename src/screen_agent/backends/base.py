"""Backend interface: one async API for screenshots, recording, pointer and keyboard."""

from __future__ import annotations

import abc
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from ..core import Rect, ScreenAgentError, UnsupportedOperation, scale_image

__all__ = ["Backend", "BackendInfo", "Cursor"]


class Cursor(str):
    """Pointer shape for :meth:`Backend.move`. Not all backends can change it."""

    DEFAULT = "default"
    HAND = "hand"
    POINTER = "pointer"
    TEXT = "text"
    CROSSHAIR = "crosshair"
    WAIT = "wait"
    NOT_ALLOWED = "not-allowed"
    N_RESIZE = "n-resize"
    S_RESIZE = "s-resize"
    E_RESIZE = "e-resize"
    W_RESIZE = "w-resize"


@dataclass
class BackendInfo:
    """What a backend found out about this machine."""

    backend: str
    platform: str
    session: str
    desktop: Rect
    capabilities: dict[str, bool] = field(default_factory=dict)
    tools: dict[str, str | None] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def render(self) -> str:
        lines = [
            f"backend        : {self.backend} ({self.session} on {self.platform})",
            f"desktop        : {self.desktop.w}x{self.desktop.h} at "
            f"({self.desktop.x}, {self.desktop.y})",
        ]
        caps = ", ".join(
            f"{k}={'yes' if v else 'no'}" for k, v in sorted(self.capabilities.items())
        )
        if caps:
            lines.append(f"capabilities    : {caps}")
        tools = ", ".join(f"{k}={v or 'MISSING'}" for k, v in sorted(self.tools.items()) if k)
        if tools:
            lines.append(f"tools          : {tools}")
        lines.extend(f"note           : {n}" for n in self.notes)
        return "\n".join(lines)


class Backend(abc.ABC):  # noqa: B024 - a template base: every method has a default that raises
    """Base class for all backends.

    Subclasses must implement every method; raise
    :class:`~screen_agent.core.UnsupportedOperation` when an operation does not
    apply to their platform/session.
    """

    name: str = "base"
    display_name: str = "base backend"

    def __init__(self) -> None:
        self._desktop: Rect | None = None

    # ------------------------------------------------------------------ probing
    @classmethod
    def detect(cls) -> str:
        """Return a non-empty reason string when this backend can be used here."""
        return ""

    # ------------------------------------------------------------------ queries
    @property
    def desktop(self) -> Rect:
        """Desktop geometry in pixels (cached after the first call)."""
        if self._desktop is None:
            self._desktop = self._query_desktop()
        return self._desktop

    def _query_desktop(self) -> Rect:  # pragma: no cover - overridden
        raise UnsupportedOperation(f"{self.name}: cannot determine the screen size")

    async def info(self) -> BackendInfo:
        raise UnsupportedOperation(f"{self.name}: no info")

    async def close(self) -> None:
        """Release any long-lived resources (D-Bus connection, child processes)."""
        return None

    # ------------------------------------------------------------------ screen
    #: Age in milliseconds of the frame ``capture_scaled`` last reused, else 0. Written by
    #: the backend, read by the server layer to report whether a shot was fresh.
    last_capture_age_ms: int = 0

    async def capture_png(self, path: Path, rect: Rect, cursor: bool = True) -> tuple[int, int]:
        """Write a PNG of *rect* to *path*; return the image size."""
        raise UnsupportedOperation(f"{self.name}: screenshots are not supported")

    async def capture_scaled(
        self, path: Path, rect: Rect, *, max_width: int = 0, cursor: bool = True
    ) -> tuple[int, int]:
        """Write a PNG of *rect* to *path*, no wider than *max_width*; return its size.

        The default captures at full size and rescales the finished file, which is what
        every backend did before this method existed, so nothing changes by default. A
        backend that can shrink the pixels while it copies them - GDI's ``StretchBlt`` -
        overrides this and never encodes the big frame at all, which is several times
        faster.

        Always a ``(width, height)`` 2-tuple on every backend, with no exceptions: a
        caller holding a ``Backend`` must not have to know which one it got. A backend
        that reuses a cached frame leaves that frame's age in ``last_capture_age_ms``
        instead of widening the return value, because a subclass that answers a
        different shape from the class it inherits is a trap for every future caller.
        """
        width, height = await self.capture_png(path, rect, cursor=cursor)
        if max_width > 0:
            return scale_image(path, max_width)
        return width, height

    async def screen_state(self) -> dict:
        """What is on the desktop, as facts rather than pixels.

        The cheap alternative to a screenshot: enough structure to decide *where* to
        look, without paying for a capture or pushing megabytes of image through the
        agent. Backends that can answer this from their own display server implement
        it; the rest raise, exactly like every other operation they cannot do.
        """
        raise UnsupportedOperation(f"{self.name}: no screen state without a capture")

    async def record_video(self, path: Path, seconds: float) -> Path:
        raise UnsupportedOperation(f"{self.name}: screen recording is not supported")

    # ----------------------------------------------------------------- pointer
    async def move(self, x: int, y: int, cursor: str = Cursor.DEFAULT) -> None:
        raise UnsupportedOperation(f"{self.name}: mouse control is not supported")

    async def move_by(self, dx: int, dy: int) -> None:
        raise UnsupportedOperation(f"{self.name}: mouse control is not supported")

    async def click(
        self, x: int | None = None, y: int | None = None, button: str = "left", clicks: int = 1
    ) -> None:
        raise UnsupportedOperation(f"{self.name}: mouse control is not supported")

    async def drag(
        self, x1: int, y1: int, x2: int, y2: int, button: str = "left", steps: int = 24
    ) -> None:
        raise UnsupportedOperation(f"{self.name}: mouse control is not supported")

    async def scroll(self, amount: int = 3, direction: str = "down", horizontal: bool = False) -> None:
        raise UnsupportedOperation(f"{self.name}: scrolling is not supported")

    # ---------------------------------------------------------------- keyboard
    async def type_text(self, text: str, interval: float = 0.008) -> int:
        raise UnsupportedOperation(f"{self.name}: typing is not supported")

    async def key_press(self, combo: str) -> None:
        raise UnsupportedOperation(f"{self.name}: key presses are not supported")

    # ------------------------------------------------------------------ shared
    @staticmethod
    def _clamp(value: int, low: int, high: int) -> int:
        return max(low, min(int(value), high))

    def _check_ready(self) -> None:
        if not self.detect():
            raise ScreenAgentError(f"{self.name}: not usable on this machine")

    @staticmethod
    def _env_session() -> str:
        if os.name == "nt":
            return "windows"
        if sys.platform == "darwin":
            return "aqua"
        wayland = os.environ.get("WAYLAND_DISPLAY")
        x11 = os.environ.get("DISPLAY")
        if wayland and x11:
            return "wayland+x11"
        if wayland:
            return "wayland"
        if x11:
            return "x11"
        return "headless"
