"""Batch desktop actions: run a list of steps in a single round trip.

The MCP surface exposes one tool per verb (``mouse_move``, ``mouse_click``,
``type_text``, ...), so a three-step interaction costs three round trips and three
model turns. :func:`run_actions` collapses a list of steps into one call, which
matters on the **non-browser fallback path**: when the agent has no exact element
reference and is working from pixels, it already knows what to click next, and the
extra observe-after-every-step turns are pure latency.

Its limits are worth being honest about:

* Batching **hides the observe-between-actions step**. A run of
  ``click, type, click`` fires without looking at the screen in between, so it is
  right for the pixel fallback path and wrong for a path that has real element
  references and can (and should) verify after each step.
* Steps are relative to whatever the *previous* step left behind. ``move_by`` and a
  ``click`` without coordinates depend on it, so a failed step can put the rest of
  the batch somewhere else than you planned. Read the per-step :class:`StepResult`
  list rather than assuming.
* This is a driver, not a planner. It does not decide anything; it does not retry;
  it does not roll back.
* Nothing here imports a backend module at module scope. The caller passes the
  backend in, so importing :mod:`screen_agent.control` never pulls in a platform
  driver and never touches the user's session on its own.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .core import ScreenAgentError, default_shot_dir, parse_region, scale_image

__all__ = [
    "BatchError",
    "MAX_WAIT_MS",
    "StepResult",
    "VALID_OPS",
    "run_actions",
]

#: Hard ceiling on a ``wait`` step. A client asking for ten minutes of sleep would
#: otherwise hold the single-threaded MCP server open for ten minutes. Oversized
#: requests are CLAMPED (not rejected) and the clamp is reported in the step detail.
MAX_WAIT_MS = 5000


@dataclass(frozen=True)
class StepResult:
    """Outcome of one step: what was asked, whether it worked, and why not."""

    index: int
    op: str
    action: Mapping[str, Any]
    ok: bool
    detail: str
    error_type: str | None = None
    error: str | None = None


class BatchError(ScreenAgentError):
    """A batch aborted on a failing step. Carries the results collected so far."""

    def __init__(self, message: str, results: Sequence[StepResult]) -> None:
        super().__init__(message)
        self.results = list(results)

    @property
    def failed_index(self) -> int | None:
        """Index of the step that aborted the batch, or ``None`` if none failed."""
        for result in self.results:
            if not result.ok:
                return result.index
        return None


# ------------------------------------------------------------------- arguments
def _as_int(action: Mapping[str, Any], key: str, default: int) -> int:
    value = action.get(key, default)
    if value is None:
        return default
    return int(value)


def _as_str(action: Mapping[str, Any], key: str, default: str) -> str:
    value = action.get(key, default)
    return default if value is None else str(value)


# --------------------------------------------------------------------- step ops
# Each entry takes the backend plus the step's own mapping and returns the detail
# string for the StepResult. Signatures mirror the per-verb MCP tools, so a batch
# step and the equivalent single tool call do exactly the same thing. The backend
# argument is `Any` on purpose: this module never imports a backend package, the
# caller hands one in.
_Handler = Callable[[Any, Mapping[str, Any]], Awaitable[str]]
_OPS: dict[str, _Handler] = {}


def _op(name: str) -> Callable[[_Handler], _Handler]:
    def register(handler: _Handler) -> _Handler:
        _OPS[name] = handler
        return handler

    return register


@_op("click")
async def _click(backend: Any, action: Mapping[str, Any]) -> str:
    # a negative coordinate means "wherever the pointer already is", the same
    # convention the mouse_click tool uses
    x, y = _as_int(action, "x", -1), _as_int(action, "y", -1)
    button = _as_str(action, "button", "left")
    clicks = _as_int(action, "clicks", 1)
    bx = x if x >= 0 else None
    by = y if y >= 0 else None
    await backend.click(bx, by, button=button, clicks=clicks)
    where = f" at ({bx}, {by})" if bx is not None and by is not None else ""
    return f"Clicked {button}{where} {clicks}x."


@_op("move")
async def _move(backend: Any, action: Mapping[str, Any]) -> str:
    x, y = _as_int(action, "x", 0), _as_int(action, "y", 0)
    cursor = _as_str(action, "cursor", "default")
    await backend.move(x, y, cursor=cursor)
    return f"Mouse is at ({x}, {y})."


@_op("move_by")
async def _move_by(backend: Any, action: Mapping[str, Any]) -> str:
    dx, dy = _as_int(action, "dx", 0), _as_int(action, "dy", 0)
    await backend.move_by(dx, dy)
    return f"Moved the mouse by ({dx}, {dy})."


@_op("drag")
async def _drag(backend: Any, action: Mapping[str, Any]) -> str:
    coords = tuple(_as_int(action, key, 0) for key in ("x1", "y1", "x2", "y2"))
    button = _as_str(action, "button", "left")
    steps = _as_int(action, "steps", 24)
    await backend.drag(*coords, button=button, steps=steps)
    return f"Dragged ({coords[0]},{coords[1]}) -> ({coords[2]},{coords[3]})."


@_op("scroll")
async def _scroll(backend: Any, action: Mapping[str, Any]) -> str:
    amount = _as_int(action, "amount", 3)
    direction = _as_str(action, "direction", "down")
    horizontal = bool(action.get("horizontal", False))
    await backend.scroll(amount, direction, horizontal)
    return f"Scrolled {direction} {amount}."


@_op("type")
async def _type(backend: Any, action: Mapping[str, Any]) -> str:
    text = _as_str(action, "text", "")
    # default 0, not 8: the gap between keystrokes is only wanted by callers who need a
    # visibly human pace. asyncio quantises every sleep to the 15.6 ms Windows timer
    # tick, so an 8 ms request is really 15.6 ms - 625 ms for a 40 character string.
    interval_ms = _as_int(action, "interval_ms", 0)
    count = await backend.type_text(text, interval=float(interval_ms) / 1000.0)
    return f"Typed {count} character(s): {text!r}"


@_op("key")
async def _key(backend: Any, action: Mapping[str, Any]) -> str:
    combo = _as_str(action, "keys", "") or _as_str(action, "combo", "")
    await backend.key_press(combo)
    return f"Pressed {combo}."


@_op("shot")
async def _shot(backend: Any, action: Mapping[str, Any]) -> str:
    target = _shot_path(_as_str(action, "path", ""))
    rect = parse_region(_as_str(action, "region", "full"), backend.desktop)
    cursor = bool(action.get("cursor", True))
    scale = _as_int(action, "scale", 0)
    size = await backend.capture_png(target, rect, cursor=cursor)
    if scale > 0:
        size = scale_image(target, scale)
    return f"Saved {target} ({size[0]}x{size[1]} px, region {rect})."


@_op("wait")
async def _wait(backend: Any, action: Mapping[str, Any]) -> str:
    requested = _as_int(action, "ms", _as_int(action, "timeout_ms", 0))
    requested = max(0, requested)
    capped = min(requested, MAX_WAIT_MS)
    await asyncio.sleep(capped / 1000.0)
    if capped != requested:
        return f"Waited {capped} ms (capped at {MAX_WAIT_MS} ms from {requested} ms)."
    return f"Waited {capped} ms."


def _shot_path(path: str, suffix: str = ".png") -> Path:
    """Resolve a caller-supplied screenshot path, or stamp one in the shot dir."""
    if path:
        target = Path(path).expanduser()
        if not target.is_absolute():
            target = Path.cwd() / target
        target.parent.mkdir(parents=True, exist_ok=True)
        return target if target.suffix else target.with_suffix(suffix)
    folder = default_shot_dir()
    folder.mkdir(parents=True, exist_ok=True)
    stamp = f"{time.strftime('%Y%m%d-%H%M%S')}-{int(time.time() * 1000) % 1000}"
    return folder / f"{stamp}{suffix}"


#: Every op :func:`run_actions` accepts, in the order the table registers them.
VALID_OPS: tuple[str, ...] = tuple(_OPS)


# ------------------------------------------------------------------ the executor
async def run_actions(
    backend: Any,
    actions: Sequence[Mapping[str, Any]],
    *,
    stop_on_error: bool = True,
) -> list[StepResult]:
    """Run *actions* in order against *backend*, one StepResult per step.

    *backend* is anything exposing the async backend surface (``desktop``,
    ``click``, ``move``, ``move_by``, ``drag``, ``scroll``, ``type_text``,
    ``key_press``, ``capture_png``). It is typed ``Any`` so that importing this
    module never imports a backend package.

    Every op is validated before the first step fires, so a typo in the last step
    cannot leave the first thirty already done.

    With ``stop_on_error=True`` (the default) the first failing step aborts the run
    and raises :class:`BatchError`, which carries the results collected up to and
    including that step. With ``stop_on_error=False`` every step is attempted and
    its outcome - success or failure - is reported individually, so a partial run is
    inspectable rather than lost.
    """
    steps = list(actions)
    # Validate every op before firing anything. A typo in the last step should not
    # be discovered after the first thirty have already moved the real mouse.
    for position, action in enumerate(steps):
        op = action.get("op")
        if not isinstance(op, str) or op not in _OPS:
            raise ScreenAgentError(
                f"step {position}: unknown op {op!r}; valid ops are {', '.join(VALID_OPS)}"
            )

    results: list[StepResult] = []
    for index, action in enumerate(steps):
        op = str(action["op"])
        # `Exception`, never `BaseException`: KeyboardInterrupt and
        # asyncio.CancelledError are BaseException subclasses, so Ctrl+C and task
        # cancellation keep propagating instead of being reported as step failures.
        try:
            detail = await _OPS[op](backend, action)
        except Exception as exc:
            result = StepResult(
                index=index,
                op=op,
                action=action,
                ok=False,
                detail=f"{op} failed: {exc}",
                error_type=type(exc).__name__,
                error=str(exc),
            )
            results.append(result)
            if stop_on_error:
                raise BatchError(
                    f"step {index} ({op}) failed: {type(exc).__name__}: {exc}", results
                ) from exc
        else:
            results.append(StepResult(index=index, op=op, action=action, ok=True, detail=detail))
    return results
