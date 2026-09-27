"""Live tests against the real Windows desktop: capture, record and injected input.

These move the mouse, type into a window and steal focus, so they only run when asked
for explicitly::

    SCREEN_AGENT_E2E=1 python -m pytest tests/test_windows_e2e.py -v

Screenshots and recording are harmless enough to keep enabled by default on Windows;
the input tests need ``SCREEN_AGENT_E2E`` because they move the pointer and type.

The input side needs a real target, so these drive a throwaway Win32 window (see
``tests/win32app.py``) and read back what it received, instead of asserting on mocks.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from screen_agent.backends import load_backend  # noqa: E402
from screen_agent.core import Rect, parse_region, scale_image  # noqa: E402

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows only")

E2E = os.environ.get("SCREEN_AGENT_E2E") == "1"


@pytest.fixture(scope="module")
def backend():
    """A live Windows backend. Creating it also makes the process DPI aware."""
    instance = load_backend()
    assert instance.name == "windows", f"expected the windows backend, got {instance.name}"
    return instance


# ------------------------------------------------------------------- capture
def test_desktop_is_a_plausible_rect(backend):
    desktop = backend.desktop
    assert desktop.w > 0 and desktop.h > 0
    assert desktop.w * desktop.h > 100_000, f"suspiciously small desktop {desktop}"


def test_capture_matches_the_desktop(backend, tmp_path: Path):
    from screen_agent.imaging import decode_png

    target = tmp_path / "screen.png"
    desktop = backend.desktop
    size = asyncio.run(backend.capture_png(target, desktop, cursor=False))
    assert size == (desktop.w, desktop.h)
    assert _png_size(target) == (desktop.w, desktop.h)
    width, height, pixels = decode_png(target.read_bytes())
    assert (width, height) == (desktop.w, desktop.h)
    assert len(pixels) == width * height * 3
    # a real desktop is never one flat colour; a blank capture means a locked session
    assert len(set(pixels[:4096])) > 4, "the capture looks like a blank screen"


def test_capture_region_is_precise(backend, tmp_path: Path):
    from screen_agent.imaging import decode_png

    desktop = backend.desktop
    rect = parse_region("0,0,320,240", desktop)
    target = tmp_path / "region.png"
    assert asyncio.run(backend.capture_png(target, rect, cursor=False)) == (320, 240)
    width, height, _pixels = decode_png(target.read_bytes())
    assert (width, height) == (320, 240)


def test_capture_with_cursor_and_without(backend, tmp_path: Path):
    """The pointer has to appear in the image when it is asked for.

    Windows suppresses the pointer over some windows (a console with its own cursor
    handling, for one), and a suppressed pointer has no handle to draw, so the test
    walks a few positions until it finds one where the cursor is actually showing.
    """
    desktop = backend.desktop
    plain = tmp_path / "plain.png"
    with_pointer = tmp_path / "pointer.png"
    for x, y in _cursor_positions(desktop):
        asyncio.run(backend.move(x, y))
        time.sleep(0.2)
        if not _cursor_is_showing():
            continue
        # centre the region on the pointer, so the whole shape is inside the capture
        rect = Rect(x - 80, y - 60, 160, 120)
        asyncio.run(backend.capture_png(plain, rect, cursor=False))
        asyncio.run(backend.capture_png(with_pointer, rect, cursor=True))
        assert plain.read_bytes() != with_pointer.read_bytes(), (
            f"the pointer was not drawn at ({x}, {y})"
        )
        return
    pytest.skip("the pointer is suppressed everywhere this session shows it")


def test_region_parser_clamps_into_the_desktop(backend):
    desktop = backend.desktop
    assert parse_region("full", desktop) == desktop
    edge = parse_region(f"{desktop.x + desktop.w - 10},{desktop.y},9999,9999", desktop)
    assert (edge.x + edge.w, edge.y + edge.h) == (desktop.x + desktop.w, desktop.y + desktop.h)


def test_scaling_a_screenshot(backend, tmp_path: Path):
    from screen_agent.imaging import decode_png

    target = tmp_path / "scaled.png"
    rect = Rect(backend.desktop.x, backend.desktop.y, 800, 600)
    asyncio.run(backend.capture_png(target, rect, cursor=False))
    width, height = scale_image(target, 400)
    assert width == 400 and height == 300
    assert decode_png(target.read_bytes())[:2] == (400, 300)


# ------------------------------------------------------------------ recording
@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")
def test_record_with_ffmpeg(backend, tmp_path: Path):
    target = tmp_path / "clip.webm"
    started = time.time()
    result = asyncio.run(backend.record_video(target, 1.2))
    assert result.suffix == ".webm"
    assert result.exists() and result.stat().st_size > 5_000
    # 10 fps for 1.2 s: the recorder must not come back long before it is done
    assert time.time() - started >= 1.0
    assert result.read_bytes()[:4] == b"\x1a\x45\xdf\xa3"  # EBML, i.e. real Matroska


def test_record_falls_back_to_animated_png(backend, tmp_path: Path):
    """No usable ffmpeg has to produce something watchable all the same."""
    pytest.importorskip("PIL.Image")
    from PIL import Image

    from screen_agent.imaging import decode_png

    backend._encoders = set()  # pretend ffmpeg cannot encode anything
    try:
        target = tmp_path / "clip.webm"
        result = asyncio.run(backend.record_video(target, 1.0))
    finally:
        backend._encoders = None
    assert result.suffix == ".apng"
    assert result.exists() and result.stat().st_size > 1_000
    with Image.open(result) as image:
        assert image.n_frames >= 2 and image.is_animated
        image.seek(1)
        frame = image.convert("RGB")
    assert frame.size == decode_png(result.read_bytes())[:2]
    assert len(set(frame.tobytes()[:2048])) > 1, "every APNG frame is identical"


# --------------------------------------------------------------------- input
@pytest.mark.skipif(not E2E, reason="set SCREEN_AGENT_E2E=1 to drive the real desktop")
class TestInjectedInput:
    """Real SendInput events into a real window, verified by what the window received."""

    @pytest.fixture
    def target(self, backend):
        from win32app import TestWindow

        window = TestWindow()
        window.focus()  # bring it forward so the click below lands on it
        asyncio.run(backend.click(*window.center("edit")))
        time.sleep(0.2)
        try:
            yield window
        finally:
            window.close()

    def test_typing_lands_in_the_focused_window(self, backend, target):
        assert asyncio.run(backend.type_text("Hello Win 123 !@#")) == 17
        time.sleep(0.2)
        assert target.text() == "Hello Win 123 !@#"

    def test_click_positions_the_caret(self, backend, target):
        """A click must land on the character under the coordinates, not nearby."""
        target.set_text("Hello world this is a long line of text")
        time.sleep(0.2)
        x, y, _w, _h = target.edit_rect()
        asyncio.run(backend.click(x + 60, y + 8))
        time.sleep(0.25)
        first = target.selection()[0]
        asyncio.run(backend.click(x + 150, y + 8))
        time.sleep(0.25)
        second = target.selection()[0]
        assert 5 < first < 10, f"click landed at character {first}"
        assert second > first + 10, f"the second click did not move the caret ({second})"

    def test_double_click_selects_a_word(self, backend, target):
        target.set_text("alpha beta gamma")
        time.sleep(0.2)
        x, y, _w, _h = target.edit_rect()
        asyncio.run(backend.click(x + 10, y + 8, clicks=2))
        time.sleep(0.25)
        start, end = target.selection()
        assert end - start >= 4, f"double click selected {end - start} characters"

    def test_ctrl_combo_reaches_the_control(self, backend, target):
        asyncio.run(backend.type_text("select me"))
        asyncio.run(backend.key_press("ctrl+a"))
        time.sleep(0.2)
        start, end = target.selection()
        assert (start, end) == (0, 9)
        asyncio.run(backend.type_text("X"))
        time.sleep(0.2)
        assert target.text() == "X"

    def test_named_keys_type_the_right_characters(self, backend, target):
        """Named punctuation must type the character asked for, whatever the layout:
        the physical bracket key is a dead key on a Vietnamese layout."""
        for combo, expected in (
            ("A", "A"), ("5", "5"), ("equal", "="), ("plus", "+"), ("minus", "-"),
            ("comma", ","), ("period", "."), ("slash", "/"), ("semicolon", ";"),
            ("lbracket", "["), ("rbracket", "]"), ("backslash", "\\"), ("space", " "),
            ("F7", ""), ("ctrl+a", ""),  # ctrl+a selects, it does not type
        ):
            target.clear()
            asyncio.run(backend.key_press(combo))
            time.sleep(0.2)
            assert target.text() == expected, f"{combo!r} produced {target.text()!r}"

    def test_return_and_backspace(self, backend, target):
        asyncio.run(backend.type_text("one\ntwo"))
        time.sleep(0.2)
        # a multiline EDIT reports CRLF, which is what Return really inserts
        assert _lines(target.text()) == ["one", "two"]
        asyncio.run(backend.key_press("BackSpace"))
        time.sleep(0.2)
        assert _lines(target.text()) == ["one", "tw"]

    def test_drag_selects_text(self, backend, target):
        target.set_text("Hello world this is a long line of text")
        time.sleep(0.2)
        x, y, width, _h = target.edit_rect()
        asyncio.run(backend.drag(x + 150, y + 8, x + 5, y + 8, steps=12))
        time.sleep(0.3)
        start, end = target.selection()
        assert end - start > 10, f"the drag selected only {end - start} characters"
        asyncio.run(backend.type_text("Z"))
        time.sleep(0.2)
        assert target.text() == "Z long line of text"

    def test_drag_moves_the_pointer_to_the_end(self, backend, target):
        import ctypes
        import ctypes.wintypes as wintypes

        x, y, width, _h = target.edit_rect()
        asyncio.run(backend.drag(x + 20, y + 8, x + 200, y + 60, steps=10))
        time.sleep(0.2)
        point = wintypes.POINT()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(point))
        assert abs(point.x - (x + 200)) <= 1 and abs(point.y - (y + 60)) <= 1, (
            f"pointer ended at {point.x},{point.y} instead of {x + 200},{y + 60}"
        )

    def test_scroll_reaches_the_focused_window(self, backend, target):
        """Windows routes the wheel to the focus window, so the frame has to have it."""
        target.focus_self()
        time.sleep(0.2)
        x, y, width, _height = target.rect()
        asyncio.run(backend.move(x + width // 2, y + 3))  # over the frame, not a child
        time.sleep(0.2)
        asyncio.run(backend.scroll(3, "down"))
        time.sleep(0.3)
        assert target.wheel_deltas() == [-120, -120, -120]
        asyncio.run(backend.scroll(2, "up"))
        time.sleep(0.3)
        assert target.wheel_deltas()[-2:] == [120, 120]

    def test_horizontal_scroll_sign(self, backend, target):
        """Injected horizontal wheel events are not delivered everywhere (some VMs and
        remote sessions drop them), so probe first and skip rather than fail."""
        target.focus_self()
        x, y, width, _height = target.rect()
        asyncio.run(backend.move(x + width // 2, y + 3))
        time.sleep(0.2)
        asyncio.run(backend.scroll(1, "right", horizontal=True))
        time.sleep(0.3)
        if not target.wheel_deltas():
            pytest.skip("this session does not deliver injected horizontal wheel events")
        asyncio.run(backend.scroll(1, "left", horizontal=True))
        time.sleep(0.3)
        assert target.wheel_deltas() == [-120, 120]

    def test_cursor_shape_sticks_and_is_restored(self, backend, target):
        """The shape is cosmetic: it is attempted, and whatever it changed is undone."""
        from screen_agent.backends.base import Cursor

        asyncio.run(backend.move(*target.center("edit"), cursor=Cursor.CROSSHAIR))
        assert backend._cursor_shape == Cursor.CROSSHAIR or backend._notes, (
            "the cursor shape was neither applied nor reported"
        )
        asyncio.run(backend.close())
        assert not backend._cursor_slots, "the cursor shape was not restored"
        assert backend._cursor_shape is None

    def test_right_click_does_not_type(self, backend, target):
        asyncio.run(backend.click(*target.center("edit"), button="right"))
        time.sleep(0.2)
        assert target.text() == ""

    def test_unknown_button_is_rejected(self, backend, target):
        from screen_agent.core import ScreenAgentError

        with pytest.raises(ScreenAgentError):
            asyncio.run(backend.click(*target.center("edit"), button="thumb"))


# --------------------------------------------------------------------- helpers
def _cursor_positions(desktop) -> list[tuple[int, int]]:
    """Centres to try, for finding a window that shows a pointer.

    Whether a pointer is visible is up to the window class under it: this session hides
    the cursor over the editor but shows it over the taskbar, so a single spot is not
    enough. Each point is a capture *centre*, so a 160x120 region around it stays inside
    the desktop.
    """
    half_w, half_h = 80, 60
    xs = [40 + half_w, 400 + half_w, 800 + half_w, 1200 + half_w,
          max(half_w, desktop.w - half_w - 40)]
    ys = [40 + half_h, 300 + half_h, 600 + half_h,
          max(half_h, desktop.h // 2),
          desktop.h - half_h]  # the taskbar row, which usually shows a pointer
    return [(desktop.x + x, desktop.y + y) for y in ys for x in xs]


def _cursor_is_showing() -> bool:
    """True when the pointer is visible and has a handle Windows will let us draw."""
    import ctypes
    import ctypes.wintypes as wintypes

    from screen_agent.backends import windows as W

    info = W._CURSORINFO()
    info.cbSize = ctypes.sizeof(info)
    user32 = ctypes.WinDLL("user32", use_last_error=True)  # type: ignore[attr-defined]
    user32.GetCursorInfo.argtypes = [ctypes.POINTER(W._CURSORINFO)]
    user32.GetCursorInfo.restype = wintypes.BOOL
    if not user32.GetCursorInfo(ctypes.byref(info)):
        return False
    return bool(info.flags & 1) and bool(info.hCursor)  # CURSOR_SHOWING


def _lines(text: str) -> list[str]:
    """Split on any line ending: a Win32 EDIT reports CRLF for a single Return."""
    return [line for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]


def _png_size(path: Path) -> tuple[int, int]:
    from screen_agent.core import _png_size

    return _png_size(path)
