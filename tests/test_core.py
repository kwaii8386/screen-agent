"""Tests for the pure logic: region parsing, key parsing, backend selection."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from screen_agent.backends import detect_backends, load_backend  # noqa: E402
from screen_agent.core import (  # noqa: E402
    Rect,
    ScreenAgentError,
    _png_size,
    default_shot_dir,
    parse_region,
    run,
)
from screen_agent.keys import Combo, canonical_modifier, parse_combo  # noqa: E402

DESKTOP = Rect(0, 0, 1920, 1080)


# --------------------------------------------------------------------- regions
def test_rect_unpacks_like_a_tuple():
    """Backends do `bx, by, bw, bh = self.desktop`, so Rect must be iterable."""
    x, y, w, h = Rect(0, 0, 1920, 1080)
    assert (x, y, w, h) == (0, 0, 1920, 1080)
    assert tuple(Rect(5, 6, 7, 8)) == (5, 6, 7, 8)


def test_full_region_is_the_whole_desktop():
    assert parse_region("full", DESKTOP) == DESKTOP
    assert parse_region("", DESKTOP) == DESKTOP
    assert parse_region("ALL", DESKTOP) == DESKTOP
    assert parse_region(None, DESKTOP) == DESKTOP


def test_explicit_region_is_parsed():
    assert parse_region("10,20,300,400", DESKTOP) == Rect(10, 20, 300, 400)
    assert parse_region("10 20 300 400", DESKTOP) == Rect(10, 20, 300, 400)


def test_region_is_clamped_to_the_desktop():
    # origin outside the top-left: the origin is pulled in, the size is kept
    assert parse_region("-50,-50,200,200", DESKTOP) == Rect(0, 0, 200, 200)
    # size running past the right/bottom edge is trimmed
    assert parse_region("1800,1000,500,500", DESKTOP) == Rect(1800, 1000, 120, 80)


def test_negative_origin_desktop_is_supported():
    multi = Rect(-1920, 0, 3840, 1080)
    assert parse_region("full", multi) == multi
    assert parse_region("0,0,100,100", multi) == Rect(0, 0, 100, 100)
    assert parse_region("-1900,10,50,50", multi) == Rect(-1900, 10, 50, 50)


def test_bad_region_is_rejected():
    for bad in ("1,2,3", "a,b,c,d", "1,2,0,4", "1,2,4,-3", ""):
        if bad == "":
            continue
        with pytest.raises(ScreenAgentError):
            parse_region(bad, DESKTOP)


# ------------------------------------------------------------------------ keys
def test_parse_simple_combo():
    combo = parse_combo("a")
    assert combo == Combo((), "a")
    assert not combo.is_named
    assert combo.character == "a"


def test_parse_modifier_combo():
    combo = parse_combo("ctrl+shift+t")
    assert combo.modifiers == ("ctrl", "shift")
    assert combo.key == "t"
    assert str(combo) == "ctrl+shift+t"


def test_named_keys_and_aliases():
    assert parse_combo("Return").key == "return"
    assert parse_combo("enter").key == "return"
    assert parse_combo("esc").key == "escape"
    assert parse_combo("F5").key == "f5"
    assert parse_combo("pgup").key == "pageup"
    assert parse_combo("Return").is_named


def test_modifier_aliases():
    assert canonical_modifier("control") == "ctrl"
    assert canonical_modifier("Command") == "super"
    assert canonical_modifier("option") == "alt"
    assert parse_combo("cmd+c").modifiers == ("super",)
    with pytest.raises(KeyError):
        canonical_modifier("nope")


def test_duplicate_modifiers_collapse():
    assert parse_combo("ctrl+ctrl+a").modifiers == ("ctrl",)


def test_modifier_keys_are_pressable_on_their_own():
    """key_press("super") opens the Activities overview, so it must parse as a key."""
    for name, expected in [("super", "super"), ("win", "super"), ("cmd", "super"),
                           ("ctrl", "ctrl"), ("shift", "shift"), ("alt", "alt")]:
        combo = parse_combo(name)
        assert combo.is_named, name
        assert combo.key == expected
        assert combo.modifiers == ()


def test_mutter_keysym_tables_cover_every_parsed_key():
    """Every key the parser accepts must resolve to a keysym on the mutter backend."""
    from screen_agent.backends.mutter import _KEYSYMS, _MODS, _NEEDS_SHIFT  # noqa: PLC0415

    for text in ["a", "Z", "5", "/", "?", "~", "Return", "F7", "kp3", "super",
                 "ctrl", "alt", "shift", "space", "escape", "left", "pageup"]:
        combo = parse_combo(text)
        assert _KEYSYMS.get(combo.key) or _MODS.get(combo.key), f"no keysym for {text!r}"
    # uppercase must require shift, lowercase must not
    assert ord("A") in _NEEDS_SHIFT and ord("a") not in _NEEDS_SHIFT


def test_unknown_key_is_rejected():
    with pytest.raises(ValueError):
        parse_combo("ctrl+notakey")
    with pytest.raises(ValueError):
        parse_combo("   ")


def test_whitespace_separated_combo():
    assert parse_combo("ctrl c").modifiers == ("ctrl",)
    assert parse_combo("ctrl c").key == "c"


# --------------------------------------------------------------------- backends
def test_all_backends_import_without_a_display():
    names = {n for n, _ in detect_backends()}
    # every backend module must import cleanly even where it cannot run
    assert {"mutter", "wayland-tools", "x11", "macos", "windows"} <= names


def test_unknown_backend_name_is_reported():
    os.environ["SCREEN_AGENT_BACKEND"] = "nonexistent-backend"
    try:
        with pytest.raises(Exception) as excinfo:
            load_backend()
        assert "unknown backend" in str(excinfo.value)
    finally:
        del os.environ["SCREEN_AGENT_BACKEND"]


def test_detection_reasons_are_human_readable():
    for name, reason in detect_backends():
        assert isinstance(name, str) and name
        assert isinstance(reason, str)


# ----------------------------------------------------------------------- helpers
def test_run_reports_missing_binary():
    import asyncio  # noqa: PLC0415

    async def go():
        with pytest.raises(ScreenAgentError):
            await run("definitely-not-a-real-binary-xyz")

    asyncio.run(go())


def test_run_captures_output():
    import asyncio  # noqa: PLC0415

    async def go():
        rc, out, _ = await run(sys.executable, "-c", "print('hi')")
        assert rc == 0
        assert out.strip() == "hi"

    asyncio.run(go())


def test_png_size_reads_header(tmp_path: Path):
    pytest.importorskip("PIL")
    from PIL import Image  # noqa: PLC0415

    target = tmp_path / "x.png"
    Image.new("RGB", (64, 32), "red").save(target)
    assert _png_size(target) == (64, 32)


def test_png_size_rejects_non_png(tmp_path: Path):
    bad = tmp_path / "bad.png"
    bad.write_text("not a png")
    with pytest.raises(ScreenAgentError):
        _png_size(bad)


def test_default_shot_dir_respects_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SCREEN_AGENT_DIR", "/tmp/sa-test")
    assert default_shot_dir() == Path("/tmp/sa-test")
