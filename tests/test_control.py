"""Tests for the batch-action layer: dispatch, error policy, and the wait cap.

Everything runs against a fake backend that only records what it was asked to do.
No test in this file touches a real desktop: the point of the batch layer is that
it is a thin, testable dispatcher over whatever backend it is handed.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from screen_agent.control import (  # noqa: E402
    MAX_WAIT_MS,
    VALID_OPS,
    BatchError,
    StepResult,
    run_actions,
)
from screen_agent.core import Rect, ScreenAgentError  # noqa: E402

DESKTOP = Rect(0, 0, 1920, 1080)


class FakeBackend:
    """Records every call; optionally raises for the named methods."""

    name = "fake"

    def __init__(self, *, fail_on: set[str] | None = None) -> None:
        self._desktop = DESKTOP
        self.calls: list[tuple[str, tuple, dict]] = []
        self.fail_on = fail_on or set()

    @property
    def desktop(self) -> Rect:
        return self._desktop

    def _record(self, name: str, *args, **kwargs) -> None:
        self.calls.append((name, args, kwargs))
        if name in self.fail_on:
            raise RuntimeError(f"{name} is not supported here")

    def names(self) -> list[str]:
        return [name for name, _, _ in self.calls]

    async def click(self, x=None, y=None, button="left", clicks=1) -> None:
        self._record("click", x, y, button=button, clicks=clicks)

    async def move(self, x: int, y: int, cursor: str = "default") -> None:
        self._record("move", x, y, cursor=cursor)

    async def move_by(self, dx: int, dy: int) -> None:
        self._record("move_by", dx, dy)

    async def drag(self, x1, y1, x2, y2, button="left", steps=24) -> None:
        self._record("drag", x1, y1, x2, y2, button=button, steps=steps)

    async def scroll(self, amount=3, direction="down", horizontal=False) -> None:
        self._record("scroll", amount, direction, horizontal)

    async def type_text(self, text: str, interval: float = 0.008) -> int:
        self._record("type_text", text, interval=interval)
        return len(text)

    async def key_press(self, combo: str) -> None:
        self._record("key_press", combo)

    async def capture_png(self, path: Path, rect: Rect, cursor: bool = True) -> tuple[int, int]:
        self._record("capture_png", path, rect, cursor=cursor)
        Path(path).write_bytes(b"not really a png")
        return (rect.w, rect.h)


def go(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------------ ops
def test_ops_dispatch_to_backend_methods():
    """Each op must land on the same backend call the single-verb tool would make."""
    backend = FakeBackend()
    actions = [
        {"op": "move", "x": 10, "y": 20},
        {"op": "move", "x": 10, "y": 20, "cursor": "hand"},
        {"op": "move_by", "dx": -5, "dy": 7},
        {"op": "click", "x": 3, "y": 4, "button": "right", "clicks": 2},
        {"op": "click"},  # no coordinates: click wherever the pointer is
        {"op": "drag", "x1": 1, "y1": 2, "x2": 3, "y2": 4, "steps": 8},
        {"op": "scroll", "amount": 5, "direction": "up", "horizontal": True},
        {"op": "type", "text": "hi", "interval_ms": 4},
        {"op": "key", "keys": "ctrl+c"},
        {"op": "key", "combo": "Return"},  # accepted alias
    ]
    results = go(run_actions(backend, actions))
    assert all(isinstance(r, StepResult) and r.ok for r in results)
    assert [r.op for r in results] == [step["op"] for step in actions]

    assert backend.names() == [
        "move", "move", "move_by", "click", "click", "drag", "scroll", "type_text",
        "key_press", "key_press",
    ]
    recorded = [(args, kwargs) for _, args, kwargs in backend.calls]
    assert recorded[0] == ((10, 20), {"cursor": "default"})
    assert recorded[1] == ((10, 20), {"cursor": "hand"})
    assert recorded[2] == ((-5, 7), {})
    assert recorded[3] == ((3, 4), {"button": "right", "clicks": 2})
    # a click with no coordinates keeps the pointer where it is
    assert recorded[4] == ((None, None), {"button": "left", "clicks": 1})
    assert recorded[5] == ((1, 2, 3, 4), {"button": "left", "steps": 8})
    assert recorded[6] == ((5, "up", True), {})
    assert recorded[7] == (("hi",), {"interval": 0.004})
    assert recorded[8] == (("ctrl+c",), {})
    assert recorded[9] == (("Return",), {})


def test_shot_op_writes_a_png_to_the_requested_path(tmp_path: Path):
    backend = FakeBackend()
    target = tmp_path / "nested" / "look.png"
    (result,) = go(run_actions(backend, [{"op": "shot", "path": str(target)}]))
    assert result.ok
    assert target.exists()
    (_, args, kwargs) = backend.calls[0]
    assert args[0] == target
    assert args[1] == DESKTOP and kwargs == {"cursor": True}
    assert "1920x1080" in result.detail


def test_every_advertised_op_is_dispatchable(tmp_path: Path):
    """VALID_OPS must not drift away from the dispatch table."""
    assert set(VALID_OPS) == {"click", "move", "move_by", "drag", "scroll", "type",
                              "key", "shot", "wait"}
    backend = FakeBackend()
    steps = [
        {"op": "click", "x": 1, "y": 1},
        {"op": "move", "x": 1, "y": 1},
        {"op": "move_by", "dx": 1, "dy": 1},
        {"op": "drag", "x1": 0, "y1": 0, "x2": 1, "y2": 1},
        {"op": "scroll"},
        {"op": "type", "text": "a"},
        {"op": "key", "keys": "a"},
        {"op": "shot", "path": str(tmp_path / "a.png")},
        {"op": "wait", "ms": 0},
    ]
    assert [step["op"] for step in steps] == list(VALID_OPS)
    results = go(run_actions(backend, steps))
    assert [r.op for r in results] == list(VALID_OPS)
    assert all(r.ok for r in results)


# --------------------------------------------------------------- error policy
def test_unknown_op_is_rejected():
    backend = FakeBackend()
    with pytest.raises(ScreenAgentError) as excinfo:
        go(run_actions(backend, [{"op": "click", "x": 1, "y": 1}, {"op": "teleport"}]))
    message = str(excinfo.value)
    assert "teleport" in message
    for op in VALID_OPS:
        assert op in message, f"{op} missing from the error message"
    # the typo is caught before anything is sent to the desktop
    assert backend.calls == []


def test_unknown_op_is_rejected_with_stop_on_error_off():
    """A bad op is a bad request, not a step failure: it still aborts the batch."""
    backend = FakeBackend()
    with pytest.raises(ScreenAgentError):
        go(run_actions(backend, [{"op": "nope"}], stop_on_error=False))
    assert backend.calls == []


def test_stop_on_error_reports_n_of_m():
    """Default policy: the failing step aborts, and the partial run is still readable."""
    actions = [{"op": "move", "x": i, "y": i} for i in range(5)]
    actions[2] = {"op": "key", "keys": "ctrl+c"}  # this step raises
    backend = FakeBackend(fail_on={"key_press"})

    with pytest.raises(BatchError) as excinfo:
        go(run_actions(backend, actions))

    # only the first 3 steps ran: the failing one is the 3rd (index 2)
    assert backend.names() == ["move", "move", "key_press"]
    results = excinfo.value.results
    assert len(results) == 3
    assert [r.ok for r in results] == [True, True, False]
    assert excinfo.value.failed_index == 2
    failed = results[2]
    assert failed.op == "key"
    assert failed.error_type == "RuntimeError"
    assert "not supported here" in failed.error
    assert "key failed" in failed.detail
    # the step keeps the inputs exactly as the caller wrote them
    assert failed.action is actions[2]
    assert failed.index == 2


def test_continue_on_error_runs_all_steps():
    """stop_on_error=False must attempt every step and report each failure."""
    actions = [
        {"op": "move", "x": 1, "y": 1},
        {"op": "key", "keys": "ctrl+c"},
        {"op": "scroll"},
        {"op": "click", "x": 2, "y": 2},
        {"op": "type", "text": "boom"},
        {"op": "wait", "ms": 0},
    ]
    backend = FakeBackend(fail_on={"key_press", "type_text"})

    results = go(run_actions(backend, actions, stop_on_error=False))

    assert len(results) == len(actions)
    # `wait` never touches the backend, so it adds no call
    assert backend.names() == [
        "move", "key_press", "scroll", "click", "type_text",
    ]
    assert [r.op for r in results] == [step["op"] for step in actions]
    assert [r.ok for r in results] == [True, False, True, True, False, True]
    assert [r.index for r in results] == [0, 1, 2, 3, 4, 5]
    for result in results:
        assert result.action is actions[result.index]
    failures = [r for r in results if not r.ok]
    assert [r.op for r in failures] == ["key", "type"]
    for result in failures:
        assert result.error_type == "RuntimeError"
        assert result.error
        assert not result.ok and result.detail
    for result in results:
        if result.ok:
            assert result.error_type is None and result.error is None


def test_cancellation_is_not_swallowed():
    """Task cancellation must escape, not be reported as a step failure."""

    class Cancelling(FakeBackend):
        async def scroll(self, *args, **kwargs) -> None:
            self._record("scroll", *args, **kwargs)
            raise asyncio.CancelledError

    async def main() -> None:
        backend = Cancelling()
        for stop in (True, False):
            with pytest.raises(asyncio.CancelledError):
                await run_actions(backend, [{"op": "scroll"}], stop_on_error=stop)
        # the single scroll step ran once per policy, and nothing after it
        assert backend.names() == ["scroll", "scroll"]

    go(main())


def test_keyboard_interrupt_is_not_swallowed():
    async def main() -> None:
        class Interrupting(FakeBackend):
            async def key_press(self, combo: str) -> None:
                raise KeyboardInterrupt

        with pytest.raises(KeyboardInterrupt):
            await run_actions(Interrupting(), [{"op": "key", "keys": "ctrl+c"}])

    go(main())


# ------------------------------------------------------------------ wait cap
def test_wait_op_is_capped():
    """An oversized sleep is clamped, not honoured: a client must not wedge the server."""
    backend = FakeBackend()
    requested = MAX_WAIT_MS * 100
    loop = asyncio.new_event_loop()
    try:
        started = loop.time()
        (result,) = loop.run_until_complete(
            run_actions(backend, [{"op": "wait", "ms": requested}])
        )
        elapsed = loop.time() - started
    finally:
        loop.close()

    assert result.ok
    # clamped rather than rejected, and the clamp is visible in the detail string
    assert str(MAX_WAIT_MS) in result.detail
    assert str(requested) in result.detail
    assert elapsed < MAX_WAIT_MS / 1000.0 + 0.5, f"waited {elapsed:.2f}s"

    # a normal wait is passed through untouched
    (ok_result,) = go(run_actions(backend, [{"op": "wait", "ms": 1}]))
    assert ok_result.detail == "Waited 1 ms."

    # a negative wait is floored at zero, not turned into a negative sleep
    (floored,) = go(run_actions(backend, [{"op": "wait", "ms": -50}]))
    assert floored.ok and floored.detail == "Waited 0 ms."

    assert MAX_WAIT_MS == 5000


# ------------------------------------------------------------------- imports
def test_no_backend_module_is_imported_at_module_scope():
    """control.py must stay importable without pulling in a platform driver.

    Checked in a subprocess: in a full test run the other modules have long since
    imported the backends, so an in-process check would prove nothing.
    """
    src = str(Path(__file__).resolve().parents[1] / "src")
    code = (
        "import sys\n"
        "import screen_agent.control\n"
        "print([m for m in sys.modules if m.startswith('screen_agent.backends.')])\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parents[1]),
        env={**os.environ, "PYTHONPATH": src},
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "[]", proc.stdout


def test_control_does_not_import_a_backend_name():
    """A belt-and-braces in-process check on the module's own imports."""
    source = Path(
        Path(__file__).resolve().parents[1] / "src" / "screen_agent" / "control.py"
    ).read_text(encoding="utf-8")
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith(("import ", "from ")):
            assert "backends" not in stripped, f"module-scope backend import: {line!r}"
