"""Unit tests for the Windows backend's event building, against a fake user32.

Nothing here touches the real desktop: :class:`FakeUser32` stands in for the DLL, so the
tests can assert exactly which ``INPUT`` records a gesture is made of, in which order
and in how many ``SendInput`` calls. That is where the subtle bugs live - a modifier
released before the key it modifies, a scroll that scrolls the wrong way, a drag that
lands one pixel short.

The live equivalents are in ``test_windows_e2e.py``.
"""

from __future__ import annotations

import asyncio
import ctypes
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from screen_agent.backends import windows as W  # noqa: E402
from screen_agent.backends.base import Cursor  # noqa: E402
from screen_agent.core import Rect, ScreenAgentError, UnsupportedOperation  # noqa: E402
from screen_agent.keys import parse_combo  # noqa: E402

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows only")


# --------------------------------------------------------------------- the fake
class FakeUser32:
    """Records every call instead of touching the desktop."""

    def __init__(self) -> None:
        self.batches: list[list[tuple[str, int, int, int, int]]] = []
        self.cursor_moves: list[tuple[int, int]] = []
        self.cursors: list[tuple[int, int]] = []
        self.position = (500, 400)
        self.fail_after = None
        self.system_cursor_ok = True

    # -- input ---------------------------------------------------------------
    def SendInput(self, count, array, size):  # noqa: N802 - mirrors the Win32 name
        batch = []
        for index in range(count):
            item = array[index]
            if item.type == W._INPUT_MOUSE:
                batch.append(
                    ("mouse", item.mi.dx, item.mi.dy, item.mi.mouseData, item.mi.dwFlags)
                )
            else:
                batch.append(
                    ("key", item.ki.wVk, item.ki.wScan, item.ki.dwFlags, 0)
                )
        self.batches.append(batch)
        return count if self.fail_after is None else self.fail_after

    # -- pointer -------------------------------------------------------------
    def SetCursorPos(self, x, y):  # noqa: N802
        self.cursor_moves.append((x, y))
        return 1

    def LoadCursorW(self, hinstance, name):  # noqa: N802
        return 0x1000 + (ctypes.cast(name, ctypes.c_void_p).value or 0)

    def SetSystemCursor(self, handle, ocr):  # noqa: N802
        self.cursors.append((handle, ocr))
        return handle if self.system_cursor_ok else None

    def SetCursor(self, handle):  # noqa: N802
        self.cursors.append((handle, -1))
        return handle

    def GetCursorPos(self, point):  # noqa: N802 - arrives as a CArgObject from byref()
        point._obj.x, point._obj.y = self.position
        return 1

    def GetSystemMetrics(self, index):  # noqa: N802
        return {0: 1920, 1: 1080, 13: 32, 14: 32, 76: 0, 77: 0, 78: 1920, 79: 1080}.get(
            index, 0
        )

    def GetDesktopWindow(self):  # noqa: N802
        return 0x1234

    def VkKeyScanW(self, ch):  # noqa: N802 - a US layout is enough for these tests
        """low byte = virtual key, high byte = shift (1), ctrl (2) or alt (4)."""
        if ch not in _ASCII:
            return -1
        vk, shift = _ASCII[ch]
        return vk | (shift << 8)

    # -- helpers -------------------------------------------------------------
    def events(self) -> list[tuple]:
        return [event for batch in self.batches for event in batch]

    def keys(self) -> list[tuple[int, int, int]]:
        """(wVk, wScan, dwFlags) for every keyboard event, in order."""
        return [(vk, scan, flags) for kind, vk, scan, flags, _ in self.events()
                if kind == "key"]

    def vks(self) -> list[int]:
        return [vk for vk, _, _ in self.keys()]

    def flags(self) -> list[int]:
        return [flags for _, _, flags in self.keys()]

    def mouse_data(self) -> list[int]:
        """mouseData as the system reads it: a DWORD that means a signed value."""
        return [_signed(data) for _kind, _dx, _dy, data, _flags in self.events()
                if _kind == "mouse"]


