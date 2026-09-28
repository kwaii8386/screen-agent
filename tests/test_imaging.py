"""Tests for the dependency-free imaging helpers, focused on the hot paths.

The headline test is structural rather than temporal: an unbounded slice of the
source buffer (``src[base:][:stride]``) is a silent 250 ms, and a timing assertion
would only catch it on a machine slow enough. Recording the reads catches the whole
class of bug on any machine, in well under a second.
"""

from __future__ import annotations

import io
import struct
import sys
import zlib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from screen_agent.imaging import (  # noqa: E402
    PNG_SIGNATURE,
    bgra_to_rgb,
    decode_png,
    downscale_rgb,
    encode_apng,
    encode_png,
)


class _RecordingBytes(bytes):
    """A ``bytes`` that remembers the extent of every slice taken of it."""

    def __new__(cls, data: bytes) -> _RecordingBytes:
        self = super().__new__(cls, data)
        self.reads: list[tuple[int, int]] = []
        return self

    def __getitem__(self, key):
        value = super().__getitem__(key)
        if isinstance(key, slice):
            self.reads.append((key.start or 0, len(self) if key.stop is None else key.stop))
        return value

    @property
    def copied(self) -> int:
        """Total number of source bytes any single read pulled into a new object."""
        return sum(stop - start for start, stop in self.reads)


def _gradient(width: int, height: int) -> bytes:
    return bytes(
        channel
        for y in range(height)
        for x in range(width)
        for channel in ((x * 7) % 256, (y * 11) % 256, (x * y) % 256)
    )


# ------------------------------------------------------------------- downscale
def test_downscale_row_slice_is_bounded():
    """A row read must cost one row, not the rest of the buffer.

    The bug this guards: ``src[base:][:stride]`` copies the entire remaining tail of
    the source on every output row and then throws all but one row away. Counted with
    the same recording trick, that is 1,122,854,400 B (1.12 GB) for 1920x1080 -> 640,
    against 360 * 5760 = 2,073,600 B for the bounded form - 542x the source image, to
    move 2 MB. (An earlier draft of this comment said 1.9 GB; the count is the number
    above, and ``bench/bench_latency.py`` reproduces it.)
    """
    width, height, max_width = 1920, 1080, 640
    src = _RecordingBytes(_gradient(width, height))
    assert len(src) == width * height * 3

    small, new_w, new_h = downscale_rgb(src, width, height, max_width)
    assert (new_w, new_h) == (640, 360)
    assert small

    stride = width * 3
    assert src.reads, "the source buffer was never sliced, so nothing was measured"
    # no read reaches past the end of the buffer (an unbounded tail slice does)
    assert max(stop for _, stop in src.reads) <= len(src)
    # and no read is longer than a single source row
    assert max(stop - start for start, stop in src.reads) <= stride
    # the whole run costs about one row per output row, not the 1.12 GB the bug took
    assert src.copied <= new_h * stride
    assert src.copied < len(src), "the old bug copied the buffer 300+ times over"


def test_downscale_matches_pillow_nearest():
    """The pixel-centre index convention has to agree with Pillow, exactly."""
    pillow = pytest.importorskip("PIL.Image")

    width, height = 1920, 1080
    rgb = _gradient(width, height)
    reference = pillow.frombytes("RGB", (width, height), rgb)
    small, new_w, new_h = downscale_rgb(rgb, width, height, 640)
    expected = reference.resize((new_w, new_h), pillow.NEAREST)
    assert (new_w, new_h) == expected.size == (640, 360)
    assert small == expected.tobytes()


def test_downscale_never_enlarges():
    rgb = b"\x10\x20\x30" * 16
    assert downscale_rgb(rgb, 4, 4, 800) == (rgb, 4, 4)


def test_downscale_rejects_a_bad_source_size():
    with pytest.raises(ValueError):
        downscale_rgb(b"", 0, 4, 100)


