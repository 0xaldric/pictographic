#!/usr/bin/env python3
"""
Centerline extraction: PNG (solid black shape) -> SVG tracing the medial axis.

Pure standard library only (zlib, struct, math). One function per pipeline step so
each part can be read and verified on its own. See README.md for the why.

Pipeline:
  1. read_png            decode PNG by hand (parse chunks, inflate, undo filters)
  2. to_mask             threshold to a 1-bit foreground mask (padded grid)
  3. zhang_suen          morphological thinning -> 1px skeleton
  4. distance_transform  two-pass chamfer, used for stroke width + spur pruning
  5. build_graph/trace   skeleton pixels -> graph edges (polyline branches)
  6. prune               drop tiny spur branches
  7. to_svg              emit stroked <path> per branch
"""

from __future__ import annotations  # lets us write list[int] / int | None on Python 3.9

import sys
import os
import zlib
import struct
import math


# ----------------------------------------------------------------------------
# Type aliases — named so a signature says what a value *means*, not just its shape.
# ----------------------------------------------------------------------------
Grid = bytearray  # padded 1-D image grid, row-major, 1 byte/pixel, stride = W (=width+2)
Skeleton = set[int]  # linear indices (into the padded grid) of skeleton pixels
Branch = list[int]  # one polyline: ordered linear indices of the pixels along an edge
Degrees = dict[int, int]  # pixel index -> topological degree (its crossing number)


# ----------------------------------------------------------------------------
# Step 1 — decode a PNG into raw pixel bytes (no libraries; zlib is stdlib)
# ----------------------------------------------------------------------------
def _paeth(a: int, b: int, c: int) -> int:
    # a = left, b = above, c = upper-left. Predicts the byte closest to the plane.
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def read_png(path: str) -> tuple[int, int, int, bytes]:
    """Return (width, height, channels, raw_bytes). raw_bytes is the de-filtered,
    row-major sample data: channels bytes per pixel, width*channels bytes per row."""
    # follow https://www.libpng.org/pub/png/spec/1.2/PNG-Structure.html
    with open(path, "rb") as fh:
        data = fh.read()

    # Just check the PNG signature; don't bother with CRCs or ancillary chunks.
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")

    # --- walk chunks: [length:4][type:4][data:length][crc:4] ---
    pos = 8
    width = height = bit_depth = color_type = None
    idat = bytearray()
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        ctype = data[pos + 4 : pos + 8]
        cdata = data[pos + 8 : pos + 8 + length]
        pos += 12 + length  # 4 len + 4 type + data + 4 crc
        # Image headers
        if ctype == b"IHDR":
            width, height, bit_depth, color_type = struct.unpack(">IIBB", cdata[:10])
        # Image data chunk
        elif ctype == b"IDAT":
            idat += cdata
        # Image end chunk
        elif ctype == b"IEND":
            break

    print(f"PNG: {width}x{height}, bit_depth={bit_depth}, color_type={color_type}")
    print(f"width={width}, height={height}, bit_depth={bit_depth}, color_type={color_type}")

    if bit_depth != 8:
        raise ValueError("only 8-bit PNGs supported (got %d)" % bit_depth)
    channels: int = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color_type]
    if color_type == 3:
        raise ValueError("palette PNGs not supported")

    # --- inflate, then undo the per-scanline filter ---
    raw: bytes = zlib.decompress(bytes(idat))
    stride = width * channels
    out = bytearray(height * stride)
    prev = bytearray(stride)  # previous (already reconstructed) scanline
    src = 0
    for y in range(height):
        ftype = raw[src]
        src += 1
        line = bytearray(raw[src : src + stride])
        src += stride
        if ftype == 0:  # None
            pass
        elif ftype == 1:  # Sub: predict from the pixel to the left
            for i in range(channels, stride):
                line[i] = (line[i] + line[i - channels]) & 0xFF
        elif ftype == 2:  # Up: predict from the pixel above
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif ftype == 3:  # Average of left and above
            for i in range(stride):
                a = line[i - channels] if i >= channels else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 0xFF
        elif ftype == 4:  # Paeth
            for i in range(stride):
                a = line[i - channels] if i >= channels else 0
                c = prev[i - channels] if i >= channels else 0
                line[i] = (line[i] + _paeth(a, prev[i], c)) & 0xFF
        else:
            raise ValueError("bad filter type %d" % ftype)
        out[y * stride : (y + 1) * stride] = line
        prev = line
    return width, height, channels, bytes(out)


