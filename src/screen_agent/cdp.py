"""A small, async Chrome DevTools Protocol (CDP) client.

Why this module exists
----------------------
Locating "the third link on this page, and the pixels to click on it" over CDP costs
about 1.1 ms end to end on a local Chromium and returns the link's exact text, its
resolved ``href`` and its geometry. Taking a screenshot to work the same thing out costs
~95 ms and returns a guess. That is an 80x difference in latency *and* the difference
between an exact answer and a plausible one, so every browser operation in
:mod:`screen_agent.browser` is built on top of this client.

It works with any Chromium - Chrome, Edge, CocCoc, Brave, Vivaldi - because they all
speak the same protocol.

How it fails, on purpose
------------------------
There is deliberately **no fallback to screen coordinates**. If no debugging endpoint is
reachable this raises :class:`CdpUnavailable` telling you which two flags the browser has
to be started with. That is not pedantry: a coordinate fallback that silently succeeds
lets an agent report "clicked the sign-in link" when it clicked empty space 40 pixels
above it, and the mistake is invisible in the transcript. Failing loudly is the safe
option.

Using it
--------
::

    client = CdpClient()
    target = await client.connect()          # the visible + focused tab, or an error
    title = await client.evaluate("document.title")
    await client.close()

Every call is a coroutine; nothing here blocks the event loop. The two seams
(``http_get`` and ``open_ws``) exist so the tests can drive the whole client without a
browser anywhere in sight.
"""

from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from .core import ReportedError

__all__ = [
    "DEFAULT_PORT",
    "PORT_ENV",
    "PROBE_JS",
    "AmbiguousTarget",
    "CdpClient",
    "CdpConnectionError",
    "CdpHttpError",
    "CdpProtocolError",
    "CdpTimeout",
    "CdpUnavailable",
    "NoActiveTab",
    "Target",
    "close_client",
    "get_client",
    "http_get_json",
    "no_endpoint_help",
    "open_websocket",
]

#: the port Chrome documents for this; every fork uses the same one
DEFAULT_PORT = 9222
#: environment variable that overrides it (``SCREEN_AGENT_CDP_PORT=9333``)
PORT_ENV = "SCREEN_AGENT_CDP_PORT"
#: only these target types are browser tabs we are willing to drive
PAGE_TYPES = ("page", "webview")
#: how long a single HTTP probe of the endpoint may take before we call it dead
HTTP_TIMEOUT = 1.0
#: how long a websocket handshake may take
CONNECT_TIMEOUT = 1.0
#: default budget for one ``Runtime.evaluate`` round trip
EVALUATE_TIMEOUT = 2.0

#: the probe used to pick the tab the user is actually looking at
PROBE_JS = "({vis:document.visibilityState, foc:document.hasFocus(), url:location.href, title:document.title})"


# --------------------------------------------------------------------------- errors
class CdpUnavailable(ReportedError):
    """Root of every error this module raises.

    Also raised directly when the debugging endpoint cannot be reached at all; the
    message then names both flags the browser has to be started with. That message is
    the entire point of raising instead of guessing a coordinate, and it only survives
    the trip to the model because the base class is one the MCP SDK recognises as
    deliberate - see ``core.ReportedError``.
    """


class CdpHttpError(CdpUnavailable):
    """The endpoint answered, but with an HTTP error (``.code`` holds the status)."""

    def __init__(self, message: str, code: int = 0) -> None:
        super().__init__(message)
        self.code = code


class CdpConnectionError(CdpUnavailable):
    """A websocket to a target could not be opened, or died mid-call."""


class CdpTimeout(CdpUnavailable):
    """A command was sent but no reply arrived inside the budget."""


class CdpProtocolError(CdpUnavailable):
    """The browser replied with a protocol error, or the page threw."""


class NoActiveTab(CdpUnavailable):
    """No target is both ``visibilityState == 'visible'`` and ``document.hasFocus()``."""


class AmbiguousTarget(CdpUnavailable):
    """More than one target claims to be the active tab - never auto-pick.

    ``targets`` carries every candidate so a caller (or a human) can choose.
    """

    def __init__(self, targets: Sequence[Target] = (), message: str | None = None) -> None:
        self.targets = list(targets)
        listed = "\n".join(f"  - {t.title!r} {t.url}" for t in self.targets)
        super().__init__(
            message
            or (
                f"{len(self.targets)} tabs are visible and focused at once; refusing to "
                f"guess which one you meant:\n{listed}"
            )
        )


