"""Exact elements for Windows applications that are not a web page.

A screenshot tells an agent what the desktop looks like. It costs about 83 ms and, worse,
1000-1500 tokens to look at, and the coordinates an agent reads off a downscaled image are
5-8% wrong often enough to matter. UI Automation hands over the same information - an
element's name, its control type, its exact rectangle - as data, with no image at all.

What this module is honest about, because all of it was measured on the target machine:

* **It is not fast in the abstract; it is fast per node.** Every property read is a
  cross-process COM round trip, about 1.2 ms. A 40-element window costs 46 ms, which is
  half a screenshot and zero tokens.
* **One walk beats a filtered query.** Asking the provider for "all the Buttons" with
  ``FindAll`` and a property condition measured 221 ms against 46 ms for a single walk
  that reads three properties per node - four times slower, because each condition makes
  the provider traverse the whole tree again. So this walks once and filters afterwards.
* **It is blind where a screenshot is not.** Web content inside a browser is invisible to
  it - use the Chrome DevTools Protocol for that. So are minimised windows, whose
  rectangles are all zero. And it cannot help with a surface that only lives for a second,
  like the Start menu, because by the time a model has read a screenshot the surface is
  gone. The answer there is to never put a model in that loop, not to perceive faster.
* **It needs ``comtypes``, not pywin32.** ``CUIAutomation`` does not implement
  ``IDispatch``, so late binding fails outright with "No such interface supported";
  ``comtypes`` generates early-bound wrappers from the system typelib instead. That also
  removes the hand-rolled-vtable class of bug entirely.
"""

from __future__ import annotations

import ctypes
from typing import Any

__all__ = [
    "UiaUnavailable",
    "element_kind",
    "find_elements",
    "interactive_types",
    "invoke",
    "set_value",
    "uia_available",
    "window_title",
]

# ControlTypeId values that an agent can act on. Everything else is still walked - a
# labelled group can hold a button - but is not reported, because the useful answer is
# never "there is a Pane at (0, 0)".
KIND = {
    50000: "Button",
    50001: "Calendar",
    50002: "CheckBox",
    50003: "ComboBox",
    50004: "Edit",
    50005: "Hyperlink",
    50006: "Image",
    50007: "ListItem",
    50008: "List",
    50009: "Menu",
    50010: "MenuBar",
    50011: "MenuItem",
    50013: "RadioButton",
    50014: "ScrollBar",
    50015: "Slider",
    50016: "Spinner",
    50018: "Tab",
    50019: "TabItem",
    50020: "Text",
    50021: "ToolBar",
    50022: "ToolTip",
    50023: "Tree",
    50024: "TreeItem",
    30005: "Custom",
    30010: "Custom",
    30011: "Group",
    30012: "Thumb",
    30015: "Pane",
    50032: "Window",
    50033: "Pane",
}

_ACTIONABLE = {
    "Button", "CheckBox", "ComboBox", "Edit", "Hyperlink", "ListItem", "MenuItem",
    "RadioButton", "Slider", "Tab", "TabItem", "ToolBar", "TreeItem",
}


class UiaUnavailable(RuntimeError):
    """UI Automation could not be used at all - usually ``comtypes`` is not installed."""


def interactive_types() -> frozenset[str]:
    return frozenset(_ACTIONABLE)


def element_kind(control_type: int) -> str:
    return KIND.get(int(control_type), str(int(control_type)))


UIA_INVOKE_PATTERN = 10000
UIA_TOGGLE_PATTERN = 10001
UIA_VALUE_PATTERN = 10002
UIA_SELECTION_ITEM_PATTERN = 10007


