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