def no_endpoint_help(port: int) -> str:
    """The message shown when nothing is listening: both flags, and the no-fallback rule."""
    return (
        f"no Chrome DevTools Protocol endpoint on 127.0.0.1:{port}.\n"
        f"Start the browser with BOTH of these flags:\n"
        f"    --remote-debugging-port={port}\n"
        f"    --user-data-dir=<a dedicated, empty profile directory>\n"
        f"Both are required: Chromium and its forks (Chrome, Edge, CocCoc, Brave, Vivaldi) "
        f"refuse --remote-debugging-port on the profile you use every day, so the separate "
        f"--user-data-dir is not optional. For Chrome on Windows:\n"
        f'    "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" '
        f'--remote-debugging-port={port} --user-data-dir="%TEMP%\\screen-agent-chrome"\n'
        f"screen-agent deliberately does NOT fall back to guessing screen coordinates: a "
        f"guessed pixel is not a link, and a silent fallback would let an agent report "
        f"\"clicked the sign-in link\" when it clicked empty space. Start the browser with "
        f"the two flags above, or take a coordinate from a screenshot on purpose."
    )


# ---------------------------------------------------------------------------- target
@dataclass(frozen=True)
class Target:
    """One entry of ``/json/list``: a tab, a worker or a devtools page."""

    id: str
    type: str
    title: str
    url: str
    web_socket_url: str = ""

    @classmethod
    def from_json(cls, raw: Any) -> Target | None:
        """Build a target from one ``/json/list`` entry, or ``None`` if unusable."""
        if not isinstance(raw, dict):
            return None
        return cls(
            id=str(raw.get("id") or ""),
            type=str(raw.get("type") or ""),
            title=str(raw.get("title") or ""),
            url=str(raw.get("url") or ""),
            web_socket_url=str(raw.get("webSocketDebuggerUrl") or ""),
        )

    @property
    def is_page(self) -> bool:
        """True for a real tab we can attach to (workers and the browser entry are not)."""
        return self.type in PAGE_TYPES and bool(self.web_socket_url)

    def as_dict(self) -> dict[str, str]:
        return {"id": self.id, "type": self.type, "title": self.title, "url": self.url}


# ------------------------------------------------------------------------ the seams
async def http_get_json(url: str, timeout: float = HTTP_TIMEOUT) -> Any:
    """GET *url* and decode the JSON body, in a worker thread (never blocks the loop)."""
    return await asyncio.to_thread(_http_get_json_sync, url, timeout)


def _http_get_json_sync(url: str, timeout: float) -> Any:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            body = response.read()
    except urllib.error.HTTPError as exc:
        raise CdpHttpError(f"{url}: HTTP {exc.code} {exc.reason}", exc.code) from exc
    except Exception as exc:  # URLError, socket.timeout, OSError, ...
        raise CdpUnavailable(f"{url}: {exc}") from exc
    try:
        return json.loads(body.decode("utf-8", "replace"))
    except ValueError as exc:
        raise CdpUnavailable(f"{url}: reply is not JSON ({exc})") from exc


async def open_websocket(url: str, timeout: float = CONNECT_TIMEOUT) -> Any:
    """Open a CDP websocket. The ``websockets`` package is imported lazily (it is optional)."""
    try:
        import websockets
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise CdpUnavailable(
            "the CDP client needs the 'websockets' package: "
            "pip install 'screen-agent[cdp]'"
        ) from exc
    try:
        return await websockets.connect(
            url,
            open_timeout=timeout,
            close_timeout=timeout,
            max_size=16 * 1024 * 1024,
            ping_interval=None,  # we are a short request/response client, not a stream
        )
    except Exception as exc:
        raise CdpConnectionError(f"{url}: cannot open a CDP socket ({exc})") from exc


# -------------------------------------------------------------------- socket helpers
def _is_transport_error(exc: BaseException) -> bool:
    """True when *exc* means "the socket is gone" rather than "the browser said no"."""
    if isinstance(exc, CdpConnectionError):
        return True
    if isinstance(exc, (OSError, EOFError, asyncio.IncompleteReadError)):
        return True
    return type(exc).__name__ in {
        "ConnectionClosed",
        "ConnectionClosedError",
        "ConnectionClosedOK",
        "InvalidHandshake",
        "InvalidURI",
        "WebSocketException",
    }