# ----------------------------------------------------------------- bgra -> rgb
def _bgra_buffer(width: int, height: int) -> bytes:
    return bytes(
        channel
        for y in range(height)
        for x in range(width)
        for channel in ((x * 5) % 256, (y * 3) % 256, (x + y) % 256, (x * y + 7) % 256)
    )


def test_bgra_to_rgb_matches_pillow():
    """Cross-check the channel shuffle against a real BGRA decoder."""
    pillow = pytest.importorskip("PIL.Image")

    width, height = 64, 48
    bgra = _bgra_buffer(width, height)
    # Pillow's "BGRA" raw reader wants RGBA byte order, so hand it the buffer with
    # R and B swapped: what comes back out is the same channel order we produce.
    rgba = bytearray()
    for i in range(0, len(bgra), 4):
        rgba += bytes((bgra[i + 2], bgra[i + 1], bgra[i], bgra[i + 3]))
    reference = pillow.frombytes("RGBA", (width, height), bytes(rgba), "raw", "BGRA", 0, 1)
    assert bgra_to_rgb(bgra) == reference.convert("RGB").tobytes()


def test_bgra_to_rgb_is_unchanged_by_the_optimisation():
    """Exact bytes for a known buffer: the de-interleave must not drift.

    Note the slow, obviously-correct triple-slice reference below is kept in the
    test on purpose, so the fast path is pinned against something readable.
    """
    bgra = _bgra_buffer(37, 11)
    expected = bytearray(len(bgra) // 4 * 3)
    expected[0::3] = bgra[0::4]
    expected[1::3] = bgra[1::4]
    expected[2::3] = bgra[2::4]
    assert bgra_to_rgb(bgra) == bytes(expected)

    # hand-checked literals, including alpha values that are not 255
    assert bgra_to_rgb(bytes([1, 2, 3, 255, 4, 5, 6, 7])) == bytes([1, 2, 3, 4, 5, 6])
    assert bgra_to_rgb(b"") == b""
    assert bgra_to_rgb(bytearray(b"\x01\x02\x03\x00")) == bytes([1, 2, 3])


# ------------------------------------------------------------------------- png
def test_png_roundtrip_is_lossless():
    rgb = _gradient(53, 31)
    blob = encode_png(53, 31, rgb)
    assert blob.startswith(PNG_SIGNATURE)
    assert decode_png(blob) == (53, 31, rgb)


def test_apng_is_a_real_animation():
    pillow = pytest.importorskip("PIL.Image")

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


def test_decoder_survives_every_scanline_filter():
    """Filters 1-4 are pure-Python loops; a wrong one is a silent pixel shift."""
    width, height = 97, 53
    rgb = _gradient(width, height)
    stride = width * 3
    for ftype in range(5):
        raw = bytearray()
        prior = bytearray(stride)
        for y in range(height):
            row = rgb[y * stride : (y + 1) * stride]
            raw.append(ftype)
            for x in range(stride):
                left = row[x - 3] if x >= 3 else 0
                up = prior[x]
                upleft = prior[x - 3] if x >= 3 else 0
                if ftype == 0:
                    pred = 0
                elif ftype == 1:
                    pred = left
                elif ftype == 2:
                    pred = up
                elif ftype == 3:
                    pred = (left + up) >> 1
                else:
                    p = left + up - upleft
                    pa, pb, pc = abs(p - left), abs(p - up), abs(p - upleft)
                    pred = left if pa <= pb and pa <= pc else (up if pb <= pc else upleft)
                raw.append((row[x] - pred) & 0xFF)
            prior = bytearray(row)

        blob = (
            PNG_SIGNATURE
            + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + _chunk(b"IDAT", zlib.compress(bytes(raw), 6))
            + _chunk(b"IEND", b"")
        )
        assert decode_png(blob) == (width, height, rgb), f"filter type {ftype}"


def _chunk(tag: bytes, data: bytes) -> bytes:
    return (
        len(data).to_bytes(4, "big") + tag + data + zlib.crc32(tag + data).to_bytes(4, "big")
    )
