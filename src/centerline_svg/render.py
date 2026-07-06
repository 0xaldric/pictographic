"""
Optional visualization: dump one PNG per pipeline stage so the mask -> distance ->
skeleton -> cleanup -> branches -> smoothed progression is visible.

This is *not* needed to convert PNG -> SVG; it is enabled only when the caller asks for it
(`png_to_svg(..., steps_dir=...)` or the CLI `--steps` flag). Pure stdlib — includes a tiny
hand-rolled PNG *encoder* (the mirror image of the decoder in `core.py`).
"""

from __future__ import annotations

import os
import zlib
import struct

from .core import Pipeline, Skeleton


# ----------------------------------------------------------------------------
# Minimal 8-bit RGB PNG encoder (mirror of core.decode_png)
# ----------------------------------------------------------------------------
def write_png(path: str, width: int, height: int, rgb: bytes | bytearray) -> None:
    """Frame IHDR / IDAT / IEND, prefix each scanline with filter byte 0, zlib-compress."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    # IHDR: w, h, bit depth 8, color type 2 (RGB), compression/filter/interlace = 0
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    stride = width * 3
    raw = bytearray()
    for y in range(height):
        raw.append(0)  # per-scanline filter type: None
        raw += rgb[y * stride : (y + 1) * stride]
    idat = zlib.compress(bytes(raw), 9)
    with open(path, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n")
        fh.write(chunk(b"IHDR", ihdr))
        fh.write(chunk(b"IDAT", idat))
        fh.write(chunk(b"IEND", b""))


# a few visually distinct colors to tell branches apart
_PALETTE = [
    (220, 40, 40),
    (40, 120, 220),
    (30, 160, 70),
    (230, 150, 20),
    (150, 60, 200),
    (0, 170, 180),
    (200, 40, 140),
    (120, 110, 40),
]


def _new_canvas(w: int, h: int, bg: tuple[int, int, int]) -> bytearray:
    return bytearray(bytes(bg) * (w * h))


def _put(
    canvas: bytearray,
    w: int,
    h: int,
    x: int,
    y: int,
    color: tuple[int, int, int],
    r: int = 0,
) -> None:
    """Paint an (2r+1)x(2r+1) block centered at (x, y); r>0 makes 1-px things visible."""
    c = bytes(color)
    for dy in range(-r, r + 1):
        yy = y + dy
        if yy < 0 or yy >= h:
            continue
        for dx in range(-r, r + 1):
            xx = x + dx
            if xx < 0 or xx >= w:
                continue
            o = (yy * w + xx) * 3
            canvas[o : o + 3] = c


# ----------------------------------------------------------------------------
# Renderers — one per stage, each returns (width, height, rgb bytes)
# ----------------------------------------------------------------------------
def render_mask(p: Pipeline) -> tuple[int, int, bytearray]:
    """Black shape on white — the binary mask after thresholding."""
    w, h = p.image_size
    W = p.stride
    canvas = _new_canvas(w, h, (255, 255, 255))
    for i, v in enumerate(p.mask):
        if v:
            _put(canvas, w, h, i % W - 1, i // W - 1, (0, 0, 0))
    return w, h, canvas


def render_dt(p: Pipeline) -> tuple[int, int, bytearray]:
    """Distance transform as a grayscale heat map: brighter = farther from the edge, so the
    bright ridge running down the middle *is* the medial axis the skeleton follows."""
    w, h = p.image_size
    W = p.stride
    dt = p.dt
    maxd = max((v for v in dt if v != float("inf")), default=1.0) or 1.0
    canvas = _new_canvas(w, h, (0, 0, 0))
    for i, v in enumerate(dt):
        if 0.0 < v < float("inf"):
            x, y = i % W - 1, i // W - 1
            if 0 <= x < w and 0 <= y < h:
                g = int(255 * v / maxd)
                _put(canvas, w, h, x, y, (g, g, g))
    return w, h, canvas


def render_skeleton(p: Pipeline, skel: Skeleton) -> tuple[int, int, bytearray]:
    """Red skeleton over a faint gray mask, so you can see the 1-px centerline in context
    (drawn 2-px thick to stay visible when the image is scaled down)."""
    w, h = p.image_size
    W = p.stride
    canvas = _new_canvas(w, h, (255, 255, 255))
    for i, v in enumerate(p.mask):  # faint context
        if v:
            _put(canvas, w, h, i % W - 1, i // W - 1, (230, 230, 230))
    for i in skel:  # the skeleton on top
        _put(canvas, w, h, i % W - 1, i // W - 1, (220, 30, 30), r=2)
    return w, h, canvas


def render_branches(p: Pipeline) -> tuple[int, int, bytearray]:
    """Each traced branch in its own color — shows how the skeleton was split into graph
    edges (the strokes joined at junctions)."""
    w, h = p.image_size
    W = p.stride
    canvas = _new_canvas(w, h, (255, 255, 255))
    for k, br in enumerate(p.branches):
        color = _PALETTE[k % len(_PALETTE)]
        for i in br:
            _put(canvas, w, h, i % W - 1, i // W - 1, color, r=2)
    return w, h, canvas


def render_polylines(p: Pipeline) -> tuple[int, int, bytearray]:
    """The smoothed polylines, each branch in its own color, rasterized segment by segment
    so you can compare against the jagged raw branches from the branches step."""
    w, h = p.image_size
    canvas = _new_canvas(w, h, (255, 255, 255))
    for k, pl in enumerate(p.polylines):
        color = _PALETTE[k % len(_PALETTE)]
        for (ax, ay), (bx, by) in zip(pl, pl[1:]):
            n = int(max(abs(bx - ax), abs(by - ay))) + 1
            for t in range(n + 1):
                x = round(ax + (bx - ax) * t / n)
                y = round(ay + (by - ay) * t / n)
                _put(canvas, w, h, x, y, color, r=1)
    return w, h, canvas


def dump_step(
    out_dir: str, base: str, name: str, rendered: tuple[int, int, bytearray]
) -> str:
    """Write one rendered stage to `<out_dir>/<base>_<name>.png` and return the path."""
    os.makedirs(out_dir, exist_ok=True)
    w, h, rgb = rendered
    path = os.path.join(out_dir, "%s_%s.png" % (base, name))
    write_png(path, w, h, rgb)
    return path