# ----------------------------------------------------------------------------
# Step 2 — threshold to a binary mask on a 1-px padded grid
# ----------------------------------------------------------------------------
def to_mask(
    width: int, height: int, channels: int, raw: bytes, threshold: int = 128
) -> tuple[int, Grid]:
    """Return (W, grid) where W = width+2 (padded stride) and grid is a bytearray of
    size (width+2)*(height+2): 1 = foreground (dark), 0 = background. The 1-px border
    of zeros lets every neighbor lookup stay in bounds."""
    W = width + 2
    H = height + 2
    grid: Grid = bytearray(W * H)
    for y in range(height):
        base = y * width * channels
        row = (y + 1) * W
        for x in range(width):
            o = base + x * channels
            # luminance follow ITU-R BT.601, https://en.wikipedia.org/wiki/YCbCr#ITU-R_BT.601_conversion
            if channels >= 3:
                lum = (299 * raw[o] + 587 * raw[o + 1] + 114 * raw[o + 2]) // 1000
            else:  # grayscale / gray+alpha
                lum = raw[o]
            if lum < threshold:
                grid[row + x + 1] = 1
    return W, grid


# ----------------------------------------------------------------------------
# Step 3 — Zhang–Suen thinning -> 1px skeleton
# ----------------------------------------------------------------------------
def zhang_suen(W: int, grid: Grid) -> Skeleton:
    """Thin the foreground of `grid` in place to a 1-px, 8-connected skeleton and
    return the set of skeleton pixel linear indices."""
    # P2..P9 clockwise from North, as linear offsets into the padded grid.
    N, NE, E, SE = -W, -W + 1, 1, W + 1
    S, SW, Wst, NW = W, W - 1, -1, -W - 1
    off: list[int] = [N, NE, E, SE, S, SW, Wst, NW]  # P2,P3,P4,P5,P6,P7,P8,P9

    # Only ever look at pixels that are still foreground (this set shrinks each pass).
    fg: Skeleton = {i for i, v in enumerate(grid) if v}

    def transitions_and_count(i: int) -> tuple[int, int, list[int]]:
        # p[0..7] = P2..P9. A = number of 0->1 transitions around the ring; B = sum.
        p = [grid[i + d] for d in off]
        b = sum(p)
        a = 0
        for k in range(8):
            if p[k] == 0 and p[(k + 1) % 8] == 1:
                a += 1
        return a, b, p

    changed = True
    while changed:
        changed = False
        for sub in (0, 1):
            to_delete: list[int] = []
            for i in fg:
                a, b, p = transitions_and_count(i)
                if not (2 <= b <= 6 and a == 1):
                    continue
                P2, P3, P4, P5, P6, P7, P8, P9 = p
                if sub == 0:
                    if P2 * P4 * P6 == 0 and P4 * P6 * P8 == 0:
                        to_delete.append(i)
                else:
                    if P2 * P4 * P8 == 0 and P2 * P6 * P8 == 0:
                        to_delete.append(i)
            if to_delete:
                changed = True
                for i in to_delete:
                    grid[i] = 0
                    fg.discard(i)
    return fg


# ----------------------------------------------------------------------------
# Step 3b — cleanup: remove redundant "staircase" pixels
# ----------------------------------------------------------------------------
def _neighbors8(i: int, W: int) -> tuple[int, ...]:
    # Ring order P2..P9 (N, NE, E, SE, S, SW, W, NW) — used for stepping and crossing #.
    return (i - W, i - W + 1, i + 1, i + W + 1, i + W, i + W - 1, i - 1, i - W - 1)


def _crossing_number(i: int, W: int, skel: Skeleton) -> int:
    """Number of connected *runs* of foreground neighbors around the 8-ring. Topological
    degree: endpoint -> 1, straight/curve body -> 2, junction -> 3+. Robust to staircased
    diagonals (N + E + SE contiguous count as one run, not three separate neighbors)."""
    p = [1 if n in skel else 0 for n in _neighbors8(i, W)]
    return sum(1 for k in range(8) if p[k] == 0 and p[(k + 1) % 8] == 1)


