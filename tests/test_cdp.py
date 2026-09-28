"""Unit tests for the CDP client, against a fake browser.

Nothing here opens a socket, launches Chrome, or attaches to anything: the client takes
its two seams (``http_get`` and ``open_ws``) as constructor arguments, and
:class:`FakeBrowser` stands in for a Chromium that has some tabs open. That is deliberate
- a test that can wake a real browser is a test that can click in a real window.

What these pin down is the part that is easy to get quietly wrong: which tab gets picked,
what happens when the answer is ambiguous or absent, and the promise that a dead endpoint
raises instead of falling back to coordinates.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from collections import deque
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from screen_agent import cdp  # noqa: E402
from screen_agent.cdp import (  # noqa: E402
    AmbiguousTarget,
    CdpClient,
    CdpConnectionError,
    CdpHttpError,
    CdpProtocolError,
    CdpTimeout,
    CdpUnavailable,
    NoActiveTab,
    Target,
)

PORT = 9222


class FakeSocketClosed(ConnectionResetError):
    """Stands in for ``websockets.exceptions.ConnectionClosed``."""


class FakeWS:
    """A websocket whose replies are whatever the fake page says, on cue."""

    def __init__(self, page: FakePage) -> None:
        self.page = page
        self.sent: list[dict[str, Any]] = []
        self.closed = False
        self.inbox: deque[str] = deque()
        self._never = asyncio.Event()

    async def send(self, text: str) -> None:
        message = json.loads(text)
        self.sent.append(message)
        body = self.page.body(message["method"], message.get("params") or {})
        if body is None:  # the page is staying silent - the timeout path
            return
        if isinstance(body, list):  # events, then the reply
            frames = [dict(item) for item in body]
        else:
            frames = [body]
        for frame in frames:
            if "method" not in frame:  # an event keeps its own shape, without an id
                frame.setdefault("id", message["id"])
            self.inbox.append(json.dumps(frame))

    async def recv(self) -> str:
        if self.inbox:
            return self.inbox.popleft()
        await self._never.wait()  # nothing will ever arrive; wait_for() gives up
        return ""  # pragma: no cover - unreachable

    async def close(self) -> None:
        self.closed = True


class FakePage:
    """One ``/json/list`` entry, the probe answer it gives, and its command replies."""

    def __init__(
        self,
        url: str = "https://example.test/",
        *,
        title: str = "Example",
        target_type: str = "page",
        probe: dict[str, Any] | None = None,
        results: list[Any] | None = None,
        error: dict[str, Any] | None = None,
        exception: dict[str, Any] | None = None,
        probe_fails: bool = False,
        socket_errors: int = 0,
        silent: bool = False,
        websocket_url: str | None = None,
    ) -> None:
        self.url = url
        self.title = title
        self.type = target_type
        self.target_id = f"target-{len(url)}-{abs(hash(url)) % 997}"
        self.websocket_url = websocket_url or f"ws://127.0.0.1:{PORT}/devtools/page/{self.target_id}"
        self.probe = probe if probe is not None else {"vis": "visible", "foc": True,
                                                      "url": url, "title": title}
        self.results = list(results or [])
        self.error = error
        self.exception = exception
        self.probe_fails = probe_fails
        self.socket_errors = socket_errors
        self.silent = silent
        self.calls: list[tuple[str, dict[str, Any]]] = []

    # -- /json/list entry --------------------------------------------------
    def entry(self) -> dict[str, Any]:
        return {
            "id": self.target_id,
            "type": self.type,
            "title": self.title,
            "url": self.url,
            "webSocketDebuggerUrl": self.websocket_url,
        }

    def visible(self) -> FakePage:
        self.probe = {**self.probe, "vis": "visible", "foc": True}
        return self

    def hidden(self) -> FakePage:
        self.probe = {**self.probe, "vis": "hidden", "foc": False}
        return self

    def blurred(self) -> FakePage:
        self.probe = {**self.probe, "vis": "visible", "foc": False}
        return self

    def occluded(self) -> FakePage:
        """What a real occluded Chrome window answers: hidden, yet document-focused."""
        self.probe = {**self.probe, "vis": "hidden", "foc": True}
        return self

    def live_calls(self, method: str) -> list[dict[str, Any]]:
        """Commands of *method* that were not the target probe."""
        return [
            params
            for name, params in self.calls
            if name == method and "visibilityState" not in params.get("expression", "")
        ]

    # -- websocket replies -------------------------------------------------
    def body(self, method: str, params: dict[str, Any]) -> Any:
        """The reply frame for one command, without its ``id`` (the socket adds it)."""
        self.calls.append((method, params))
        is_probe = method == "Runtime.evaluate" and "visibilityState" in params.get(
            "expression", ""
        )
        if is_probe:
            if self.probe_fails:
                raise FakeSocketClosed("connection closed by the fake browser")
            return {"result": {"result": {"type": "object", "value": self.probe}}}
        # socket_errors hits live commands only, so a test can fail the command it is
        # about without also taking the target probe down with it
        if self.socket_errors > 0:
            self.socket_errors -= 1
            raise FakeSocketClosed("connection closed by the fake browser")
        if self.silent:
            return None
        if method == "Runtime.evaluate":
            if self.error is not None:
                return {"error": self.error}
            body: dict[str, Any] = {}
            if self.exception is not None:
                body["exceptionDetails"] = self.exception
            if self.results:
                body["result"] = {"type": "object", "value": self.results.pop(0)}
            else:
                body["result"] = {"type": "undefined"}
            return {"result": body}
        return {"result": {}}


class FakeBrowser:
    """A whole fake browser: the HTTP endpoints plus every websocket it hands out."""

    def __init__(self, *pages: FakePage, dead: bool = False, list_404: bool = False) -> None:
        self.pages = list(pages)
        self.dead = dead
        self.list_404 = list_404
        self.opened: list[tuple[str, float]] = []
        self.http_asked: list[tuple[str, float]] = []
        self.sockets: list[FakeWS] = []

    def client(self, **kwargs: Any) -> CdpClient:
        kwargs.setdefault("port", PORT)
        kwargs.setdefault("http_get", self.http_get)
        kwargs.setdefault("open_ws", self.open_ws)
        return CdpClient(**kwargs)

    # -- http seam ---------------------------------------------------------
    async def http_get(self, url: str, timeout: float) -> Any:
        self.http_asked.append((url, timeout))
        if self.dead:
            raise CdpUnavailable(f"{url}: [WinError 10061] connection refused")
        if url.endswith("/json/version"):
            return {"Browser": "CocCoc/133.0", "webSocketDebuggerUrl": "ws://browser"}
        if url.endswith("/json/list"):
            if self.list_404:
                raise CdpHttpError(f"{url}: HTTP 404 Not Found", 404)
            return [page.entry() for page in self.pages]
        if url.endswith("/json"):
            return [page.entry() for page in self.pages]
        raise AssertionError(f"unexpected URL {url}")  # pragma: no cover

    # -- websocket seam ----------------------------------------------------
    async def open_ws(self, url: str, timeout: float) -> FakeWS:
        self.opened.append((url, timeout))
        for page in self.pages:
            if page.websocket_url == url:
                socket = FakeWS(page)
                self.sockets.append(socket)
                return socket
        raise CdpConnectionError(f"{url}: no such target")

    def page_for(self, socket: FakeWS) -> FakePage:
        return socket.page


# ------------------------------------------------------------------- target picking
def test_active_target_is_the_visible_focused_tab_not_the_first_one():
    browser = FakeBrowser(
        FakePage("https://background.test/", title="Background").hidden(),
        FakePage("https://other.test/", title="Other").blurred(),
        FakePage("https://wanted.test/", title="Wanted").visible(),
    )
    client = browser.client()
    target = asyncio.run(client.active_target())
    assert target.url == "https://wanted.test/"
    assert target.title == "Wanted"
    # the losers were probed too - the first entry in /json/list is not the answer
    assert len(browser.opened) == 3


def test_two_focused_tabs_raise_ambiguous_target_and_never_pick_one():
    browser = FakeBrowser(
        FakePage("https://a.test/", title="A").visible(),
        FakePage("https://b.test/", title="B").visible(),
    )
    client = browser.client()
    with pytest.raises(AmbiguousTarget) as caught:
        asyncio.run(client.active_target())
    error = caught.value
    assert len(error.targets) == 2
    assert {t.url for t in error.targets} == {"https://a.test/", "https://b.test/"}
    assert "refusing to guess" in str(error)
    assert all(isinstance(t, Target) for t in error.targets)


def test_no_visible_focused_tab_raises_no_active_tab():
    browser = FakeBrowser(
        FakePage("https://a.test/", title="A").hidden(),
        FakePage("https://b.test/", title="B").blurred(),
    )
    with pytest.raises(NoActiveTab) as caught:
        asyncio.run(browser.client().active_target())
    message = str(caught.value)
    assert "https://a.test/" in message and "https://b.test/" in message
    assert "vis='hidden'" in message and "foc=False" in message


def test_a_lone_occluded_tab_is_still_driven():
    """One page, reporting itself hidden, is the normal case and must not be refused.

    A Chrome window behind the terminal the agent is running in answers
    ``visibilityState: "hidden"`` while still being perfectly drivable. Requiring
    ``visible && focused`` made the single most common automation setup impossible.
    """
    browser = FakeBrowser(FakePage("https://only.test/", title="Only").hidden())
    target = asyncio.run(browser.client().active_target())
    assert target.url == "https://only.test/"


def test_a_lone_occluded_tab_with_document_focus_is_still_driven():
    """The exact shape measured on a real machine: vis='hidden', hasFocus()=True."""
    browser = FakeBrowser(FakePage("https://only.test/", title="Only").occluded())
    target = asyncio.run(browser.client().active_target())
    assert target.url == "https://only.test/"


def test_several_occluded_tabs_still_refuse_to_guess():
    """Relaxing the single-page case must not make multi-tab guessing acceptable."""
    browser = FakeBrowser(
        FakePage("https://a.test/", title="A").hidden(),
        FakePage("https://b.test/", title="B").hidden(),
    )
    with pytest.raises(NoActiveTab) as caught:
        asyncio.run(browser.client().active_target())
    assert "more than one tab" in str(caught.value)


def test_a_page_that_cannot_be_probed_does_not_hide_the_active_one():
    browser = FakeBrowser(
        FakePage("https://crashed.test/", title="Crashed", probe_fails=True),
        FakePage("https://live.test/", title="Live").visible(),
    )
    target = asyncio.run(browser.client().active_target())
    assert target.url == "https://live.test/"


def test_browser_with_no_tabs_at_all_raises_no_active_tab():
    with pytest.raises(NoActiveTab):
        asyncio.run(FakeBrowser().client().active_target())


def test_only_page_targets_are_considered():
    browser = FakeBrowser(
        FakePage("https://worker.test/", title="sw.js", target_type="service_worker").visible(),
        FakePage("https://page.test/", title="page").visible(),
    )
    targets = asyncio.run(browser.client().list_targets())
    assert [t.url for t in targets] == ["https://page.test/"]


# ------------------------------------------------------------------ dead endpoint
def test_dead_endpoint_names_both_flags_and_refuses_to_guess_coordinates():
    browser = FakeBrowser(dead=True)
    with pytest.raises(CdpUnavailable) as caught:
        asyncio.run(browser.client().list_targets())
    message = str(caught.value)
    assert f"--remote-debugging-port={PORT}" in message
    assert "--user-data-dir" in message
    assert "does NOT fall back" in message


def test_dead_endpoint_probes_are_short_and_bounded():
    browser = FakeBrowser(dead=True)
    client = browser.client(http_timeout=1.0)
    with pytest.raises(CdpUnavailable):
        asyncio.run(client.list_targets())
    assert browser.http_asked
    assert all(0 < timeout <= 1.0 for _, timeout in browser.http_asked)
    assert browser.opened == []  # never even tries a socket


def test_list_targets_falls_back_to_json_when_json_list_is_missing():
    browser = FakeBrowser(FakePage("https://a.test/"), list_404=True)
    targets = asyncio.run(browser.client().list_targets())
    assert [t.url for t in targets] == ["https://a.test/"]
    assert [url for url, _ in browser.http_asked][-1].endswith("/json")


# ----------------------------------------------------------------------- evaluate
def test_evaluate_returns_the_value():
    page = FakePage(results=[{"a": 1, "b": [2, 3]}])
    client = FakeBrowser(page).client()
    assert asyncio.run(client.evaluate("({a:1,b:[2,3]})")) == {"a": 1, "b": [2, 3]}


def test_evaluate_asks_for_a_value_and_awaits_promises():
    page = FakePage(results=["done"])
    client = FakeBrowser(page).client()
    asyncio.run(client.evaluate("(async () => 'done')()"))
    params = [p for m, p in page.calls if m == "Runtime.evaluate"][-1]
    assert params["returnByValue"] is True
    assert params["awaitPromise"] is True
    assert params["expression"] == "(async () => 'done')()"


def test_protocol_error_is_surfaced_with_its_message():
    page = FakePage(error={"code": -32000, "message": "Cannot find context with specified id"})
    with pytest.raises(CdpProtocolError) as caught:
        asyncio.run(FakeBrowser(page).client().evaluate("location.href"))
    message = str(caught.value)
    assert "Cannot find context with specified id" in message
    assert "-32000" in message


def test_a_page_side_throw_is_surfaced_not_swallowed():
    page = FakePage(
        exception={
            "text": "Uncaught",
            "exception": {"description": "TypeError: nope is not a function\n  at <anonymous>"},
        }
    )
    with pytest.raises(CdpProtocolError) as caught:
        asyncio.run(FakeBrowser(page).client().evaluate("nope()"))
    assert "TypeError: nope is not a function" in str(caught.value)


def test_evaluate_is_bounded_by_its_timeout():
    page = FakePage(silent=True)  # the fake page never answers
    client = FakeBrowser(page).client()
    started = time.monotonic()
    with pytest.raises(CdpTimeout):
        asyncio.run(client.evaluate("while (true) {}", timeout=0.05))
    assert time.monotonic() - started < 2.0


def test_events_do_not_satisfy_a_command():
    page = FakePage()
    original = page.body

    def body(method: str, params: dict[str, Any]) -> Any:
        if "visibilityState" in params.get("expression", ""):
            return original(method, params)
        return [
            {"method": "Page.frameNavigated", "params": {"frame": {}}},
            {"result": {"result": {"type": "string", "value": "after the event"}}},
        ]

    page.body = body  # type: ignore[method-assign]
    client = FakeBrowser(page).client()
    assert asyncio.run(client.evaluate("'after the event'")) == "after the event"


def test_concurrent_commands_do_not_mix_their_answers():
    page = FakePage(results=["first", "second"])
    client = FakeBrowser(page).client()

    async def both() -> list[Any]:
        return await asyncio.gather(client.evaluate("'first'"), client.evaluate("'second'"))

    assert asyncio.run(both()) == ["first", "second"]


# ------------------------------------------------------------------- connections
def test_a_dropped_socket_is_retried_once_and_the_dead_one_is_dropped():
    page = FakePage(results=["recovered"], socket_errors=1)
    browser = FakeBrowser(page)
    client = browser.client()
    assert asyncio.run(client.evaluate("'recovered'")) == "recovered"
    assert len(page.live_calls("Runtime.evaluate")) == 2  # the call, plus one retry
    assert client.connected is True  # and the dead socket is not the one still held


def test_a_permanently_dead_socket_gives_up_after_exactly_one_retry():
    page = FakePage(socket_errors=99)
    browser = FakeBrowser(page)
    client = browser.client()
    with pytest.raises(CdpConnectionError):
        asyncio.run(client.evaluate("1"))
    assert len(page.live_calls("Runtime.evaluate")) == 2  # no more, no less
    assert client.connected is False


def test_close_marks_the_client_dead_and_is_idempotent():
    page = FakePage(results=[1])
    browser = FakeBrowser(page)
    client = browser.client()
    asyncio.run(client.connect())
    asyncio.run(client.close())
    asyncio.run(client.close())
    assert client.connected is False
    assert client.target is None
    assert all(socket.closed for socket in browser.sockets)


def test_shared_client_is_rebuilt_once_it_is_dead(monkeypatch):
    class Stub:
        def __init__(self) -> None:
            self.connected = False

    made: list[Stub] = []

    def factory() -> Stub:
        stub = Stub()
        made.append(stub)
        return stub

    monkeypatch.setattr(cdp, "CdpClient", factory)
    monkeypatch.setattr(cdp, "_shared", None)
    first = cdp.get_client()
    assert len(made) == 1
    first.connected = True
    assert cdp.get_client() is first and len(made) == 1  # alive: reused
    first.connected = False
    assert cdp.get_client() is not first and len(made) == 2  # dead: never cached


def test_env_port_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv(cdp.PORT_ENV, "9333")
    assert cdp.env_port() == 9333
    monkeypatch.setenv(cdp.PORT_ENV, "not-a-port")
    assert cdp.env_port() == cdp.DEFAULT_PORT
    monkeypatch.delenv(cdp.PORT_ENV)
    assert cdp.env_port() == cdp.DEFAULT_PORT


# ------------------------------------------------------------------ mouse events
def test_dispatch_mouse_event_sends_a_matched_press_and_release():
    page = FakePage()
    client = FakeBrowser(page).client()
    asyncio.run(client.dispatch_mouse_event("mousePressed", 12.5, 34.0))
    asyncio.run(client.dispatch_mouse_event("mouseReleased", 12.5, 34.0))
    events = [params for name, params in page.calls if name == "Input.dispatchMouseEvent"]
    assert [e["type"] for e in events] == ["mousePressed", "mouseReleased"]
    assert all(e["x"] == 12.5 and e["y"] == 34.0 for e in events)
    assert all(e["button"] == "left" and e["clickCount"] == 1 for e in events)
    assert [e["buttons"] for e in events] == [1, 0]