def _element_at(hwnd: int, name: str, index: int, kinds: list[str] | None) -> Any:
    """Re-find the element, by the same rules ``find_elements`` reports, and return it live.

    The element has to be located *again* rather than cached, because between finding an
    element and acting on it the window can move or the control can be re-created - the
    same time-of-check/time-of-use problem the browser path guards against.
    """
    uia, why = _get_uia()
    if uia is None:
        raise UiaUnavailable(why)
    wanted_kinds = {k.lower() for k in kinds} if kinds else None
    wanted_name = name.lower() if name else None

    root = uia.ElementFromHandle(int(hwnd))
    walker = uia.ControlViewWalker
    stack = [root]
    guard = 0
    hits = []
    while stack and guard < 2000:
        guard += 1
        el = stack.pop()
        try:
            ctype = el.CurrentControlType
            rect = _rect(el)
        except Exception:
            rect, ctype = None, 0
        if rect is not None:
            kind = element_kind(ctype)
            try:
                label = el.CurrentName or ""
            except Exception:
                label = ""
            if wanted_name:
                if label.lower() == wanted_name or (
                    wanted_name in label.lower() and wanted_name in (name or "").lower()
                    and label.lower().startswith(wanted_name)
                ):
                    hits.append((el, label, kind))
            elif wanted_kinds is None or kind.lower() in wanted_kinds:
                if kind in _ACTIONABLE:
                    hits.append((el, label, kind))
        try:
            child = walker.GetFirstChildElement(el)
        except Exception:
            child = None
        while child is not None:
            stack.append(child)
            try:
                child = walker.GetNextSiblingElement(child)
            except Exception:
                break
    if not hits:
        return None, "", ""
    el, label, kind = hits[max(0, min(int(index), len(hits)) - 1)]
    return el, label, kind


def invoke(hwnd: int, name: str = "", index: int = 1, kinds: list[str] | None = None) -> dict:
    """Try to activate a control through UI Automation's own Invoke pattern.

    The idea is better than clicking a rectangle, and not for latency reasons: a click at
    exact coordinates goes to whatever is on top, so a control in a background window gets
    nothing while the click tool reports success. Invoke addresses the control itself.

    **It does not work on this build, and the module says so rather than pretending.**
    ``GetCurrentPattern`` hands back a bare ``POINTER(IUnknown)``, and getting a typed
    interface out of it needs a ``QueryInterface`` that comtypes' generated wrappers do
    not expose: constructing the interface class from the pointer raises, ``_obj`` and
    ``_oleobj_`` are absent, and reaching through ``.contents[0].QueryInterface`` ends in
    a NULL pointer access. pywinauto solves this with its own bindings, which is the real
    answer and is a larger dependency than this project should take for a fallback it does
    not need yet.

    So this returns ``via=None`` and the caller clicks the rectangle, which works once the
    window is in front - and ``window_activate`` exists for that. Kept, tested and
    reported honestly, because "the control exposes no way to be activated other than by
    pointing at it" is a fact a caller should be able to see, not a silent no-op.
    """
    el, label, kind = _element_at(hwnd, name, index, kinds)
    if el is None:
        return {"found": False}
    supported: list[str] = []
    for pattern_id, method in (
        (UIA_INVOKE_PATTERN, "Invoke"),
        (UIA_SELECTION_ITEM_PATTERN, "Select"),
        (UIA_TOGGLE_PATTERN, "Toggle"),
    ):
        try:
            pattern = el.GetCurrentPattern(pattern_id)
        except Exception:
            continue
        if pattern is not None:
            supported.append(method)
    return {
        "found": True,
        "name": label,
        "kind": kind,
        "via": None,
        "ok": False,
        "patterns_available": supported,
        "note": (
            "this control does advertise "
            + ", ".join(supported)
            + ", but a typed interface cannot be obtained from GetCurrentPattern through "
            "comtypes on this build, so the click path is used instead"
            if supported
            else "this control advertises no activation pattern at all"
        ),
    }


