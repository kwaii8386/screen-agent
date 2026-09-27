"""Backend registry and auto-detection."""

from __future__ import annotations

import os

from ..core import BackendUnavailable
from .base import Backend, BackendInfo, Cursor

__all__ = ["Backend", "BackendInfo", "Cursor", "load_backend", "all_backends", "detect_backends"]


def all_backends() -> list[type[Backend]]:
    """Every backend that can be imported on this machine, in preference order."""
    found: list[type[Backend]] = []
    for module_name, class_name in (
        ("mutter", "MutterBackend"),  # GNOME on Wayland: best fidelity
        ("wayland_tools", "WaylandToolsBackend"),  # any Wayland + grim/ydotool
        ("x11", "X11Backend"),  # X11 sessions
        ("macos", "MacOSBackend"),
        ("windows", "WindowsBackend"),
    ):
        try:
            module = __import__(f"screen_agent.backends.{module_name}", fromlist=[class_name])
        except Exception:  # noqa: BLE001, PERF203 - a broken backend must not hide the rest
            continue
        cls = getattr(module, class_name, None)
        if cls is not None:
            found.append(cls)
    return found


def detect_backends() -> list[tuple[str, str]]:
    """Return ``(name, reason)`` for every backend: empty reason means usable."""
    out = []
    for cls in all_backends():
        try:
            reason = cls.detect()
        except Exception as exc:  # noqa: BLE001
            reason = f"probe failed: {exc}"
        out.append((cls.name, reason))
    return out


def load_backend(name: str | None = None) -> Backend:
    """Instantiate a backend by name, or auto-detect the best one.

    ``SCREEN_AGENT_BACKEND`` forces a specific backend, which is handy for testing.
    """
    wanted = name or os.environ.get("SCREEN_AGENT_BACKEND") or ""
    backends = all_backends()
    if not backends:
        raise BackendUnavailable("no backends could be imported")

    if wanted:
        for cls in backends:
            if cls.name == wanted:
                return cls()
        raise BackendUnavailable(
            f"unknown backend {wanted!r}; available: {', '.join(c.name for c in backends)}"
        )

    reasons: list[str] = []
    for cls in backends:
        reason = cls.detect()
        if not reason:
            return cls()
        reasons.append(f"{cls.name}: {reason}")
    raise BackendUnavailable(
        "no usable backend found for this session.\n  " + "\n  ".join(reasons)
    )
