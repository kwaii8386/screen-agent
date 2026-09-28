"""Dependency-free image helpers: PNG / APNG encoding, decoding and downscaling.

The Windows backend grabs raw pixels out of GDI and has to write them out itself, so
that a bare ``pip install screen-agent`` can take screenshots on a machine that has no
Pillow. Pillow stays optional and is still preferred where it is installed, because it
is faster at resizing.

Everything here works on plain ``bytes`` in 8-bit RGB, top-down, 3 bytes per pixel -
the format GDI hands back once the channel order is fixed up.
"""

from __future__ import annotations

import struct
import zlib
from operator import itemgetter

__all__ = [
    "PNG_SIGNATURE",
    "encode_png",
    "encode_apng",
    "decode_png",
    "downscale_rgb",
    "bgra_to_rgb",
]

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

_COLOR_RGB = 2


def bgra_to_rgb(bgra: bytes | bytearray) -> bytes:
    """Swap a GDI ``BGRA`` buffer into ``RGB`` using slice arithmetic (no numpy)."""
    if not bgra:
        return b""
    out = bytearray(len(bgra) // 4 * 3)
    out[0::3] = bgra[0::4]
    out[1::3] = bgra[1::4]
    out[2::3] = bgra[2::4]
    return bytes(out)


# --------------------------------------------------------------------------- encode
def _chunk(tag: bytes, data: bytes) -> bytes:
    """One PNG chunk: length, type, payload, CRC32."""
    return (
        len(data).to_bytes(4, "big")
        + tag
        + data
        + zlib.crc32(tag + data).to_bytes(4, "big")
    )


def _ihdr(width: int, height: int) -> bytes:
    return _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, _COLOR_RGB, 0, 0, 0))


def _idat(rgb: bytes | bytearray, width: int, height: int, level: int) -> bytes:
    """zlib stream of the image data: every scanline prefixed with filter type 0.

    Filter 0 (None) costs a few bytes per row but keeps the encoder branch-free; the
    whole hot loop is two C-level slice copies per row.
    """
    stride = width * 3
    raw = bytearray((stride + 1) * height)
    for y in range(height):
        off = y * (stride + 1)
        raw[off] = 0
        raw[off + 1 : off + 1 + stride] = rgb[y * stride : (y + 1) * stride]
    return zlib.compress(bytes(raw), level)


def encode_png(
    width: int, height: int, rgb: bytes | bytearray, *, level: int = 6
) -> bytes:
    """Encode 8-bit top-down RGB pixels as a PNG file."""
    if width <= 0 or height <= 0:
        raise ValueError("PNG needs a positive size")
    if len(rgb) < width * height * 3:
        raise ValueError(f"need {width * height * 3} bytes of RGB, got {len(rgb)}")
    return b"".join(
        (
            PNG_SIGNATURE,
            _ihdr(width, height),
            _chunk(b"IDAT", _idat(rgb, width, height, level)),
            _chunk(b"IEND", b""),
        )
    )


def encode_apng(
    frames: list[tuple[int, int, bytes | bytearray]], fps: float = 10.0, *, level: int = 4
) -> bytes:
    """Encode a list of equally sized RGB frames as an animated PNG.

    APNG is the one animation container that needs no encoder library: each frame is
    ordinary PNG image data, so ``zlib`` is the whole dependency. Browsers, Edge,
    Windows Photos and every image viewer that matters play it back as an animation.
    """
    if not frames:
        raise ValueError("no frames to encode")
    width, height, _ = frames[0]
    if width <= 0 or height <= 0:
        raise ValueError("APNG needs a positive size")
    # delay is a rational number of seconds: 1/den == one frame period
    den = max(1, int(round(fps or 1.0)))
    out = bytearray(PNG_SIGNATURE)
    out += _ihdr(width, height)
    out += _chunk(b"acTL", struct.pack(">II", len(frames), 0))  # 0 = loop forever
    sequence = 0
    for index, (fw, fh, rgb) in enumerate(frames):
        if (fw, fh) != (width, height):
            raise ValueError("every APNG frame must have the same size")
        out += _chunk(
            b"fcTL",
            # seq, w, h, x_offset, y_offset, delay_num, delay_den, dispose, blend
            struct.pack(">IIIIIHHBB", sequence, fw, fh, 0, 0, 1, den, 0, 0),
        )
        sequence += 1
        data = _idat(rgb, fw, fh, level)
        if index == 0:
            # the first frame is the image itself, so it rides in an IDAT chunk,
            # which is not part of the sequence numbering
            out += _chunk(b"IDAT", data)
        else:
            out += _chunk(b"fdAT", struct.pack(">I", sequence) + data)
            sequence += 1
    out += _chunk(b"IEND", b"")
    return bytes(out)


