"""A throwaway Win32 window used to verify injected input on Windows.

The point is to check ``SendInput`` against something that actually reports what it
received, not against a mock: a real top-level window with a real ``EDIT`` and
``LISTBOX`` child, created on its own thread with its own message loop, so a test can

* click the edit box and read back what ``GetWindowTextW`` says,
* press combos such as ``ctrl+a`` and watch the selection change,
* drag across the text to select it and type over it,
* scroll the wheel over the list box and watch the top index move.

Create the backend *before* this window: the backend makes the process DPI aware, and a
window created before that would be laid out in logical pixels while clicks arrive in
physical ones.

Windows only - import it from a Windows-only test.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import itertools
import threading
import time

__all__ = ["TestWindow", "foreground"]

_user32 = ctypes.WinDLL("user32", use_last_error=True)  # type: ignore[attr-defined]
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]

_LRESULT = ctypes.c_ssize_t
_WNDPROC = ctypes.WINFUNCTYPE(
    _LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
)
_LPARAM = ctypes.c_void_p

# messages
_WM_DESTROY = 0x0002
_WM_CLOSE = 0x0010
_WM_SETTEXT = 0x000C
_WM_GETTEXTLENGTH = 0x000E
_WM_MOUSEWHEEL = 0x020A
_WM_MOUSEHWHEEL = 0x023E  # a horizontal wheel is a different message, not a flag
_WM_APP_FOCUS_EDIT = 0x8001
_WM_APP_FOCUS_LIST = 0x8002
_WM_APP_FOCUS_SELF = 0x8003

# control messages
_EM_SETSEL = 0x00B1
_EM_GETSEL = 0x00B0
_LB_GETTOPINDEX = 0x018E
_LB_GETCOUNT = 0x018B
_LB_ADDSTRING = 0x0180
_LB_SETTOPINDEX = 0x0197

# styles
_ES_MULTILINE = 0x0004
_ES_AUTOHSCROLL = 0x0080
_ES_WANTRETURN = 0x1000
_LBS_DISPLAYHEIGHT = 0x0080
_WS_CHILD = 0x40000000
_WS_VISIBLE = 0x10000000
_WS_BORDER = 0x00800000
_WS_OVERLAPPEDWINDOW = 0x00CF0000
_WS_EX_TOPMOST = 0x00000008
_SW_SHOW = 5


class _WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", _WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


class _MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt", wintypes.POINT),
    ]


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long), ("top", ctypes.c_long),
        ("right", ctypes.c_long), ("bottom", ctypes.c_long),
    ]


# every signature that carries a pointer or a 64-bit value needs declaring, otherwise
# ctypes narrows it to c_int and the call raises OverflowError
_user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                                  wintypes.LPARAM]
_user32.DefWindowProcW.restype = _LRESULT
_user32.GetMessageW.argtypes = [ctypes.POINTER(_MSG), wintypes.HWND, wintypes.UINT,
                                wintypes.UINT]
_user32.GetMessageW.restype = ctypes.c_int
_user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, _LPARAM]
_user32.SendMessageW.restype = _LRESULT
_user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, _LPARAM]
_user32.PostMessageW.restype = wintypes.BOOL
_user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                    wintypes.DWORD, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                    wintypes.HMENU, wintypes.HINSTANCE, _LPARAM]
_user32.CreateWindowExW.restype = wintypes.HWND
_user32.DestroyWindow.argtypes = [wintypes.HWND]
_user32.SetFocus.argtypes = [wintypes.HWND]
_user32.SetFocus.restype = wintypes.HWND
_user32.SetForegroundWindow.argtypes = [wintypes.HWND]
_user32.SetForegroundWindow.restype = wintypes.BOOL
_user32.SetActiveWindow.argtypes = [wintypes.HWND]
_user32.GetForegroundWindow.restype = wintypes.HWND
_user32.BringWindowToTop.argtypes = [wintypes.HWND]
_user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.UpdateWindow.argtypes = [wintypes.HWND]
_user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(_RECT)]
_user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
_user32.GetWindowTextLengthW.restype = ctypes.c_int
_user32.RegisterClassW.argtypes = [ctypes.POINTER(_WNDCLASSW)]
_user32.PostQuitMessage.argtypes = [ctypes.c_int]
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, _LPARAM]
_user32.GetWindowThreadProcessId.restype = wintypes.DWORD
_user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
_kernel32.GetCurrentThreadId.restype = wintypes.DWORD
_kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
_kernel32.GetModuleHandleW.restype = wintypes.HMODULE

#: Window classes live in the process, and a class keeps a raw pointer to its callback,
#: so every callback is parked here for the lifetime of the test session. Letting one be
#: collected leaves the class pointing at freed memory and the next window crashes.
_LIVE_CALLBACKS: list = []
_CLASS_SEQ = itertools.count()


def _rect(hwnd) -> tuple[int, int, int, int]:
    box = _RECT()
    if not _user32.GetWindowRect(hwnd, ctypes.byref(box)):
        raise OSError(ctypes.get_last_error(), "GetWindowRect failed")
    return box.left, box.top, box.right - box.left, box.bottom - box.top


def _text(hwnd) -> str:
    length = int(_user32.GetWindowTextLengthW(hwnd))
    buffer = ctypes.create_unicode_buffer(max(2, length + 1))
    _user32.GetWindowTextW(hwnd, buffer, len(buffer))
    return buffer.value


def _send(hwnd, message: int, wparam: int = 0, lparam=None) -> int:
    """``SendMessageW`` that accepts a Python string as *lparam*."""
    arg = ctypes.c_wchar_p(lparam) if isinstance(lparam, str) else lparam
    return int(_user32.SendMessageW(hwnd, message, wparam, arg))


def foreground(hwnd) -> bool:
    """Best effort: make *hwnd* the foreground window.

    A process may only call ``SetForegroundWindow`` if it owns the current foreground
    window or received the last input event, so we try the plain call and fall back to
    the ``AttachThreadInput`` trick, which pairs our message queue with the target's.
    The tests also click the window, which focuses it by definition.
    """
    if _user32.SetForegroundWindow(hwnd) and _user32.GetForegroundWindow() == hwnd:
        return True
    target_thread = _user32.GetWindowThreadProcessId(hwnd, None)
    current_thread = _kernel32.GetCurrentThreadId()
    attached = bool(_user32.AttachThreadInput(current_thread, target_thread, True))
    try:
        _user32.BringWindowToTop(hwnd)
        _user32.SetForegroundWindow(hwnd)
        _user32.SetActiveWindow(hwnd)
    finally:
        if attached:
            _user32.AttachThreadInput(current_thread, target_thread, False)
    return _user32.GetForegroundWindow() == hwnd


class TestWindow:
    """A topmost window with an ``EDIT`` on top and a 200-item ``LISTBOX`` below."""

    def __init__(self, title: str = "screen-agent input target", width: int = 520,
                 height: int = 320, x: int = 80, y: int = 80) -> None:
        self._ready = threading.Event()
        self._error: BaseException | None = None
        self._keep_alive: _WNDPROC | None = None
        self.hwnd = 0
        self.edit = 0
        self.listbox = 0
        self._wheel: list[int] = []
        self._thread = threading.Thread(
            target=self._run, args=(title, width, height, x, y), daemon=True,
            name="screen-agent-test-window",
        )
        self._thread.start()
        if not self._ready.wait(10) or self._error is not None:
            raise self._error or TimeoutError("the test window never appeared")

    # ------------------------------------------------------------------ geometry
    def rect(self) -> tuple[int, int, int, int]:
        return _rect(self.hwnd)

    def edit_rect(self) -> tuple[int, int, int, int]:
        return _rect(self.edit)

    def listbox_rect(self) -> tuple[int, int, int, int]:
        return _rect(self.listbox)

    def center(self, which: str = "edit") -> tuple[int, int]:
        x, y, w, h = self.edit_rect() if which == "edit" else self.listbox_rect()
        return x + w // 2, y + h // 2

    # -------------------------------------------------------------------- content
    def text(self) -> str:
        return _text(self.edit)

    def set_text(self, value: str) -> None:
        _send(self.edit, _WM_SETTEXT, 0, value)

    def clear(self) -> None:
        self.set_text("")

    def select_all(self) -> None:
        _send(self.edit, _EM_SETSEL, 0, -1)

    def selection(self) -> tuple[int, int]:
        # with both pointers NULL, EM_GETSEL returns the selection packed into the
        # result: start in the low word, end in the high word
        packed = _send(self.edit, _EM_GETSEL)
        return packed & 0xFFFF, (packed >> 16) & 0xFFFF

    def listbox_top(self) -> int:
        return _send(self.listbox, _LB_GETTOPINDEX)

    def listbox_set_top(self, index: int) -> None:
        _send(self.listbox, _LB_SETTOPINDEX, index)

    def listbox_count(self) -> int:
        return _send(self.listbox, _LB_GETCOUNT)

    # -------------------------------------------------------------------- control
    def focus_edit(self) -> None:
        _user32.PostMessageW(self.hwnd, _WM_APP_FOCUS_EDIT, 0, None)

    def focus_listbox(self) -> None:
        _user32.PostMessageW(self.hwnd, _WM_APP_FOCUS_LIST, 0, None)

    def focus_self(self) -> None:
        """Focus the frame itself, so it - and not a child - receives the wheel."""
        _user32.PostMessageW(self.hwnd, _WM_APP_FOCUS_SELF, 0, None)

    def wheel_deltas(self) -> list[int]:
        """Raw wheel deltas the frame has seen, in order (120 == one notch up)."""
        return list(self._wheel)

    def focus(self) -> bool:
        """Bring the window forward and put the caret in the edit box."""
        self.focus_edit()
        ok = foreground(self.hwnd)
        time.sleep(0.2)
        return ok

    def close(self) -> None:
        if self.hwnd:
            _user32.PostMessageW(self.hwnd, _WM_CLOSE, 0, None)
        self._thread.join(timeout=5)

    def __enter__(self) -> TestWindow:
        return self

    def __exit__(self, *exc: object) -> bool:
        self.close()
        return False

    # ------------------------------------------------------------------ internals
    def _run(self, title: str, width: int, height: int, x: int, y: int) -> None:
        try:
            self._create(title, width, height, x, y)
        except BaseException as exc:  # noqa: BLE001 - handed back to the constructor
            self._error = exc
            self._ready.set()
            return
        self._ready.set()
        self._pump()

    def _create(self, title: str, width: int, height: int, x: int, y: int) -> None:
        instance = _kernel32.GetModuleHandleW(None)
        callback = _WNDPROC(self._wnd_proc)
        _LIVE_CALLBACKS.append(callback)
        self._keep_alive = callback
        # a fresh class per window, so a stale registration can never be reused
        class_name = f"screen_agent_input_target_{next(_CLASS_SEQ)}"
        cls = _WNDCLASSW()
        cls.lpfnWndProc = callback
        cls.hInstance = instance
        cls.lpszClassName = class_name
        if not _user32.RegisterClassW(ctypes.byref(cls)) and ctypes.get_last_error() != 1410:
            raise OSError(ctypes.get_last_error(), "RegisterClassW failed")
        self.hwnd = _user32.CreateWindowExW(
            _WS_EX_TOPMOST, class_name, title, _WS_OVERLAPPEDWINDOW,
            x, y, width, height, None, None, instance, None,
        )
        if not self.hwnd:
            raise OSError(ctypes.get_last_error(), "CreateWindowExW failed")
        style = _WS_CHILD | _WS_VISIBLE | _WS_BORDER
        self.edit = _user32.CreateWindowExW(
            0, "EDIT", "", style | _ES_MULTILINE | _ES_AUTOHSCROLL | _ES_WANTRETURN,
            10, 10, width - 20, 60, self.hwnd, None, instance, None,
        )
        # LBS_DISPLAYHEIGHT so LB_GETTOPINDEX reflects what is on screen
        self.listbox = _user32.CreateWindowExW(
            0, "LISTBOX", "", style | _LBS_DISPLAYHEIGHT, 10, 80, width - 20,
            height - 110, self.hwnd, None, instance, None,
        )
        if not self.edit or not self.listbox:
            raise OSError(ctypes.get_last_error(), "could not create the child controls")
        for index in range(200):
            _send(self.listbox, _LB_ADDSTRING, 0, f"item {index:03d}")
        _user32.ShowWindow(self.hwnd, _SW_SHOW)
        _user32.UpdateWindow(self.hwnd)

    def _wnd_proc(self, hwnd, message, wparam, lparam) -> int:  # noqa: ANN001
        try:
            if message == _WM_APP_FOCUS_EDIT:
                _user32.SetFocus(self.edit)
            elif message == _WM_APP_FOCUS_LIST:
                _user32.SetFocus(self.listbox)
            elif message == _WM_APP_FOCUS_SELF:
                _user32.SetFocus(hwnd)
            elif message in (_WM_MOUSEWHEEL, _WM_MOUSEHWHEEL):
                # Windows routes the wheel to the focus window of the thread that owns
                # the window under the pointer, so record it here and hand it to the
                # list box the way a real application would
                self._wheel.append(ctypes.c_short((wparam >> 16) & 0xFFFF).value)
                _user32.SendMessageW(self.listbox, message, wparam, lparam)
                return 0
            elif message == _WM_CLOSE:
                _user32.DestroyWindow(hwnd)
            elif message == _WM_DESTROY:
                _user32.PostQuitMessage(0)
        except OSError:  # pragma: no cover - never let a callback raise into the loop
            return 0
        return _user32.DefWindowProcW(hwnd, message, wparam, lparam)

    def _pump(self) -> None:
        message = _MSG()
        while _user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            _user32.TranslateMessage(ctypes.byref(message))
            _user32.DispatchMessageW(ctypes.byref(message))