def _neighbors_form_one_group(nbrs: list[int], W: int) -> bool:
    """True if the given neighbor pixels are all 8-connected to each other (so the pixel
    they surround is not the sole bridge between them)."""
    nbset = set(nbrs)
    seen = {nbrs[0]}
    stack = [nbrs[0]]
    while stack:
        cur = stack.pop()
        for m in _neighbors8(cur, W):
            if m in nbset and m not in seen:
                seen.add(m)
                stack.append(m)
    return len(seen) == len(nbset)


def cleanup_skeleton(W: int, skel: Skeleton) -> Skeleton:
    """Zhang–Suen leaves 2-px staircase artifacts on diagonal/curved strokes, which turn a
    smooth run into a chain of spurious junctions. Remove a pixel when it is *redundant*:
    not an endpoint (>=2 neighbors), not a real junction (crossing number <= 2), and its
    neighbors stay 8-connected without it (removing it can't disconnect anything). Straight
    and diagonal line pixels have two non-adjacent neighbor groups, so they are kept."""
    skel = set(skel)
    changed = True
    while changed:
        changed = False
        for i in list(skel):
            nb = [n for n in _neighbors8(i, W) if n in skel]
            if len(nb) < 2:
                continue
            if _crossing_number(i, W, skel) >= 3:
                continue
            if _neighbors_form_one_group(nb, W):
                skel.discard(i)
                changed = True
    return skel


# ----------------------------------------------------------------------------
# Step 4 — distance transform (two-pass chamfer) for width + pruning
# ----------------------------------------------------------------------------
def distance_transform(W: int, H: int, mask_grid: Grid) -> list[float]:
    """Approximate Euclidean distance from each foreground pixel to the nearest
    background pixel, via a two-pass chamfer (weights 1 orthogonal, sqrt2 diagonal)."""
    A, B = 1.0, math.sqrt(2.0)
    INF = float("inf")
    dt: list[float] = [0.0 if v == 0 else INF for v in mask_grid]
    # forward pass: top-left -> bottom-right
    for y in range(1, H - 1):
        row = y * W
        for x in range(1, W - 1):
            i = row + x
            if dt[i] == 0.0:
                continue
            dt[i] = min(
                dt[i],
                dt[i - 1] + A,
                dt[i - W] + A,
                dt[i - W - 1] + B,
                dt[i - W + 1] + B,
            )
    # backward pass: bottom-right -> top-left
    for y in range(H - 2, 0, -1):
        row = y * W
        for x in range(W - 2, 0, -1):
            i = row + x
            if dt[i] == 0.0:
                continue
            dt[i] = min(
                dt[i],
                dt[i + 1] + A,
                dt[i + W] + A,
                dt[i + W + 1] + B,
                dt[i + W - 1] + B,
            )
    return dt


# ----------------------------------------------------------------------------
# Step 5 — skeleton pixels -> graph edges (polyline branches)
# ----------------------------------------------------------------------------
def trace_branches(W: int, skel: Skeleton) -> tuple[list[Branch], set[int], Degrees]:
    """Split the skeleton graph into edges. Each edge is a list of linear indices
    running between two 'special' nodes (degree != 2) or around a pure loop."""
    skel = set(skel)
    deg: Degrees = {i: _crossing_number(i, W, skel) for i in skel}
    specials: set[int] = {i for i in skel if deg[i] != 2}

    branches: list[Branch] = []
    used: set[int] = set()  # interior (degree-2) pixels already consumed by a branch
    started: set[tuple[int, int]] = set()  # directed (from, first_step) half-edges done
    cap = len(skel) + 2  # hard step cap so a malformed skeleton can never hang

    def walk(s: int, n: int) -> tuple[Branch, int | None, int]:
        """Walk from special node s into neighbor n along degree-2 pixels until the next
        special node (or a dead end). Returns (path, end_node, pixel_before_end)."""
        path: Branch = [s]
        prev, cur = s, n
        for _ in range(cap):
            path.append(cur)
            if cur in specials:
                return path, cur, prev
            used.add(cur)
            nxt = [
                m
                for m in _neighbors8(cur, W)
                if m in skel and m != prev and m not in used
            ]
            if not nxt:
                # no fresh continuation: attach to an adjacent junction if one is here
                sp = [m for m in _neighbors8(cur, W) if m in specials and m != prev]
                if sp:
                    path.append(sp[0])
                    return path, sp[0], cur
                return path, None, prev
            prev, cur = cur, nxt[0]
        return path, None, prev

    # 1) edges anchored at special nodes (endpoints + junctions)
    for s in specials:
        for n in _neighbors8(s, W):
            if n not in skel or (s, n) in started or n in used:
                continue
            started.add((s, n))
            path, end_node, before_end = walk(s, n)
            if end_node is not None:
                started.add((end_node, before_end))  # don't retrace from the far end
            branches.append(path)

    # 2) pure loops (cycles of degree-2 pixels with no special node)
    remaining = skel - used - specials
    while remaining:
        start = next(iter(remaining))
        path = [start]
        prev, cur = None, start
        for _ in range(cap):
            used.add(cur)
            remaining.discard(cur)
            nxt = [
                m
                for m in _neighbors8(cur, W)
                if m in skel and m != prev and (m == start or m not in used)
            ]
            if not nxt:
                break
            prev, cur = cur, nxt[0]
            path.append(cur)
            if cur == start:
                break
        branches.append(path)

    return branches, specials, deg