#: US layout: character -> (virtual key, needs shift)
_ASCII: dict[str, tuple[int, int]] = {}
_ASCII.update({c: (ord(c.upper()), 0) for c in "abcdefghijklmnopqrstuvwxyz"})
_ASCII.update({c: (ord(c), 1) for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"})
_ASCII.update({c: (ord(c), 0) for c in "0123456789"})
_ASCII.update({
    " ": (0x20, 0), "-": (0xBD, 0), "=": (0xBB, 0), "+": (0xBB, 1), "[": (0xDB, 0),
    "]": (0xDD, 0), ";": (0xBA, 0), "'": (0xDE, 0), "`": (0xC0, 0), ",": (0xBC, 0),
    ".": (0xBE, 0), "/": (0xBF, 0), "\\": (0xDC, 0),
})


def _signed(value: int) -> int:
    return value - 0x100000000 if value & 0x80000000 else value


@pytest.fixture
def backend() -> W.WindowsBackend:
    """A WindowsBackend with the DLL replaced by a recorder."""
    fake = FakeUser32()
    be = W.WindowsBackend()
    be._user32 = fake  # type: ignore[assignment]
    be._desktop = Rect(0, 0, 1920, 1080)
    be.fake = fake  # type: ignore[attr-defined]
    return be


def key_down(vk: int) -> tuple[int, int, int]:
    return (vk, 0, 0)


def key_up(vk: int) -> tuple[int, int, int]:
    return (vk, 0, W._KEYEVENTF_KEYUP)


def char_event(ch: str, up: bool = False) -> tuple[int, int, int]:
    flags = W._KEYEVENTF_UNICODE | (W._KEYEVENTF_KEYUP if up else 0)
    return (0, ord(ch), flags)


# ------------------------------------------------------------------- detection
def test_detect_succeeds_on_a_live_session():
    assert W.WindowsBackend.detect() == ""


def test_desktop_is_the_virtual_screen():
    """SM_CXSCREEN is only the primary monitor; the virtual screen may start negative."""
    be = W.WindowsBackend()
    be._user32 = FakeUser32()  # type: ignore[assignment]
    assert be._query_desktop() == Rect(0, 0, 1920, 1080)

    class Second(FakeUser32):
        def GetSystemMetrics(self, index):  # noqa: N802
            return {76: -1920, 77: -200, 78: 2560, 79: 1440}.get(index, 0)

    be._user32 = Second()  # type: ignore[assignment]
    assert be._query_desktop() == Rect(-1920, -200, 2560, 1440)


def test_desktop_falls_back_when_virtual_metrics_are_zero():
    class NoVirtual(FakeUser32):
        def GetSystemMetrics(self, index):  # noqa: N802
            return {0: 1280, 1: 1024}.get(index, 0)

    be = W.WindowsBackend()
    be._user32 = NoVirtual()  # type: ignore[assignment]
    assert be._query_desktop() == Rect(0, 0, 1280, 1024)


def test_capture_rect_is_clipped_to_the_desktop(backend):
    """A rect hanging off the screen is trimmed, and the target size follows."""
    rect, width, height = backend._clip(Rect(1800, 900, 400, 400), 400, 400)
    assert rect == Rect(1800, 900, 120, 180)
    assert (width, height) == (120, 180)  # no scaling was asked for, so 1:1
    scaled_rect, width, height = backend._clip(Rect(1800, 900, 400, 400), 200, 200)
    assert (scaled_rect.w, scaled_rect.h) == (120, 180)
    assert (width, height) == (60, 90)  # half scale, so half the pixels
    assert backend._clip(Rect(0, 0, 10, 10), 10, 10) == (Rect(0, 0, 10, 10), 10, 10)


def test_capture_rect_must_touch_the_desktop(backend):
    with pytest.raises(ScreenAgentError, match="outside the desktop"):
        backend._grab_bgra(Rect(5000, 5000, 10, 10), 10, 10)


# ---------------------------------------------------------------------- pointer
def test_move_is_clamped_to_the_desktop(backend):
    asyncio.run(backend.move(-500, -500))
    asyncio.run(backend.move(99999, 99999))
    assert backend.fake.cursor_moves == [(0, 0), (1919, 1079)]


def test_move_reports_a_failed_setcursorpos(backend):
    backend.fake.SetCursorPos = lambda x, y: 0  # type: ignore[assignment]
    with pytest.raises(ScreenAgentError, match="SetCursorPos"):
        asyncio.run(backend.move(10, 10))


def test_move_by_uses_an_absolute_move(backend):
    """Injected relative moves are run through the pointer acceleration curve, so a
    relative move has to be turned into an absolute one to land where it was asked."""
    backend.fake.position = (500, 400)
    asyncio.run(backend.move_by(-30, 12))
    (event,) = backend.fake.events()
    assert event[0] == "mouse"
    flags = event[4]
    assert flags & W._MOUSEEVENTF_ABSOLUTE
    assert flags & W._MOUSEEVENTF_VIRTUALDESK
    assert not flags & W._MOUSEEVENTF_ABSOLUTE == 0
    # 470/1919 and 412/1079 of the virtual desktop, normalised to 0..65535
    assert (event[1], event[2]) == (round(470 * 65535 / 1919), round(412 * 65535 / 1079))


def test_move_by_normalises_a_negative_origin(backend):
    """A monitor left of the primary one makes the desktop origin negative."""
    be = W.WindowsBackend()
    be._user32 = backend.fake  # type: ignore[assignment]
    be._desktop = Rect(-1920, -200, 2560, 1440)
    backend.fake.position = (0, 0)
    asyncio.run(be.move_by(0, 0))
    (event,) = backend.fake.events()
    assert (event[1], event[2]) == (round(1920 * 65535 / 2559), round(200 * 65535 / 1439))


def test_click_is_one_batch_of_down_and_up(backend):
    asyncio.run(backend.click(100, 200))
    assert backend.fake.cursor_moves == [(100, 200)]
    assert len(backend.fake.batches) == 1
    assert [event[4] for event in backend.fake.batches[0]] == [
        W._MOUSEEVENTF_LEFTDOWN, W._MOUSEEVENTF_LEFTUP
    ]


def test_double_click_is_two_batches(backend):
    asyncio.run(backend.click(10, 10, clicks=2))
    assert len(backend.fake.batches) == 2
    assert all(len(batch) == 2 for batch in backend.fake.batches)


def test_click_buttons_map_to_the_right_flags(backend):
    for name, down, up in (
        ("right", W._MOUSEEVENTF_RIGHTDOWN, W._MOUSEEVENTF_RIGHTUP),
        ("middle", W._MOUSEEVENTF_MIDDLEDOWN, W._MOUSEEVENTF_MIDDLEUP),
    ):
        backend.fake.batches.clear()
        asyncio.run(backend.click(None, None, button=name))
        assert [event[4] for event in backend.fake.batches[0]] == [down, up]


def test_x_buttons_carry_their_data(backend):
    asyncio.run(backend.click(None, None, button="back"))
    data = {event[3] for event in backend.fake.batches[0]}
    assert data == {W._XBUTTON1}


def test_unknown_button_is_rejected(backend):
    with pytest.raises(ScreenAgentError, match="unknown button"):
        asyncio.run(backend.click(None, None, button="pinky"))


def test_drag_lands_exactly_on_the_target(backend):
    """Every step is an absolute move, and the last one is exactly (x2, y2)."""
    asyncio.run(backend.drag(100, 100, 400, 250, steps=7))
    assert backend.fake.cursor_moves == [(100, 100)]  # homed with SetCursorPos
    moves = [(dx, dy, flags) for kind, dx, dy, _, flags in backend.fake.events()
             if kind == "mouse" and flags & W._MOUSEEVENTF_ABSOLUTE]
    assert len(moves) == 7
    assert all(flags & W._MOUSEEVENTF_VIRTUALDESK for _, _, flags in moves)
    desktop = backend.desktop
    last = moves[-1]
    assert _denormalise(last[0], last[1], desktop) == (400, 250)


def _denormalise(nx: int, ny: int, rect: Rect) -> tuple[int, int]:
    """Undo the 0..65535 normalisation of an absolute mouse event."""
    return (
        rect.x + round(nx * (rect.w - 1) / 65535),
        rect.y + round(ny * (rect.h - 1) / 65535),
    )


def test_drag_presses_and_releases_around_the_moves(backend):
    asyncio.run(backend.drag(0, 0, 30, 0, steps=3))
    flags = [event[4] for event in backend.fake.events() if event[0] == "mouse"]
    assert flags[0] == W._MOUSEEVENTF_LEFTDOWN
    assert flags[-1] == W._MOUSEEVENTF_LEFTUP
    assert all(flag & W._MOUSEEVENTF_ABSOLUTE for flag in flags[1:-1])


# ---------------------------------------------------------------------- scroll
def test_scroll_down_sends_one_event_per_notch(backend):
    """Windows coalesces back-to-back wheel messages, so notches are spaced out."""
    asyncio.run(backend.scroll(3, "down"))
    assert backend.fake.mouse_data() == [-W._WHEEL_DELTA] * 3
    assert all(event[4] == W._MOUSEEVENTF_WHEEL for event in backend.fake.events())


def test_scroll_up_is_a_positive_delta(backend):
    asyncio.run(backend.scroll(2, "up"))
    assert backend.fake.mouse_data() == [W._WHEEL_DELTA] * 2


def test_horizontal_scroll_uses_the_other_flag(backend):
    asyncio.run(backend.scroll(1, "right", horizontal=True))
    event = backend.fake.events()[0]
    assert event[4] == W._MOUSEEVENTF_HWHEEL
    assert backend.fake.mouse_data() == [-W._WHEEL_DELTA]  # negative == left in HWHEEL


def test_scroll_direction_is_validated(backend):
    with pytest.raises(ScreenAgentError):
        asyncio.run(backend.scroll(1, "sideways"))


# --------------------------------------------------------------------- keyboard
def test_named_key_is_down_then_up_in_one_batch(backend):
    asyncio.run(backend.key_press("Return"))
    assert backend.fake.keys() == [key_down(0x0D), key_up(0x0D)]
    assert len(backend.fake.batches) == 1


def test_modifier_wraps_the_key(backend):
    asyncio.run(backend.key_press("ctrl+shift+t"))
    assert backend.fake.vks() == [0x11, 0x10, 0x54, 0x54, 0x10, 0x11]
    assert backend.fake.keys()[-1][2] & W._KEYEVENTF_KEYUP
    assert backend.fake.keys()[-2][2] & W._KEYEVENTF_KEYUP


def test_alt_f4_is_a_real_f4(backend):
    """alt+F4 must be VK_F4, not a dead alt+unicode-F."""
    asyncio.run(backend.key_press("alt+F4"))
    assert backend.fake.vks() == [0x12, 0x73, 0x73, 0x12]  # 0x73 == VK_F4
    assert 0x10 not in backend.fake.vks()  # no shift: alt+F4 is not alt+shift+F4


def test_uppercase_letter_adds_shift(backend):
    asyncio.run(backend.key_press("A"))
    assert backend.fake.vks() == [0x10, 0x41, 0x41, 0x10]


def test_explicit_shift_is_not_doubled(backend):
    asyncio.run(backend.key_press("shift+a"))
    assert backend.fake.vks() == [0x10, 0x41, 0x41, 0x10]


def test_layout_unknown_character_falls_back_to_unicode(backend):
    asyncio.run(backend.key_press("€"))
    assert backend.fake.keys() == [char_event("€"), char_event("€", up=True)]


def test_extended_keys_carry_the_flag(backend):
    for name in ("left", "right", "up", "down", "home", "end", "pageup", "pagedown",
                 "insert", "delete", "super", "numlock"):
        backend.fake.batches.clear()
        asyncio.run(backend.key_press(name))
        vk, _, flags = backend.fake.keys()[0]
        assert flags & W._KEYEVENTF_EXTENDEDKEY, name


def test_plain_keys_do_not_carry_the_extended_flag(backend):
    for name in ("return", "tab", "space", "a", "f5", "printscreen", "shift"):
        backend.fake.batches.clear()
        asyncio.run(backend.key_press(name))
        assert not backend.fake.keys()[0][2] & W._KEYEVENTF_EXTENDEDKEY, name


def test_oem_punctuation_is_typed_literally(backend):
    """A named punctuation key means the character, not the physical key: the bracket
    key is a dead key on a Vietnamese layout, so it has to bypass the layout entirely."""
    for name, char in (
        ("equal", "="), ("plus", "+"), ("minus", "-"), ("comma", ","), ("period", "."),
        ("slash", "/"), ("semicolon", ";"), ("apostrophe", "'"), ("grave", "`"),
        ("lbracket", "["), ("rbracket", "]"), ("backslash", "\\"),
    ):
        backend.fake.batches.clear()
        asyncio.run(backend.key_press(name))
        assert backend.fake.keys() == [char_event(char), char_event(char, up=True)], name


def test_single_characters_use_the_layout(backend):
    """A typed character goes through VkKeyScanW, so ctrl+c is a real C key event."""
    backend.fake.batches.clear()
    asyncio.run(backend.key_press("ctrl+c"))
    assert backend.fake.vks() == [0x11, 0x43, 0x43, 0x11]
    assert backend.fake.batches[0][1][2] == 0  # a virtual key, not a scan code


def test_keypad_digits_have_virtual_keys(backend):
    for digit in range(10):
        backend.fake.batches.clear()
        asyncio.run(backend.key_press(f"kp{digit}"))
        assert backend.fake.vks() == [0x60 + digit, 0x60 + digit]


def test_fn_and_hyper_are_rejected_with_a_reason(backend):
    for name in ("fn", "hyper", "ctrl+fn"):
        with pytest.raises(UnsupportedOperation, match="modifier|firmware"):
            asyncio.run(backend.key_press(name))


def test_type_text_sends_unicode_pairs(backend):
    count = asyncio.run(backend.type_text("aA!"))
    assert count == 3
    assert backend.fake.keys() == [
        char_event("a"), char_event("a", up=True),
        char_event("A"), char_event("A", up=True),
        char_event("!"), char_event("!", up=True),
    ]


def test_type_text_uses_real_keys_for_newline_and_tab(backend):
    """Literal text goes out as Unicode; \\n and \\t as the keys they stand for."""
    asyncio.run(backend.type_text("a\n\t"))
    assert backend.fake.keys() == [
        char_event("a"), char_event("a", up=True),
        key_down(0x0D), key_up(0x0D),
        key_down(0x09), key_up(0x09),
    ]


def test_type_text_splits_astral_characters(backend):
    """KEYEVENTF_UNICODE takes UTF-16 units, so an emoji needs a surrogate pair."""
    asyncio.run(backend.type_text("\U0001F600"))
    scans = [scan for _vk, scan, _flags in backend.fake.keys()]
    assert scans == [0xD83D, 0xD83D, 0xDE00, 0xDE00]  # high down/up, low down/up


def test_type_text_skips_unprintable_control_characters(backend):
    assert asyncio.run(backend.type_text("a\x00\x01b")) == 2


# ---------------------------------------------------------------------- cursor
def test_cursor_shape_uses_the_right_slot(backend):
    asyncio.run(backend.move(10, 10, cursor=Cursor.TEXT))
    _handle, ocr = backend.fake.cursors[-1]
    assert ocr == 0x0001  # OCR_IBEAM


def test_default_shape_never_replaces_the_system_arrow(backend):
    """SetSystemCursor(IDC_ARROW, OCR_NORMAL) is a no-op at best, and Windows is free
    to treat it as replacing a system cursor, so moving with the default shape must
    not touch it."""
    backend.fake.cursors.clear()
    asyncio.run(backend.move(10, 10))
    asyncio.run(backend.move(20, 20, cursor=Cursor.DEFAULT))
    assert backend.fake.cursors == []


def test_default_shape_restores_a_previous_shape(backend):
    asyncio.run(backend.move(10, 10, cursor=Cursor.TEXT))
    assert backend._cursor_slots == {0x0001}
    asyncio.run(backend.move(20, 20, cursor=Cursor.DEFAULT))
    assert backend._cursor_slots == set()
    assert backend._cursor_shape is None


def test_cursor_shape_falls_back_to_setcursor(backend):
    """Some window stations refuse SetSystemCursor; SetCursor is then the best effort."""
    backend.fake.system_cursor_ok = False
    asyncio.run(backend.move(10, 10, cursor=Cursor.WAIT))
    assert backend._cursor_shape == Cursor.WAIT
    assert any(note for note in backend._notes if "SetSystemCursor" in note)


def test_cursor_shape_refusal_is_only_a_note(backend):
    backend.fake.system_cursor_ok = False
    backend.fake.SetCursor = lambda handle: None  # type: ignore[assignment]
    asyncio.run(backend.move(10, 10, cursor=Cursor.WAIT))  # must not raise
    assert any("ignored" in note for note in backend._notes)


def test_resize_cursors_map_to_distinct_stock_shapes(backend):
    for shape, ocr in (("n-resize", 0x0004), ("s-resize", 0x0005), ("e-resize", 0x000C)):
        backend.fake.cursors.clear()
        asyncio.run(backend.move(5, 5, cursor=shape))
        assert backend.fake.cursors[-1][1] == ocr, shape


def test_cursor_shape_is_only_applied_once(backend):
    asyncio.run(backend.move(5, 5, cursor=Cursor.HAND))
    asyncio.run(backend.move(6, 6, cursor=Cursor.HAND))
    assert len(backend.fake.cursors) == 1


def test_close_restores_the_cursors(backend):
    asyncio.run(backend.move(5, 5, cursor=Cursor.CROSSHAIR))
    asyncio.run(backend.close())
    assert backend.fake.cursors[-1][1] == 0x0002  # OCR_CROSS back to IDC_CROSS
    assert backend._cursor_slots == set()


# -------------------------------------------------------------------- batching
def test_partial_sendinput_is_reported(backend):
    backend.fake.fail_after = 1
    with pytest.raises(ScreenAgentError, match="SendInput delivered 1 of 2"):
        asyncio.run(backend.click(None, None))


# ----------------------------------------------------------------- key coverage
def test_windows_virtual_keys_cover_every_parsed_key():
    """Every key the parser accepts must resolve to a VK or a layout character."""
    from screen_agent.keys import NAMED_KEYS

    for name in sorted(set(NAMED_KEYS) | {"f1", "f24", "kp0", "kp9"}):
        if name in ("fn", "hyper"):
            continue  # firmware keys: Windows never sends them to applications
        combo = parse_combo(name)
        assert W._VK.get(combo.key) or W._KEY_CHARS.get(combo.key), f"no key for {name!r}"


def test_every_named_key_presses_without_raising(backend):
    from screen_agent.keys import NAMED_KEYS

    for name in sorted(NAMED_KEYS):
        if name in ("fn", "hyper"):
            continue
        backend.fake.batches.clear()
        asyncio.run(backend.key_press(name))
        assert backend.fake.batches, name


def test_info_reports_the_desktop_and_caveats(backend):
    info = asyncio.run(backend.info())
    text = info.render()
    assert info.backend == "windows"
    assert info.platform == "win32"
    assert info.capabilities["screenshot"] is True
    assert info.capabilities["record"] is True
    assert info.capabilities["absolute_pointer"] is True
    assert "DPI awareness" in text
    assert "interactive desktop session" in text
