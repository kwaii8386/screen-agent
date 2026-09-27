"""Tests for the pure logic: regions, keys, images, backend detection, scroll sign."""

from __future__ import annotations

import os
import sys
import zlib
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
    scale_image,
    scroll_delta,
)
from screen_agent.imaging import (  # noqa: E402
    bgra_to_rgb,
    decode_png,
    downscale_rgb,
    encode_apng,
    encode_png,
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
    """A monitor left of the primary one makes the desktop origin negative."""
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


def test_plus_and_space_are_pressable():
    """'+' and ' ' are keys, not parser noise: key_press('ctrl++') has to work."""
    assert parse_combo("+").key == "plus"
    assert parse_combo("ctrl++") == Combo(("ctrl",), "plus")
    assert parse_combo("ctrl+plus") == Combo(("ctrl",), "plus")
    assert parse_combo(" ") == Combo((), "space")
    assert parse_combo("tab").key == "tab"


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
    with pytest.raises(ValueError):
        parse_combo("ctrl+")


def test_whitespace_separated_combo():
    assert parse_combo("ctrl c").modifiers == ("ctrl",)
    assert parse_combo("ctrl c").key == "c"


# ----------------------------------------------------------------------- scroll
def test_scroll_delta_shares_one_convention():
    """Every backend goes through this, so 'down' means the same thing everywhere."""
    assert scroll_delta(3, "up") == 3
    assert scroll_delta(3, "down") == -3
    assert scroll_delta(2, "left", horizontal=True) == 2
    assert scroll_delta(2, "right", horizontal=True) == -2
    assert scroll_delta(-4, "up") == 4  # the sign comes from the direction
    with pytest.raises(ScreenAgentError):
        scroll_delta(1, "sideways")


# --------------------------------------------------------------------- imaging
def _gradient(width: int, height: int) -> bytes:
    return bytes(
        channel
        for y in range(height)
        for x in range(width)
        for channel in ((x * 7) % 256, (y * 11) % 256, (x * y) % 256)
    )


def test_png_roundtrip_is_lossless():
    rgb = _gradient(53, 31)
    blob = encode_png(53, 31, rgb)
    assert blob.startswith(b"\x89PNG\r\n\x1a\n")
    assert decode_png(blob) == (53, 31, rgb)


def test_png_needs_enough_pixels():
    with pytest.raises(ValueError):
        encode_png(4, 4, b"\x00" * 10)


def test_decoded_png_matches_pillow():
    """Cross-check the decoder (and its scanline filters) against a real encoder."""
    pillow = pytest.importorskip("PIL.Image")
    import io

    reference = pillow.frombytes("RGB", (97, 53), _gradient(97, 53))
    buffer = io.BytesIO()
    reference.save(buffer, format="PNG")
    width, height, decoded = decode_png(buffer.getvalue())
    assert (width, height) == (97, 53)
    assert decoded == reference.tobytes()


def test_downscale_matches_pillow_nearest():
    pillow = pytest.importorskip("PIL.Image")

    rgb = _gradient(97, 53)
    reference = pillow.frombytes("RGB", (97, 53), rgb)
    for max_width in (96, 40, 1, 400):
        small, width, height = downscale_rgb(rgb, 97, 53, max_width)
        expected = reference.resize((width, height), pillow.NEAREST)
        assert (width, height) == expected.size
        assert small == expected.tobytes()


def test_downscale_never_enlarges():
    rgb = b"\x10\x20\x30" * 16
    assert downscale_rgb(rgb, 4, 4, 800) == (rgb, 4, 4)


def test_bgra_to_rgb_drops_alpha():
    assert bgra_to_rgb(bytes([1, 2, 3, 255, 4, 5, 6, 7])) == bytes([1, 2, 3, 4, 5, 6])
    assert bgra_to_rgb(b"") == b""


def test_apng_is_a_real_animation():
    pillow = pytest.importorskip("PIL.Image")
    import io

    base = _gradient(16, 16)
    frames = [(16, 16, bytes((value + step) % 256 for value in base)) for step in range(4)]
    blob = encode_apng(frames, fps=10)
    assert b"acTL" in blob and blob.count(b"fcTL") == 4
    image = pillow.open(io.BytesIO(blob))
    assert image.n_frames == 4
    assert image.is_animated
    assert image.size == (16, 16)
    for step in range(4):
        image.seek(step)
        assert image.convert("RGB").tobytes() == frames[step][2]


def test_apng_rejects_mismatched_frames():
    with pytest.raises(ValueError):
        encode_apng([(4, 4, b"\x00" * 48), (8, 8, b"\x00" * 192)])
    with pytest.raises(ValueError):
        encode_apng([])


def test_apng_delay_matches_the_frame_rate():
    import struct

    blob = encode_apng([(2, 2, b"\x00" * 12)] * 3, fps=5)
    # a chunk is length(4) + tag(4) + data + crc(4), so the data starts right after the tag
    frames, plays = struct.unpack(">II", blob.split(b"acTL", 1)[1][:8])
    delay_num, delay_den = struct.unpack(">HH", blob.split(b"fcTL", 1)[1][20:24])
    assert (frames, plays) == (3, 0)  # 0 = loop forever
    assert (delay_num, delay_den) == (1, 5)  # one frame every 1/5 s


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


def test_scale_image_without_pillow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The Pillow-free path has to shrink a PNG too, or `screenshot(scale=)` lies."""
    target = tmp_path / "big.png"
    target.write_bytes(encode_png(80, 40, _gradient(80, 40)))

    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __import__

    def no_pillow(name, *args, **kwargs):
        if name.startswith("PIL"):
            raise ImportError("no pillow in this test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", no_pillow)
    assert scale_image(target, 40) == (40, 20)
    width, height = decode_png(target.read_bytes())[:2]
    assert (width, height) == (40, 20)


def test_scale_image_leaves_small_images_alone(tmp_path: Path):
    target = tmp_path / "small.png"
    target.write_bytes(encode_png(10, 10, b"\x00" * 300))
    assert scale_image(target, 100) == (10, 10)
    assert scale_image(target, 0) == (10, 10)


def test_png_data_is_really_compressed():
    """A solid frame must be far smaller raw than zlib-compressed."""
    blob = encode_png(64, 64, b"\x7f" * (64 * 64 * 3), level=6)
    assert len(blob) < 64 * 64 * 3 // 10
    assert zlib.decompress  # the module is the only compressor we rely on


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


def test_backend_can_be_forced_by_environment():
    os.environ["SCREEN_AGENT_BACKEND"] = "windows"
    try:
        # the import is platform independent, so this works on every machine
        assert load_backend().name == "windows"
    finally:
        del os.environ["SCREEN_AGENT_BACKEND"]


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


def test_run_capture_reports_a_missing_recorder():
    import asyncio  # noqa: PLC0415

    from screen_agent.core import run_capture  # noqa: PLC0415

    async def go():
        with pytest.raises(ScreenAgentError):
            await run_capture(["definitely-not-a-real-recorder-xyz"], 0.1)

    asyncio.run(go())


def test_default_shot_dir_respects_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SCREEN_AGENT_DIR", "/tmp/sa-test")
    assert default_shot_dir() == Path("/tmp/sa-test")
