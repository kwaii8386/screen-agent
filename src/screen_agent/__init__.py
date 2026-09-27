"""screen-agent: an MCP server that gives an AI eyes and hands on the local desktop.

Backends are chosen automatically (see :func:`screen_agent.backends.load_backend`)
and every backend implements the same async surface: screenshots, screen recording,
pointer control and keyboard control.
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