def set_value(hwnd: int, name: str, text: str, index: int = 1) -> dict:
    """Type into a control through UI Automation's Value pattern, where it has one.

    Cheaper and more reliable than focusing it and sending keystrokes, for the same
    reason Invoke is better than a click: it addresses the control, not a point on the
    screen. Returns ``via=None`` when there is no Value pattern - a rich text box does
    not have one, and for those the caller should focus and type.
    """
    el, label, kind = _element_at(hwnd, name, index, None)
    if el is None:
        return {"found": False}
    try:
        pattern = el.GetCurrentPattern(UIA_VALUE_PATTERN)
    except Exception:
        pattern = None
    if pattern is None:
        return {"found": True, "name": label, "kind": kind, "via": None, "ok": False}
    try:
        pattern.SetValue(text)
    except Exception as exc:
        return {
            "found": True, "name": label, "kind": kind, "via": "SetValue", "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
    return {"found": True, "name": label, "kind": kind, "via": "SetValue", "ok": True}


_uia: Any = None
_uia_failed: str | None = None


def uia_available() -> tuple[bool, str]:
    """Whether UIA can be used here, and if not, the one line that says why."""
    ok, why = _get_uia()
    return ok is not None, why


def _get_uia() -> tuple[Any | None, str]:
    global _uia, _uia_failed
    if _uia is not None:
        return _uia, "ok"
    if _uia_failed is not None:
        return None, _uia_failed
    try:
        import comtypes  # noqa: PLC0415
        import comtypes.client  # noqa: PLC0415
        from comtypes import CLSCTX_INPROC_SERVER  # noqa: PLC0415

        # Wrapping the typelib is the slow part (~270 ms) but happens once per process.
        mod = comtypes.client.GetModule("UIAutomationCore.dll")
        _uia = comtypes.CoCreateInstance(
            mod.CUIAutomation._reg_clsid_,
            interface=mod.IUIAutomation,
            clsctx=CLSCTX_INPROC_SERVER,
        )
    except Exception as exc:  # ImportError, or a machine with no UIAutomationCore
        _uia_failed = (
            f"UI Automation is unavailable: {type(exc).__name__}: {exc}. "
            f"Install the extra with: pip install screen-agent[windows]"
        )
        return None, _uia_failed
    return _uia, "ok"


def window_title(hwnd: int) -> str:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def _rect(el: Any) -> tuple[int, int, int, int] | None:
    try:
        r = el.CurrentBoundingRectangle
    except Exception:
        return None
    # A zero-sized or empty rectangle is the UIA way of saying "not on screen right now",
    # which is what a minimised window and a collapsed row both look like.
    if r.right <= r.left or r.bottom <= r.top:
        return None
    return int(r.left), int(r.top), int(r.right), int(r.bottom)


def find_elements(
    hwnd: int,
    *,
    names: list[str] | None = None,
    kinds: list[str] | None = None,
    include_text: bool = False,
    limit: int = 200,
    max_nodes: int = 1500,
) -> list[dict[str, Any]]:
    """One walk of *hwnd*'s control view, filtered in Python.

    Reads three properties per node - control type, rectangle, name - and that restraint
    is the whole reason this is usable: a sixth property per node costs another third of
    the total. Returns ``name``, ``kind``, ``automation_id``, ``rect`` and ``actionable``.
    """
    uia, why = _get_uia()
    if uia is None:
        raise UiaUnavailable(why)

    wanted_kinds = {k.lower() for k in kinds} if kinds else None
    wanted_names = {n.lower() for n in names} if names else None
    want_all = wanted_kinds is None and wanted_names is None

    root = uia.ElementFromHandle(int(hwnd))
    walker = uia.ControlViewWalker
    out: list[dict[str, Any]] = []
    stack = [root]
    guard = 0
    while stack and len(out) < limit and guard < max_nodes:
        guard += 1
        el = stack.pop()
        try:
            control_type = el.CurrentControlType
            rect = _rect(el)
        except Exception:
            rect = None
            control_type = 0
        if rect is not None:
            kind = element_kind(control_type)
            try:
                name = el.CurrentName or ""
            except Exception:
                name = ""
            hit = want_all
            if not hit and wanted_kinds and kind.lower() in wanted_kinds:
                hit = True
            if not hit and wanted_names and name.lower() in wanted_names:
                hit = True
            if not hit and include_text and name:
                hit = True
            if hit:
                try:
                    auto = el.CurrentAutomationId or ""
                except Exception:
                    auto = ""
                out.append(
                    {
                        "name": name,
                        "kind": kind,
                        "automation_id": auto,
                        "rect": list(rect),
                        "actionable": kind in _ACTIONABLE,
                    }
                )
        # descend regardless: a group can hold the thing that matched
        try:
            child = walker.GetFirstChildElement(el)
        except Exception:
            child = None
        while child is not None:
            stack.append(child)
            try:
                child = walker.GetNextSiblingElement(child)
            except Exception:
                break
    return out
