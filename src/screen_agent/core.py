"""Shared primitives: geometry, region parsing, errors, small async process helpers."""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

__all__ = [
    "Rect",
    "ScreenAgentError",
    "UnsupportedOperation",
    "BackendUnavailable",
    "parse_region",
    "parse_region_arg",
    "have",
    "missing",
    "run",
    "run_capture",
    "scale_image",
    "default_shot_dir",
]


class ScreenAgentError(RuntimeError):
    """Base class for every error raised by screen-agent."""


class UnsupportedOperation(ScreenAgentError):
    """The active backend cannot do this (e.g. recording on a headless X session)."""


class BackendUnavailable(ScreenAgentError):
    """No backend matched this machine / no backend has the pieces it needs."""


@dataclass(frozen=True)
class Rect:
    """A pixel rectangle in desktop coordinates; (0, 0) is the top-left of the desktop."""

    x: int
    y: int
    w: int
    h: int

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return f"{self.w}x{self.h}+{self.x}+{self.y}"


def parse_region(text: str | None, desktop: Rect) -> Rect:
    """Parse ``'full'`` / ``'x,y,w,h'`` into a rect clamped to the desktop."""
    if not text or text.strip().lower() in ("full", "all", "screen", "desktop"):
        return desktop
    parts = [p for p in text.replace(" ", ",").split(",") if p]
    if len(parts) != 4:
        raise ScreenAgentError(
            f"invalid region {text!r}: use 'full' or 'x,y,w,h' (e.g. '0,0,800,600')"
        )
    try:
        x, y, w, h = (int(round(float(p))) for p in parts)
    except ValueError as exc:
        raise ScreenAgentError(f"invalid region {text!r}: numbers expected") from exc
    if w <= 0 or h <= 0:
        raise ScreenAgentError(f"invalid region {text!r}: width and height must be > 0")
    x = max(desktop.x, min(x, desktop.x + desktop.w - 1))
    y = max(desktop.y, min(y, desktop.y + desktop.h - 1))
    w = max(1, min(w, desktop.x + desktop.w - x))
    h = max(1, min(h, desktop.y + desktop.h - y))
    return Rect(x, y, w, h)


#: the CLI flags keep the shell-friendly ``x,y,w,h`` spelling
parse_region_arg = parse_region


def have(*tools: str) -> list[str]:
    """Return the subset of *tools* that are not on PATH."""
    return [t for t in tools if shutil.which(t) is None]


def missing(*tools: str) -> list[str]:
    return [t for t in tools if shutil.which(t) is None and t]


async def run(
    *argv: str | os.PathLike[str],
    timeout: float | None = 30.0,
    check: bool = False,
) -> tuple[int, str, str]:
    """Run a command without blocking the event loop. Returns (rc, stdout, stderr)."""
    argv = [str(a) for a in argv]
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise ScreenAgentError(f"{argv[0]}: command not found") from exc
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        with contextlib_suppress():
            proc.kill()
        out, err = await proc.communicate()
        raise ScreenAgentError(
            f"{argv[0]} timed out after {timeout:g}s: {err.decode(errors='replace').strip()[:300]}"
        ) from None
    rc = proc.returncode or 0
    text_out = out.decode(errors="replace")
    text_err = err.decode(errors="replace")
    if check and rc != 0:
        raise ScreenAgentError(
            f"{' '.join(argv)} exited {rc}: {(text_err or text_out).strip()[:400]}"
        )
    return rc, text_out, text_err


class contextlib_suppress:
    """Tiny local helper so we do not import contextlib for one use."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: object) -> bool:
        return True


async def run_capture(argv: Sequence[str], deadline: float, poll: float = 0.05) -> None:
    """Run a producer command until *deadline* (seconds) then stop it, ignoring its output.

    Used for screen recorders, which keep running until they are interrupted.
    """
    proc = await asyncio.create_subprocess_exec(
        *[str(a) for a in argv],
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        await asyncio.sleep(deadline)
    finally:
        with contextlib_suppress():
            proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=10)
        except asyncio.TimeoutError:
            with contextlib_suppress():
                proc.kill()


def scale_image(path: Path, max_width: int) -> tuple[int, int]:
    """Downscale a PNG in place. Returns the (possibly unchanged) size."""
    if max_width <= 0:
        return _png_size(path)
    try:
        from PIL import Image  # noqa: PLC0415
    except ImportError:
        return _png_size(path)
    with Image.open(path) as im:
        if im.width <= max_width:
            return im.size
        ratio = max_width / im.width
        size = (int(max_width), max(1, round(im.height * ratio)))
        im = im.convert("RGB").resize(size)
        im.save(path)
        return im.size


def _png_size(path: Path) -> tuple[int, int]:
    """Read the size out of a PNG header without any third-party library."""
    with open(path, "rb") as fh:
        head = fh.read(24)
    if len(head) < 24 or head[:8] != b"\x89PNG\r\n\x1a\n":
        raise ScreenAgentError(f"{path} is not a PNG file")
    return int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")


def default_shot_dir() -> Path:
    base = os.environ.get("SCREEN_AGENT_DIR")
    if base:
        return Path(base).expanduser()
    if sys.platform == "win32":
        root = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return Path(root) / "screen-agent"
    return Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")) / "screen-agent"