# ----------------------------------------------------------------------------
# Step 6 — prune short spur branches
# ----------------------------------------------------------------------------
def _polyline_length(path: Branch, W: int) -> float:
    total = 0.0
    for a, b in zip(path, path[1:]):
        ax, ay = a % W, a // W
        bx, by = b % W, b // W
        total += math.hypot(ax - bx, ay - by)
    return total


def prune(branches: list[Branch], deg: Degrees, W: int, min_len: float) -> list[Branch]:
    """Remove branches that dead-end at an endpoint (degree-1) and are shorter than
    `min_len` — the little spurs thinning leaves at corners and rounded ends."""
    kept: list[Branch] = []
    for path in branches:
        endpoint_ends = sum(1 for p in (path[0], path[-1]) if deg.get(p, 0) == 1)
        if endpoint_ends >= 1 and _polyline_length(path, W) < min_len:
            continue
        kept.append(path)
    return kept


# ----------------------------------------------------------------------------
# Step 7 — emit SVG
# ----------------------------------------------------------------------------
def to_svg(
    branches: list[Branch], W: int, width: int, height: int, stroke_width: float
) -> str:
    def d_of(path: Branch) -> str:
        pts: list[str] = []
        for k, i in enumerate(path):
            x, y = i % W - 1, i // W - 1  # undo the 1-px padding
            pts.append(("M" if k == 0 else "L") + "%d %d" % (x, y))
        return "".join(pts)

    lines: list[str] = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" '
        'viewBox="0 0 %d %d">' % (width, height, width, height),
        '<g fill="none" stroke="#000000" stroke-width="%g" '
        'stroke-linecap="round" stroke-linejoin="round">' % stroke_width,
    ]
    for path in branches:
        if len(path) >= 2:
            lines.append('<path d="%s"/>' % d_of(path))
    lines.append("</g>")
    lines.append("</svg>")
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# Pipeline — a fluent builder that wires the step functions together
# ----------------------------------------------------------------------------
class Pipeline:
    """Builds and runs the centerline pipeline with a fluent, chainable API.

    The step functions above are the pure "how"; this class is the "orchestration":
    it carries the intermediate state between stages and the configuration, so a run
    reads top-to-bottom as the pipeline itself:

        svg = (Pipeline()
                   .threshold(128)          # config
                   .load("in.png")          # stages...
                   .to_mask()
                   .distance_transform()
                   .thin()
                   .cleanup()
                   .trace()
                   .prune()
                   .to_svg()
                   .result())

    Every config setter and every stage returns `self`, so they can be chained. Stages
    assume the previous stage has run (each documents what state it needs / produces).
    """

    def __init__(self) -> None:
        # --- configuration (with sensible defaults) ---
        self._threshold: int = 128  # luminance below this = foreground
        self._stroke_width: float | None = None  # None -> auto (2 * median distance)
        self._prune_factor: float = 1.5  # drop spurs shorter than factor * median radius
        # --- state, filled in as stages run ---
        self._w: int = 0
        self._h: int = 0
        self._W: int = 0
        self._H: int = 0
        self._channels: int = 0
        self._raw: bytes | None = None
        self._mask: Grid | None = None
        self._dt: list[float] | None = None
        self._skel: Skeleton | None = None
        self._deg: Degrees | None = None
        self._branches: list[Branch] | None = None
        self._svg: str | None = None
        self._stroke_used: float | None = None

    # ---- configuration (builder setters) ----
    def threshold(self, value: int) -> Pipeline:
        self._threshold = value
        return self

    def stroke_width(self, value: float | None) -> Pipeline:
        """Force a fixed stroke width; pass None to keep the auto (distance-transform) estimate."""
        self._stroke_width = value
        return self

    def prune_factor(self, value: float) -> Pipeline:
        self._prune_factor = value
        return self

    # ---- stages (each mutates state, returns self) ----
    def load(self, path: str) -> Pipeline:
        self._w, self._h, self._channels, self._raw = read_png(path)
        self._W, self._H = self._w + 2, self._h + 2
        return self

    def to_mask(self) -> Pipeline:
        self._mask = to_mask(
            self._w, self._h, self._channels, self._raw, self._threshold
        )[1]
        return self

    def distance_transform(self) -> Pipeline:
        # run on the mask before thinning consumes it
        self._dt = distance_transform(self._W, self._H, self._mask)
        return self

    def thin(self) -> Pipeline:
        grid = bytearray(self._mask)  # thinning mutates its grid; keep the mask intact
        self._skel = zhang_suen(self._W, grid)
        return self

    def cleanup(self) -> Pipeline:
        self._skel = cleanup_skeleton(self._W, self._skel)
        return self

    def trace(self) -> Pipeline:
        self._branches, _specials, self._deg = trace_branches(self._W, self._skel)
        return self

    def prune(self) -> Pipeline:
        self._branches = prune(
            self._branches,
            self._deg,
            self._W,
            min_len=self._median_radius() * self._prune_factor,
        )
        return self

    def to_svg(self) -> Pipeline:
        self._stroke_used = (
            self._stroke_width
            if self._stroke_width is not None
            else round(2 * self._median_radius(), 1)
        )
        self._svg = to_svg(self._branches, self._W, self._w, self._h, self._stroke_used)
        return self

    def save(self, out_path: str) -> Pipeline:
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        with open(out_path, "w") as fh:
            fh.write(self._svg)
        return self

    # ---- outputs / stats ----
    def result(self) -> str | None:
        return self._svg

    def stats(self) -> dict[str, object]:
        return {
            "fg": sum(self._mask) if self._mask else 0,
            "skeleton": len(self._skel) if self._skel else 0,
            "branches": len(self._branches) if self._branches else 0,
            "stroke_width": self._stroke_used,
        }

    # ---- helper ----
    def _median_radius(self) -> float:
        radii = sorted(self._dt[i] for i in self._skel)
        return radii[len(radii) // 2] if radii else 22.5


# ----------------------------------------------------------------------------
# Driver
# ----------------------------------------------------------------------------
def convert(in_path: str, out_path: str, verbose: bool = True) -> None:
    p = (
        Pipeline()
        .load(in_path)
        .to_mask()
        .distance_transform()
        .thin()
        .cleanup()
        .trace()
        .prune()
        .to_svg()
        .save(out_path)
    )
    if verbose:
        s = p.stats()
        print(
            "%-28s fg=%d skel=%d branches=%d width=%s"
            % (
                os.path.basename(in_path),
                s["fg"],
                s["skeleton"],
                s["branches"],
                s["stroke_width"],
            )
        )


def main(argv: list[str]) -> int:
    if len(argv) == 3 and os.path.isfile(argv[1]):
        convert(argv[1], argv[2])
    elif len(argv) == 3 and os.path.isdir(argv[1]):
        in_dir, out_dir = argv[1], argv[2]
        for name in sorted(os.listdir(in_dir)):
            if name.lower().endswith(".png"):
                convert(
                    os.path.join(in_dir, name),
                    os.path.join(out_dir, name[:-4] + ".svg"),
                )
    else:
        print("usage: python3 centerline.py <in.png> <out.svg>")
        print("       python3 centerline.py <in_dir> <out_dir>")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
