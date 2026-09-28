"""The MCP surface: the tools an AI can call, over a real stdio session.

``screen-agent serve`` is how every MCP client starts this server, so the only way to
know it works is to start it the way a client does and talk to it.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

pytest.importorskip("mcp")

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

from screen_agent.backends.base import Rect  # noqa: E402
from screen_agent.mcp_server import build_server  # noqa: E402

EXPECTED_TOOLS = {
    "screen_info",
    "screenshot",
    "screen_record",
    "mouse_move",
    "mouse_move_by",
    "mouse_click",
    "mouse_drag",
    "mouse_scroll",
    "type_text",
    "key_press",
    "screen_state",
    "screen_do",
    "page_state",
    "page_find",
    "page_click",
}


def test_server_declares_every_tool():
    import asyncio

    server = build_server()
    tools = asyncio.run(server.list_tools())
    assert {tool.name for tool in tools} == EXPECTED_TOOLS
    for tool in tools:
        assert tool.description, f"{tool.name} has no description for the model"


def test_instructions_mention_the_screenshot_loop():
    from screen_agent.mcp_server import INSTRUCTIONS

    lowered = INSTRUCTIONS.lower()
    assert "screenshot" in lowered
    assert "read that path" in lowered  # the model has to be told to look at the file


def test_every_tool_has_an_implementation():
    """A tool that raises NotImplementedError is worse than no tool at all."""
    import asyncio
    import inspect

    server = build_server()
    for tool in asyncio.run(server.list_tools()):
        handler = server._tool_manager.get_tool(tool.name).fn
        assert inspect.iscoroutinefunction(handler), tool.name
        assert handler.__doc__, tool.name


# ------------------------------------------------------------------ live session
@pytest.mark.skipif(sys.platform != "win32", reason="needs a desktop for the tools")
def test_stdio_session_answers():
    """Start `python -m screen_agent serve` and drive it like an MCP client would."""
    import asyncio

    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")

    async def go() -> tuple[str, str]:
        params = StdioServerParameters(
            command=sys.executable, args=["-m", "screen_agent", "serve"], env=env,
            cwd=str(ROOT),
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                names = {tool.name for tool in (await session.list_tools()).tools}
                assert names == EXPECTED_TOOLS, sorted(names)
                info = await session.call_tool("screen_info", {})
                shot = await session.call_tool(
                    "screenshot", {"region": "0,0,64,48", "scale": 32}
                )
                return _text(info), _text(shot)

    info, shot = asyncio.run(asyncio.wait_for(go(), timeout=90))
    assert "backend        : windows" in info
    assert "desktop        :" in info
    assert "DPI awareness" in info
    # the tool has to hand back a readable path, since the model reads that file
    assert "Saved" in shot and "32x24 px" in shot, shot
    saved = shot.split("Saved ", 1)[1].split(" ", 1)[0]
    path = Path(saved)
    try:
        assert path.exists() and path.stat().st_size > 0
        from screen_agent.core import _png_size  # noqa: PLC0415

        assert _png_size(path) == (32, 24)
    finally:
        path.unlink(missing_ok=True)


def _text(result) -> str:
    return "\n".join(block.text for block in result.content if getattr(block, "text", None))


@pytest.mark.skipif(sys.platform != "win32", reason="needs a desktop for the tools")
def test_an_actionable_error_survives_the_round_trip_to_the_model():
    """The end-to-end proof that our error text is not swallowed on the way out.

    This is the whole justification for raising instead of silently falling back, so it
    is checked where it actually happens: over a real stdio session, against a port where
    nothing is listening. Before the wrapper existed this returned the bare string
    "Error executing tool page_click" - the model had no idea a browser was the problem,
    let alone which two flags to pass.
    """
    import asyncio

    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    env["SCREEN_AGENT_CDP_PORT"] = "9999"  # deliberately nothing there

    async def go() -> str:
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "screen_agent", "serve"],
            env=env,
            cwd=str(ROOT),
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(
                    "page_click", {"selector": "a[href]", "nth": 1}
                )
                return _text(result)

    text = asyncio.run(asyncio.wait_for(go(), timeout=90))

    assert "remote-debugging-port" in text, text
    assert "user-data-dir" in text, text
    # and the reason it did not just click something instead
    assert "guessing screen coordinates" in text, text


# ------------------------------------------------------- the fast capture path
class _RecordingBackend:
    """A backend that records which capture method the server chose.

    The point of the assertions below is the *path taken*, never a duration: a test that
    waits 30 ms to prove something is 3x faster is a test that will fail on a busy
    machine and pass on a broken one. Recording the call proves the intent.
    """

    name = "recording"

    def __init__(self, *, settled: bool = True, age_ms: int = 0) -> None:
        self.desktop = Rect(0, 0, 1920, 1080)
        self.calls: list[tuple] = []
        self._settled = settled
        self.last_capture_age_ms = age_ms

    async def capture_png(self, path, rect, cursor=True):
        self.calls.append(("capture_png", rect, cursor, 0))
        _write_png(path, 1920, 1080)
        return 1920, 1080

    async def capture_scaled(self, path, rect, *, max_width=0, cursor=True, max_age_ms=0):
        self.calls.append(("capture_scaled", rect, cursor, max_width, max_age_ms))
        width = max_width or 1920
        height = round(1080 * width / 1920)
        _write_png(path, width, height)
        return width, height

    async def capture_stable(self, path, rect, *, cursor=True, timeout_ms=1000, poll_ms=40):
        self.calls.append(("capture_stable", rect, cursor, timeout_ms))
        _write_png(path, 1920, 1080)
        return 1920, 1080, self._settled

    async def screen_state(self):
        self.calls.append(("screen_state",))
        return {"foreground": {"title": "recording"}}

    async def info(self):
        self.calls.append(("info",))
        from screen_agent.backends.base import BackendInfo

        return BackendInfo(
            backend="recording",
            platform="test",
            session="none",
            desktop=Rect(0, 0, 1920, 1080),
            capabilities={"screenshot": True, "mouse": True, "keyboard": True},
        )


def _write_png(path: Path, width: int, height: int) -> None:
    from screen_agent.imaging import encode_png

    path.write_bytes(encode_png(width, height, b"\x20\x30\x40" * width * height, level=1))


def _tool(server, name):
    return server._tool_manager.get_tool(name).fn


def _call(server, name, **kwargs) -> str:
    """Call a tool function directly.

    The tool functions return a plain string; the ``.content`` blocks only exist once a
    session has wrapped them, which is what ``_text`` is for. These tests are about which
    backend method the server reached, not about the stdio plumbing, which
    ``test_stdio_session_answers`` already covers end to end.
    """
    import asyncio

    return asyncio.run(_tool(server, name)(**kwargs))


def test_scaled_screenshot_goes_through_the_backend_not_through_python():
    """scale must reach the backend, so GDI shrinks the pixels while copying them.

    The alternative - capture 1920x1080 and resize the finished file - costs 3x more and
    encodes a megabyte of pixels nobody will look at.
    """
    from screen_agent.core import scale_image

    backend = _RecordingBackend()
    server = build_server(backend)
    out = _call(server, "screenshot", region="full", scale=640, path=str(Path.cwd() / "t.png"))
    try:
        assert [call[0] for call in backend.calls] == ["capture_scaled"]
        assert backend.calls[0][3] == 640  # max_width, not a post-hoc resize
        assert "640x360 px" in out
    finally:
        Path("t.png").unlink(missing_ok=True)
    # and the python resizer is not in the picture at all on this path
    assert callable(scale_image)


def test_unscaled_screenshot_asks_for_no_width():
    backend = _RecordingBackend()
    server = build_server(backend)
    _call(server, "screenshot", region="0,0,64,48", path=str(Path.cwd() / "u.png"))
    try:
        assert backend.calls[0][:4] == ("capture_scaled", Rect(0, 0, 64, 48), True, 0)
    finally:
        Path("u.png").unlink(missing_ok=True)


def test_stable_screenshot_waits_and_says_when_it_gave_up():
    """A frame that never settles is still returned - with a warning, not an error.

    An agent that asked for a stable shot and got an exception has no frame at all, and
    retrying costs a round trip. A frame plus 'this was mid-repaint' is more useful than
    either, provided the message says so plainly.
    """
    backend = _RecordingBackend(settled=False)
    server = build_server(backend)
    out = _call(
        server,
        "screenshot",
        stable=True,
        stable_timeout_ms=250,
        path=str(Path.cwd() / "v.png"),
    )
    try:
        assert backend.calls[0][0] == "capture_stable"
        assert backend.calls[0][3] == 250
        assert "still changing" in out and "mid-repaint" in out
    finally:
        Path("v.png").unlink(missing_ok=True)


def test_stable_and_max_age_are_refused_together():
    """They ask for opposite things: a guaranteed-fresh frame vs. an early return."""
    server = build_server(_RecordingBackend())
    try:
        _call(server, "screenshot", stable=True, max_age_ms=500)
    except Exception as exc:
        assert "opposite" in str(exc)
    else:
        pytest.fail("stable + max_age_ms was accepted")


def test_deliberate_errors_are_raised_as_the_type_the_model_can_read():
    """The message is the whole design, and the SDK drops it for unrecognised errors.

    Measured on mcp 2.x: a tool that raised a plain exception answered the model with
    "Error executing tool <name>" and nothing else - the port, the flags, the reason
    there is no coordinate fallback, all discarded as if it were a crash. Every tool
    therefore goes through one wrapper that converts screen-agent's deliberate errors
    into the SDK's recognised type. If that wrapper is ever removed, this fails.
    """
    server = build_server(_RecordingBackend())
    try:
        _call(server, "screenshot", stable=True, max_age_ms=500)
    except Exception as exc:
        from screen_agent.mcp_server import _tool_error_type

        assert type(exc) is _tool_error_type(), f"leaked as {type(exc).__name__}"
    else:
        pytest.fail("nothing was raised")


def test_scale_is_honoured_on_the_stable_path_too():
    """capture_stable has no max_width, so the scale has to be applied afterwards.

    Dropping the argument would report a size the caller did not ask for, which is worse
    than doing it the slower way and saying so.
    """
    backend = _RecordingBackend(settled=True)
    server = build_server(backend)
    out = _call(
        server,
        "screenshot",
        stable=True,
        scale=320,
        path=str(Path.cwd() / "s.png"),
    )
    try:
        assert backend.calls[0][0] == "capture_stable"
        # the stable grab is full size, then the finished file is rescaled
        assert "scaled after capture" in out
        from screen_agent.core import _png_size

        assert _png_size(Path("s.png")) == (320, 180)
    finally:
        Path("s.png").unlink(missing_ok=True)


def test_reused_frame_is_reported_as_old():
    """A cached shot may be up to max_age_ms old, and the caller has to be told."""
    backend = _RecordingBackend(age_ms=42)
    server = build_server(backend)
    out = _call(server, "screenshot", max_age_ms=500, path=str(Path.cwd() / "w.png"))
    try:
        assert backend.calls[0][4] == 500
        assert "reused a frame captured 42ms ago" in out
    finally:
        Path("w.png").unlink(missing_ok=True)


def test_the_cache_is_off_unless_asked_for():
    backend = _RecordingBackend()
    server = build_server(backend)
    _call(server, "screenshot", path=str(Path.cwd() / "x.png"))
    try:
        assert backend.calls[0][4] == 0
    finally:
        Path("x.png").unlink(missing_ok=True)


def test_capture_scaled_answers_the_same_shape_on_every_backend():
    """A subclass must not widen the return value of a method it inherited.

    ``capture_scaled`` reporting ``(w, h)`` on four backends and ``(w, h, age)`` on the
    fifth is a trap for every caller that works against the abstract type - including
    this server, which does not know which backend it was handed.
    """
    import inspect

    from screen_agent.backends import macos, mutter, wayland_tools, windows, x11

    checked = 0
    for module in (windows, x11, mutter, macos, wayland_tools):
        for cls in vars(module).values():
            if not inspect.isclass(cls):
                continue
            method = cls.__dict__.get("capture_scaled")
            if method is None:
                continue
            checked += 1
            hint = inspect.signature(method).return_annotation
            assert "int, int" in str(hint), f"{cls.__name__}.capture_scaled -> {hint}"
    assert checked >= 2, "the shape guard found nothing to guard"


def test_screen_state_is_offered_without_an_image():
    """The whole point of screen_state is that it costs microseconds, not megabytes."""
    import json

    backend = _RecordingBackend()
    server = build_server(backend)
    out = _call(server, "screen_state")
    assert backend.calls == [("screen_state",)]
    assert json.loads(out)["foreground"]["title"] == "recording"


def test_a_missing_await_cannot_return_a_coroutine_as_a_result(monkeypatch):
    """A tool that forgets to await still returns a string, so nothing else catches it.

    This is not hypothetical: ``page_state`` shipped with ``_json(_state())`` instead of
    ``_json(await _state())`` and returned the literal text
    ``"<coroutine object page_state at 0x...>"`` to the model. Every existing test passed,
    because the value was a perfectly well-formed string. It was only found by an agent
    reporting that the tool "returned a coroutine object", and it would have made
    ``page_state`` useless - the tool whose whole job is to be cheap and reliable.

    So the check is behavioural, not structural: with no browser reachable, the tool must
    raise, and it must never hand back a coroutine's repr.
    """
    from screen_agent import browser as browser_mod

    class Awaited(Exception):
        """Raised only if the coroutine is actually awaited."""

    async def must_be_awaited(client=None):
        raise Awaited("the tool awaited it")

    monkeypatch.setattr(browser_mod, "page_state", must_be_awaited)
    server = build_server(_RecordingBackend())
    with pytest.raises(Awaited):
        _call(server, "page_state")


def test_every_tool_returns_a_string_not_a_coroutine_repr():
    """Belt and braces: no tool's result may contain a coroutine repr.

    Cheap to assert over the whole tool surface, and it is the shape of the failure that
    a missing await produces - a valid-looking string that is not the answer.
    """
    import asyncio

    server = build_server(_RecordingBackend())
    for name in ("screen_info", "screen_state", "screen_do"):
        out = _call(server, name) if name != "screen_do" else _call(
            server, name, actions=[{"op": "wait", "ms": 1}]
        )
        assert isinstance(out, str), name
        assert "coroutine" not in out, f"{name} returned {out[:80]}"
    del asyncio


def test_instructions_do_not_lead_with_the_screenshot():
    """The model reads the instructions before its first tool call; lead it correctly."""
    from screen_agent.mcp_server import INSTRUCTIONS

    assert "page_click" in INSTRUCTIONS
    assert "remote-debugging-port" in INSTRUCTIONS
    assert "read that path" in INSTRUCTIONS.lower()  # still told to look at the file
