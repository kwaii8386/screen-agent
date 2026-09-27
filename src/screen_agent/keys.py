"""Cross-platform key names: ``ctrl+shift+t`` -> (modifiers, key) and per-platform lookups."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

__all__ = ["Combo", "MODIFIERS", "NAMED_KEYS", "parse_combo", "canonical_modifier", "char_key"]

#: canonical modifier names, in the order they are pressed / released around a key
MODIFIERS = ("ctrl", "alt", "shift", "super", "meta", "fn", "hyper")

_MODIFIER_ALIASES = {
    "control": "ctrl",
    "ctl": "ctrl",
    "control_l": "ctrl",
    "control_r": "ctrl",
    "option": "alt",
    "alt_l": "alt",
    "alt_r": "alt",
    "shift_l": "shift",
    "shift_r": "shift",
    "cmd": "super",
    "command": "super",
    "win": "super",
    "windows": "super",
    "super_l": "super",
    "super_r": "super",
    "gui": "meta",
    "logo": "meta",
    "altgr": "alt",
}

#: non-character keys, normalised to lower case
NAMED_KEYS = {
    "return": "return",
    "enter": "return",
    "kpenter": "return",
    "tab": "tab",
    "escape": "escape",
    "esc": "escape",
    "space": "space",
    "spacebar": "space",
    "backspace": "backspace",
    "bksp": "backspace",
    "delete": "delete",
    "del": "delete",
    "insert": "insert",
    "ins": "insert",
    "home": "home",
    "end": "end",
    "pageup": "pageup",
    "pgup": "pageup",
    "pagedown": "pagedown",
    "pgdn": "pagedown",
    "up": "up",
    "down": "down",
    "left": "left",
    "right": "right",
    "capslock": "capslock",
    "numlock": "numlock",
    "scrolllock": "scrolllock",
    "printscreen": "printscreen",
    "prtsc": "printscreen",
    "pause": "pause",
    "menu": "menu",
    "contextmenu": "menu",
    "minus": "minus",
    "dash": "minus",
    "equal": "equal",
    "equals": "equal",
    "plus": "plus",
    "comma": "comma",
    "period": "period",
    "dot": "period",
    "slash": "slash",
    "backslash": "backslash",
    "semicolon": "semicolon",
    "apostrophe": "apostrophe",
    "quote": "apostrophe",
    "grave": "grave",
    "backtick": "grave",
    "lbracket": "lbracket",
    "rbracket": "rbracket",
}
NAMED_KEYS.update({f"f{i}": f"f{i}" for i in range(1, 25)})
# keypad digits: 'kp0' .. 'kp9'
NAMED_KEYS.update({f"kp{i}": f"kp{i}" for i in range(10)})
# Modifier keys are also pressable on their own - Super alone opens the Activities
# overview on GNOME, which an agent needs for launching apps.
NAMED_KEYS.update(
    {
        "super": "super", "meta": "meta", "win": "super", "windows": "super",
        "command": "super", "cmd": "super", "gui": "meta", "logo": "meta",
        "ctrl": "ctrl", "control": "ctrl", "alt": "alt", "option": "alt",
        "shift": "shift", "fn": "fn", "hyper": "hyper",
    }
)


def canonical_modifier(name: str) -> str:
    key = name.strip().lower()
    key = _MODIFIER_ALIASES.get(key, key)
    if key not in MODIFIERS:
        raise KeyError(f"unknown modifier {name!r}")
    return key


def char_key(name: str) -> str:
    """Return a single character for *name* if it denotes a printable character."""
    key = name.strip()
    if len(key) == 1:
        return key
    low = key.lower()
    if low in NAMED_KEYS:
        return ""  # a named key, not a character
    if low.startswith("kp") and low[2:].isdigit():
        return low[2]
    raise KeyError(f"unknown key {name!r}")


@dataclass(frozen=True)
class Combo:
    """A key press: zero or more modifiers plus exactly one main key."""

    modifiers: tuple[str, ...] = ()
    key: str = ""
    raw: str = field(default="", compare=False)

    def __str__(self) -> str:
        return "+".join([*self.modifiers, self.key])

    @property
    def is_named(self) -> bool:
        return self.key in NAMED_KEYS.values()

    @property
    def character(self) -> str:
        """The printable character, or '' for named keys."""
        return "" if self.is_named else self.key


_SPLIT = re.compile(r"\s*[+]\s*|\s+")


def parse_combo(text: str) -> Combo:
    """Parse ``'ctrl+shift+t'`` / ``'alt+F4'`` / ``'Return'`` / ``'a'`` into a :class:`Combo`."""
    parts = [p for p in _SPLIT.split(text.strip()) if p]
    if not parts:
        raise ValueError("no key given")
    *mods, main = parts
    modifiers: list[str] = []
    for m in mods:
        mod = canonical_modifier(m)
        if mod not in modifiers:
            modifiers.append(mod)
    key = main.strip()
    low = key.lower()
    if low in NAMED_KEYS:
        key = NAMED_KEYS[low]
    elif len(key) != 1 and not (low.startswith("kp") and low[2:].isdigit()):
        # allow 'kp5' and single characters only
        raise ValueError(
            f"unknown key {main!r}; use a single character, a named key "
            f"(Return, Tab, Escape, F5, ...) or a modifier combo like ctrl+c"
        )
    return Combo(tuple(modifiers), key, raw=text.strip())