def _is_open(ws: Any) -> bool:
    """Best-effort liveness check that works for both websockets API generations."""
    state = getattr(ws, "state", None)
    if state is not None:
        return getattr(state, "name", str(state)) == "OPEN"
    closed = getattr(ws, "closed", None)
    if isinstance(closed, bool):
        return not closed
    return True  # unknown shape: try it and let the error speak


async def _close_ws(ws: Any) -> None:
    try:
        await ws.close()
    except Exception:  # noqa: BLE001 - closing a dead socket must never raise
        pass


async def _recv_reply(ws: Any, message_id: int, timeout: float) -> dict[str, Any]:
    """Read frames until the reply to *message_id* arrives; events are skipped."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise asyncio.TimeoutError
        raw = await asyncio.wait_for(ws.recv(), remaining)
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", "replace")
        try:
            message = json.loads(raw)
        except ValueError as exc:
            raise CdpProtocolError(f"CDP sent a frame that is not JSON: {str(raw)[:120]!r}") from exc
        if isinstance(message, dict) and message.get("id") == message_id:
            return message
        # an event, or a late reply belonging to an abandoned command: keep reading,
        # but the deadline above stops us from spinning forever


async def _exchange(ws: Any, message_id: int, method: str, params: dict, timeout: float) -> Any:
    """Send one command and return its ``result``; raise with a readable message."""
    payload = json.dumps({"id": message_id, "method": method, "params": params})
    try:
        await asyncio.wait_for(ws.send(payload), timeout)
        reply = await _recv_reply(ws, message_id, timeout)
    except asyncio.TimeoutError:
        raise CdpTimeout(f"{method}: no reply within {timeout:g}s") from None
    except CdpProtocolError:
        raise
    except Exception as exc:
        if _is_transport_error(exc):
            raise CdpConnectionError(f"{method}: the CDP socket went away ({exc})") from exc
        raise
    error = reply.get("error")
    if error:
        code = error.get("code") if isinstance(error, dict) else None
        message = error.get("message") if isinstance(error, dict) else str(error)
        raise CdpProtocolError(f"{method} failed (code {code}): {message}")
    return reply.get("result") or {}


# -------------------------------------------------------------------------- client
class CdpClient:
    """A connection to one browser tab.

    *open_ws* and *http_get* are the two seams: they default to the real
    :func:`open_websocket` / :func:`http_get_json` and exist so tests can inject a fake
    browser. Both are called as ``await seam(url_or_none, timeout)``; ``open_ws`` must
    return an object with ``send``/``recv``/``close`` coroutines.
    """

    def __init__(
        self,
        *,
        port: int | None = None,
        host: str = "127.0.0.1",
        open_ws: Callable[[str, float], Awaitable[Any]] | None = None,
        http_get: Callable[[str, float], Awaitable[Any]] | None = None,
        http_timeout: float = HTTP_TIMEOUT,
        connect_timeout: float = CONNECT_TIMEOUT,
        evaluate_timeout: float = EVALUATE_TIMEOUT,
    ) -> None:
        self.port = int(port) if port else env_port()
        self.host = host
        self.http_timeout = float(http_timeout)
        self.connect_timeout = float(connect_timeout)
        self.evaluate_timeout = float(evaluate_timeout)
        self._open_ws = open_ws or open_websocket
        self._http_get = http_get or http_get_json
        self._ws: Any = None
        self._target: Target | None = None
        self._next_id = 0
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ geometry
    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def target(self) -> Target | None:
        """The tab this client is attached to, once :meth:`connect` has run."""
        return self._target

    @property
    def connected(self) -> bool:
        """False when there is no usable socket - a dead client is never cached."""
        return self._ws is not None and _is_open(self._ws)

    # -------------------------------------------------------------------- http
    async def version(self) -> dict[str, Any]:
        """``/json/version``: the browser product string and the browser-level socket."""
        data = await self._get_json("/json/version")
        return data if isinstance(data, dict) else {}

    async def list_targets(self) -> list[Target]:
        """Every page target the browser currently has open."""
        data = await self._get_json("/json/list")
        if isinstance(data, dict):  # a few forks only expose the unversioned /json
            data = await self._get_json("/json")
        if not isinstance(data, list):
            raise CdpUnavailable(
                f"{self.base_url}/json/list returned {type(data).__name__}, expected a list"
            )
        targets = [t for t in (Target.from_json(item) for item in data) if t is not None]
        return [t for t in targets if t.is_page]

    async def _get_json(self, path: str) -> Any:
        url = f"{self.base_url}{path}"
        try:
            return await self._http_get(url, self.http_timeout)
        except CdpHttpError as exc:
            if exc.code in (400, 404):
                return await self._http_get(url.replace(path, "/json"), self.http_timeout)
            raise CdpUnavailable(f"{no_endpoint_help(self.port)}\n(asked {url}: {exc})") from exc
        except CdpUnavailable as exc:
            raise CdpUnavailable(f"{no_endpoint_help(self.port)}\n(asked {url}: {exc})") from exc

    # ---------------------------------------------------------------- targeting
    async def active_target(self) -> Target:
        """The one tab that is visible *and* focused, or a loud error.

        ``/json/list`` order is not tab order and the first entry is often a background
        tab, so the choice is made from the page's own state. Two visible+focused tabs
        means two windows; guessing is how you click the wrong one, so it raises
        :class:`AmbiguousTarget` with the candidates attached.

        A single page is always taken, even when it reports itself as neither visible
        nor focused. Both signals are unreliable on their own: a window behind the
        terminal the agent is running in is occluded, not hidden, and refusing to drive it
        would rule out the ordinary case of a browser sharing the screen with a shell.
        """
        targets = await self.list_targets()
        if not targets:
            raise NoActiveTab(
                f"{self.base_url} reports no open tab to drive. Open a page in the "
                f"browser, or pass a selector that works on the tab you mean."
            )
        matched: list[tuple[Target, dict[str, Any]]] = []
        probed: list[tuple[Target, dict[str, Any]]] = []
        skipped: list[str] = []
        for target in targets:
            try:
                info = await self.probe(target)
            except CdpUnavailable as exc:
                skipped.append(f"{target.url or target.id}: {exc}")
                continue
            probed.append((target, info))
            if info.get("vis") == "visible" and info.get("foc") is True:
                matched.append((target, info))
        if len(matched) == 1:
            return matched[0][0]
        if len(matched) > 1:
            raise AmbiguousTarget([t for t, _ in matched])
        if len(probed) == 1:
            # One page, and it is not reporting itself as focused. This is the ordinary
            # case, not an error: an agent driving a desktop has a terminal in front of
            # the browser, and Chrome answers ``visibilityState: "hidden"`` for a window
            # that is merely occluded while ``hasFocus()`` can still be true. Demanding
            # both would make the common case impossible to automate at all.
            return probed[0][0]
        # More than one page and none claims focus: the browser has several tabs open and
        # we have no honest way to tell which one the caller means. Saying so beats
        # picking the first, which is how you type into the wrong form.
        seen = "\n".join(
            f"  - {t.title or '(untitled)'} {t.url} -> vis={info.get('vis')!r} "
            f"foc={info.get('foc')!r}"
            for t, info in probed
        )
        broken = "\n".join(f"  - {line}" for line in skipped)
        raise NoActiveTab(
            f"none of the {len(targets)} open tabs reports itself as the visible, focused "
            f"one, and there is more than one tab to choose from. Focus the tab you mean, "
            f"or close the others.\nprobed:\n{seen}\n"
            + (f"unreachable:\n{broken}\n" if broken else "")
        )

    async def probe(self, target: Target) -> dict[str, Any]:
        """Run :data:`PROBE_JS` in *target* over a throwaway socket."""
        ws = await self._open_ws(target.web_socket_url, self.connect_timeout)
        try:
            result = await _exchange(ws, 1, "Runtime.evaluate", _evaluate_params(PROBE_JS), 1.0)
        finally:
            await _close_ws(ws)
        value = result.get("result") or {}
        return value.get("value") or {}

    async def connect(self) -> Target:
        """Attach to the active tab (idempotent); re-selects after a dropped socket."""
        if self.connected and self._target is not None:
            return self._target
        target = await self.active_target()
        self._ws = await self._open_ws(target.web_socket_url, self.connect_timeout)
        self._target = target
        return target

    # ----------------------------------------------------------------- protocol
    def _id(self) -> int:
        self._next_id += 1
        return self._next_id

    async def send(
        self, method: str, params: dict[str, Any] | None = None, *, timeout: float | None = None
    ) -> Any:
        """Send one CDP command and return its ``result``.

        A dropped socket is retried exactly once per call and then dropped for good, so a
        browser restart costs one extra round trip rather than a permanently dead client.
        Calls are serialised: this is a request/response protocol on one socket, and two
        callers interleaving their reads is a race nobody needs.
        """
        budget = self.evaluate_timeout if timeout is None else float(timeout)
        failure: CdpConnectionError | None = None
        async with self._lock:
            for _ in range(2):
                try:
                    ws = await self._socket()
                    return await _exchange(ws, self._id(), method, params or {}, budget)
                except CdpConnectionError as exc:
                    failure = exc
                    await self._drop()
        raise failure if failure else CdpConnectionError(f"{method}: no socket")

    async def _socket(self) -> Any:
        if self.connected:
            return self._ws
        await self._drop()  # never hand a dead socket to a command
        await self.connect()
        return self._ws

    async def evaluate(self, expression: str, *, timeout: float = EVALUATE_TIMEOUT) -> Any:
        """Evaluate *expression* in the page and return its value.

        ``returnByValue`` gives plain JSON (what the tools need) and ``awaitPromise``
        means an async IIFE resolves before the answer comes back. A page-side throw
        becomes :class:`CdpProtocolError` carrying the JS error, not a silent ``None``.
        """
        result = await self.send("Runtime.evaluate", _evaluate_params(expression), timeout=timeout)
        details = result.get("exceptionDetails")
        if details:
            raise CdpProtocolError(f"the page threw while evaluating that expression: {_js_error(details)}")
        value = result.get("result") or {}
        if "value" in value:
            return value["value"]
        if "unserializableValue" in value:
            return value["unserializableValue"]
        return None

    async def dispatch_mouse_event(
        self,
        event: str,
        x: float,
        y: float,
        *,
        button: str = "left",
        click_count: int = 1,
        timeout: float = EVALUATE_TIMEOUT,
    ) -> Any:
        """``Input.dispatchMouseEvent`` at CSS pixels, viewport-relative.

        These coordinates need no conversion to desktop pixels - the browser already knows
        where its own viewport sits on the screen - and the event is trusted, so page
        handlers see a real click.
        """
        return await self.send(
            "Input.dispatchMouseEvent",
            {
                "type": event,
                "x": float(x),
                "y": float(y),
                "button": button,
                "clickCount": int(click_count),
                "buttons": 1 if event == "mousePressed" else 0,
                "pointerType": "mouse",
            },
            timeout=timeout,
        )

    # ------------------------------------------------------------------ teardown
    async def _drop(self) -> None:
        ws, self._ws = self._ws, None
        self._target = None
        if ws is not None:
            await _close_ws(ws)

    async def close(self) -> None:
        """Close the socket. Safe to call twice, and on a client that never connected."""
        async with self._lock:
            await self._drop()

    async def __aenter__(self) -> CdpClient:
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()


def _evaluate_params(expression: str) -> dict[str, Any]:
    return {
        "expression": expression,
        "returnByValue": True,
        "awaitPromise": True,
        "includeCommandLineAPI": False,
    }


def _js_error(details: dict[str, Any]) -> str:
    exception = details.get("exception") or {}
    text = exception.get("description") or exception.get("value") or details.get("text") or ""
    return str(text)[:400] or "no detail"


def env_port() -> int:
    """``SCREEN_AGENT_CDP_PORT`` if it is a usable integer, else :data:`DEFAULT_PORT`."""
    raw = os.environ.get(PORT_ENV, "").strip()
    if not raw:
        return DEFAULT_PORT
    try:
        port = int(raw)
    except ValueError:
        return DEFAULT_PORT
    return port if 0 < port < 65536 else DEFAULT_PORT


# ------------------------------------------------------------- the shared instance
_shared: CdpClient | None = None


def get_client() -> CdpClient:
    """The process-wide client, rebuilt whenever the last one died.

    Rebuilding rather than reusing is the point: a browser that was restarted, or a
    socket that timed out, must not leave a permanently broken client behind.
    """
    global _shared
    if _shared is None or not _shared.connected:
        _shared = CdpClient()
    return _shared


async def close_client() -> None:
    """Close the shared client, if there is one."""
    global _shared
    client, _shared = _shared, None
    if client is not None:
        await client.close()