# --------------------------------------------------------------------------- decode
def decode_png(data: bytes) -> tuple[int, int, bytes]:
    """Decode an 8-bit non-interlaced RGB/RGBA/grey PNG into ``(w, h, rgb)``.

    Only what this package writes or a screenshot tool produces is supported: colour
    type 0/2/6, bit depth 8, no interlace. It exists so ``screenshot(scale=...)`` can
    still shrink an image on a machine without Pillow.
    """
    if not data.startswith(PNG_SIGNATURE):
        raise ValueError("not a PNG file")
    pos = len(PNG_SIGNATURE)
    width = height = depth = color = 0
    idat = bytearray()
    palette = b""
    while pos + 8 <= len(data):
        length = int.from_bytes(data[pos : pos + 4], "big")
        tag = data[pos + 4 : pos + 8]
        body = data[pos + 8 : pos + 8 + length]
        pos += 12 + length  # length + type + payload + crc
        if tag == b"IHDR":
            width, height, depth, color, _comp, _filt, interlace = struct.unpack(">IIBBBBB", body)
            if depth != 8 or interlace:
                raise ValueError("only 8-bit non-interlaced PNGs are supported")
        elif tag == b"PLTE":
            palette = body
        elif tag == b"IDAT":
            idat += body
        elif tag == b"IEND":
            break
    if not width or not height:
        raise ValueError("PNG has no IHDR")
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(color)
    if channels is None:
        raise ValueError(f"unsupported PNG colour type {color}")
    if color == 3 and not palette:
        raise ValueError("indexed PNG without a palette")

    raw = zlib.decompress(bytes(idat))
    stride = width * channels
    if len(raw) < (stride + 1) * height:
        raise ValueError("truncated PNG image data")
    out = bytearray(stride * height)
    prior = bytes(stride)
    for y in range(height):
        ftype = raw[y * (stride + 1)]
        line = bytearray(raw[y * (stride + 1) + 1 : (y + 1) * (stride + 1)])
        _unfilter(ftype, line, prior, channels)
        out[y * stride : (y + 1) * stride] = line
        prior = bytes(line)

    if color == 2:
        return width, height, bytes(out)
    rgb = bytearray(width * height * 3)
    if color == 6:  # RGBA -> drop alpha
        rgb[0::3] = out[0::4]
        rgb[1::3] = out[1::4]
        rgb[2::3] = out[2::4]
    elif color in (0, 4):  # grey (or grey+alpha) -> replicate
        step = 1 if color == 0 else 2
        grey = out[0::step]
        rgb[0::3] = grey
        rgb[1::3] = grey
        rgb[2::3] = grey
    else:  # indexed
        for i in range(width * height):
            index = out[i] * 3
            rgb[i * 3 : i * 3 + 3] = palette[index : index + 3]
    return width, height, bytes(rgb)


def _unfilter(ftype: int, line: bytearray, prior: bytes, bpp: int) -> None:
    """Reverse one PNG scanline filter in place (RFC 2083 section 6)."""
    if ftype == 0:
        return
    if ftype == 1:  # Sub
        for i in range(bpp, len(line)):
            line[i] = (line[i] + line[i - bpp]) & 0xFF
    elif ftype == 2:  # Up
        for i in range(len(line)):
            line[i] = (line[i] + prior[i]) & 0xFF
    elif ftype == 3:  # Average
        for i in range(len(line)):
            left = line[i - bpp] if i >= bpp else 0
            line[i] = (line[i] + ((left + prior[i]) >> 1)) & 0xFF
    elif ftype == 4:  # Paeth
        for i in range(len(line)):
            left = line[i - bpp] if i >= bpp else 0
            up = prior[i]
            upleft = prior[i - bpp] if i >= bpp else 0
            p = left + up - upleft
            pa, pb, pc = abs(p - left), abs(p - up), abs(p - upleft)
            if pa <= pb and pa <= pc:
                pred = left
            elif pb <= pc:
                pred = up
            else:
                pred = upleft
            line[i] = (line[i] + pred) & 0xFF
    else:
        raise ValueError(f"unknown PNG filter type {ftype}")


# -------------------------------------------------------------------------- resizing
def downscale_rgb(
    rgb: bytes | bytearray,
    width: int,
    height: int,
    max_width: int,
    *,
    max_height: int = 0,
) -> tuple[bytes, int, int]:
    """Nearest-neighbour downscale to fit ``max_width`` (and ``max_height`` if given).

    ``itemgetter`` does the horizontal gather in C, so even a 4K screenshot shrinks in
    a few tens of milliseconds without Pillow. Screenshots shrink with a box filter in
    GDI long before they ever reach this function.
    """
    if width <= 0 or height <= 0:
        raise ValueError("bad source size")
    factor = max_width / width if max_width > 0 else 1.0
    if max_height > 0:
        factor = min(factor, max_height / height)
    factor = min(1.0, factor)
    new_w = max(1, int(width * factor))
    new_h = max(1, int(height * factor))
    if (new_w, new_h) == (width, height):
        return bytes(rgb), width, height

    # Normalise to `bytes` so `itemgetter` below sees a plain sequence, but keep an
    # object that already is `bytes`: every in-tree caller decodes to `bytes`, and
    # re-wrapping would copy the whole 6 MB buffer and hide the row reads.
    src = rgb if isinstance(rgb, bytes) else bytes(rgb)
    src_stride = width * 3
    out = bytearray(new_w * new_h * 3)
    # byte offsets of the source pixel landing on each output pixel, using the
    # pixel-centre convention (which is what PIL/nearest and every scaler uses)
    xs = [
        min(width - 1, int((x + 0.5) * width / new_w)) * 3 + channel
        for x in range(new_w)
        for channel in (0, 1, 2)
    ]
    # new_w is at least 1, so xs always holds at least 3 offsets and itemgetter
    # always returns a tuple - no single-index special case is needed.
    pick = itemgetter(*xs)
    for y in range(new_h):
        src_y = min(height - 1, int((y + 0.5) * height / new_h))
        # the row base has to be bounded on BOTH sides. Slicing `src[base:]` and
        # trimming afterwards copies the whole remaining tail of the buffer (up
        # to 6 MB per row) and throws all but one row of it away - 1.9 GB of
        # memcpy for a 1920x1080 -> 640 downscale.
        base = src_y * src_stride
        row = src[base : base + src_stride]
        off = y * new_w * 3
        out[off : off + new_w * 3] = pick(row)
    return bytes(out), new_w, new_h
