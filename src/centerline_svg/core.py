"""
Centerline extraction core: PNG (solid shape) -> SVG tracing the medial axis.

Pure standard library only (zlib, struct, math). One function per pipeline step, wired
together by the fluent `Pipeline` class. The rendering / step-image helpers live in
`render.py`; this module is the algorithm only. See README.md for the "why".

Pipeline:
  1. decode_png          decode PNG by hand (parse chunks, inflate, undo filters)
  2. to_mask             threshold to a 1-bit foreground mask (padded grid)
  3. zhang_suen          morphological thinning -> 1px skeleton
     cleanup_skeleton    remove staircase artifacts
  4. distance_transform  two-pass chamfer, used for stroke width + spur pruning
  5. trace_branches      skeleton pixels -> graph edges (polyline branches)
  6. prune               drop tiny spur branches
     smooth              straighten strokes, re-solve junctions, smooth curves
  7. to_svg              emit stroked <path> per branch
"""

from __future__ import annotations  # lets us write list[int] / int | None on Python 3.9

import os
import zlib
import struct
import math

# ----------------------------------------------------------------------------
# Type aliases — named so a signature says what a value *means*, not just its shape.
# ----------------------------------------------------------------------------
# padded 1-D image grid, row-major, 1 byte/pixel, stride = W (= width+2)
Grid = bytearray
Skeleton = set[int]  # linear indices (into the padded grid) of skeleton pixels
Branch = list[int]  # one polyline: ordered linear indices of the pixels along an edge
Degrees = dict[int, int]  # pixel index -> topological degree (its crossing number)
Point = tuple[float, float]  # a real (x, y) coordinate (padding already removed)
Polyline = list[Point]  # a branch as float points, ready to smooth / emit


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


def decode_png(data: bytes) -> tuple[int, int, int, bytes]:
    """Decode raw PNG bytes -> (width, height, channels, raw_bytes). raw_bytes is the
    de-filtered, row-major sample data: channels bytes per pixel, width*channels per row.
    Supports 8-bit grayscale / RGB / (gray|rgb)+alpha; no palette, no interlacing."""
    # follow https://www.libpng.org/pub/png/spec/1.2/PNG-Structure.html
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
        if ctype == b"IHDR":
            width, height, bit_depth, color_type = struct.unpack(">IIBB", cdata[:10])
        elif ctype == b"IDAT":
            idat += cdata
        elif ctype == b"IEND":
            break

    if bit_depth != 8:
        raise ValueError("only 8-bit PNGs supported (got %r)" % bit_depth)
    if color_type == 3:
        raise ValueError("palette PNGs not supported")
    channels: int = {0: 1, 2: 3, 4: 2, 6: 4}[color_type]

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


def read_png(path: str) -> tuple[int, int, int, bytes]:
    """Read a PNG file from disk. See `decode_png`."""
    with open(path, "rb") as fh:
        return decode_png(fh.read())


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
            # luminance per ITU-R BT.601
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
# Step 6b — straighten strokes / re-solve junctions / smooth curves
# ----------------------------------------------------------------------------
def branch_to_points(branch: Branch, W: int) -> Polyline:
    """Linear indices -> real (x, y) float points (undo the 1-px padding)."""
    return [(float(i % W - 1), float(i // W - 1)) for i in branch]


def _perp_dist(p: Point, a: Point, b: Point) -> float:
    """Perpendicular distance from point p to the line through a and b."""
    (px, py), (ax, ay), (bx, by) = p, a, b
    dx, dy = bx - ax, by - ay
    seg = math.hypot(dx, dy)
    if seg < 1e-9:
        return math.hypot(px - ax, py - ay)
    return abs((px - ax) * dy - (py - ay) * dx) / seg


def rdp(points: Polyline, eps: float) -> Polyline:
    """Ramer-Douglas-Peucker: keep only the vertices that carry the shape (those farther
    than `eps` from the chord), dropping the rest. This turns a staircased/wobbly run into
    clean straight segments — a straight stroke collapses to just 2 endpoints."""
    if len(points) < 3:
        return points[:]
    dmax, idx = 0.0, 0
    for k in range(1, len(points) - 1):
        d = _perp_dist(points[k], points[0], points[-1])
        if d > dmax:
            dmax, idx = d, k
    if dmax > eps:  # a real corner/bend lives here -> split and recurse
        left = rdp(points[: idx + 1], eps)
        right = rdp(points[idx:], eps)
        return left[:-1] + right
    return [points[0], points[-1]]  # everything between is within eps of the chord


def _chaikin_open(points: Polyline, iterations: int) -> Polyline:
    """Chaikin corner-cutting on an open polyline; endpoints stay fixed so branches that
    share a junction remain joined."""
    for _ in range(iterations):
        if len(points) < 3:
            break
        new: Polyline = [points[0]]
        for a, b in zip(points, points[1:]):
            new.append((0.75 * a[0] + 0.25 * b[0], 0.75 * a[1] + 0.25 * b[1]))
            new.append((0.25 * a[0] + 0.75 * b[0], 0.25 * a[1] + 0.75 * b[1]))
        new.append(points[-1])
        points = new
    return points


def _chaikin_closed(points: Polyline, iterations: int) -> Polyline:
    """Chaikin on a closed loop (wraps around; no fixed endpoints)."""
    for _ in range(iterations):
        if len(points) < 3:
            break
        new: Polyline = []
        n = len(points)
        for k in range(n):
            a, b = points[k], points[(k + 1) % n]
            new.append((0.75 * a[0] + 0.25 * b[0], 0.75 * a[1] + 0.25 * b[1]))
            new.append((0.25 * a[0] + 0.75 * b[0], 0.25 * a[1] + 0.75 * b[1]))
        points = new
    return points


def _straighten_junction_ends(
    points: Polyline, start_junc: bool, end_junc: bool, radius: float
) -> Polyline:
    """Replace the wiggle within `radius` of a junction endpoint with a straight run from
    the node to the first point that is `radius` away — so the branch meets the junction
    in a straight line instead of the medial axis's little hook."""
    if len(points) < 3:
        return points
    pts = points
    if start_junc:
        j = 1
        while j < len(pts) - 1 and math.dist(pts[j], pts[0]) < radius:
            j += 1
        pts = [pts[0]] + pts[j:]
    if end_junc and len(pts) >= 3:
        j = len(pts) - 2
        while j > 0 and math.dist(pts[j], pts[-1]) < radius:
            j -= 1
        pts = pts[: j + 1] + [pts[-1]]
    return pts


def _fit_line_tls(pts: Polyline) -> tuple[Point, Point]:
    """Total-least-squares line fit: returns (centroid, unit direction). Unlike the chord
    between the endpoints, this is insensitive to where the ends bend."""
    n = len(pts)
    mx = sum(p[0] for p in pts) / n
    my = sum(p[1] for p in pts) / n
    sxx = syy = sxy = 0.0
    for x, y in pts:
        dx, dy = x - mx, y - my
        sxx += dx * dx
        syy += dy * dy
        sxy += dx * dy
    theta = 0.5 * math.atan2(2.0 * sxy, sxx - syy)
    return (mx, my), (math.cos(theta), math.sin(theta))


def _fit_straight(
    points: Polyline, trim: float, tol: float = 3.0
) -> tuple[Point, Point, tuple[Point, Point]] | None:
    """Detect a genuinely straight stroke and return it dead straight.

    The medial axis of a straight stroke bends near its ends (at caps cut at an angle and
    at junctions), so we fit the line to the STABLE INTERIOR only — the points more than
    `trim` (≈ one stroke width) of arc length away from either end. If every interior
    point sits within `tol` of that TLS line, the branch is a straight stroke: return the
    original endpoints PROJECTED onto the fitted line (so the bent tail can no longer
    rotate the axis) plus the line itself for junction re-solving. Curves and L-shaped
    branches deviate far more than `tol` and return None."""
    if len(points) < 5:
        return None
    cum = [0.0]
    for a, b in zip(points, points[1:]):
        cum.append(cum[-1] + math.dist(a, b))
    total = cum[-1]
    if total < 4 * tol:
        return None
    interior = [p for p, s in zip(points, cum) if trim <= s <= total - trim]
    if len(interior) < 5:
        interior = points
    c, d = _fit_line_tls(interior)
    nx, ny = -d[1], d[0]  # unit normal

    def perp(p: Point) -> float:
        return abs((p[0] - c[0]) * nx + (p[1] - c[1]) * ny)

    if max(perp(p) for p in interior) > tol:
        return None

    def proj(p: Point) -> Point:
        t = (p[0] - c[0]) * d[0] + (p[1] - c[1]) * d[1]
        return (c[0] + t * d[0], c[1] + t * d[1])

    return proj(points[0]), proj(points[-1]), (c, d)


def _lsq_node(lines: list[tuple[Point, Point]], fallback: Point) -> Point:
    """The point minimizing the summed squared perpendicular distance to all `lines`
    ((centroid, direction) pairs) — i.e. the best common intersection of the stroke axes.
    Falls back when the system is degenerate (nearly parallel lines)."""
    a11 = a12 = a22 = b1 = b2 = 0.0
    for c, d in lines:
        nx, ny = -d[1], d[0]
        s = nx * c[0] + ny * c[1]
        a11 += nx * nx
        a12 += nx * ny
        a22 += ny * ny
        b1 += nx * s
        b2 += ny * s
    det = a11 * a22 - a12 * a12
    if abs(det) < 1e-9:
        return fallback
    return ((a22 * b1 - a12 * b2) / det, (a11 * b2 - a12 * b1) / det)


def _turn_angle(a: Point, b: Point) -> float:
    """Angle (radians) between direction vectors a and b (0 = straight, pi = U-turn)."""
    la, lb = math.hypot(*a), math.hypot(*b)
    if la < 1e-9 or lb < 1e-9:
        return 0.0
    dot = (a[0] * b[0] + a[1] * b[1]) / (la * lb)
    return math.acos(max(-1.0, min(1.0, dot)))


def smooth_polyline(
    points: Polyline, rdp_eps: float, chaikin_iters: int, corner_deg: float = 45.0
) -> Polyline:
    """Simplify then smooth one branch, but keep genuine sharp corners sharp.

    A closed loop is smoothed as a closed curve. An open branch is RDP-simplified (straight
    strokes collapse to 2 points), then we split it at vertices whose turn exceeds
    `corner_deg` and Chaikin-smooth each straight-ish run separately — so a 90° corner
    (an H crossbar meeting a stem, the arrow's turn, an arrowhead tip) stays a crisp corner
    while only gentle curves get rounded. Endpoints stay fixed so junctions stay joined.
    """
    if len(points) >= 3 and points[0] == points[-1]:  # closed loop
        core = _chaikin_closed(points[:-1], chaikin_iters)
        return core + [core[0]]

    v = rdp(points, rdp_eps)
    if len(v) <= 2:
        return v

    # corner vertices = sharp turns; they split the branch into runs kept sharp between.
    thr = math.radians(corner_deg)
    corners = [0]
    for k in range(1, len(v) - 1):
        a = (v[k][0] - v[k - 1][0], v[k][1] - v[k - 1][1])
        b = (v[k + 1][0] - v[k][0], v[k + 1][1] - v[k][1])
        if _turn_angle(a, b) > thr:
            corners.append(k)
    corners.append(len(v) - 1)

    out: Polyline = [v[corners[0]]]
    for s, e in zip(corners, corners[1:]):
        run = _chaikin_open(v[s : e + 1], chaikin_iters)
        out.extend(run[1:])  # first already present; keep the rest incl. the corner
    return out


# ----------------------------------------------------------------------------
# Step 7 — emit SVG
# ----------------------------------------------------------------------------
def to_svg(
    polylines: list[Polyline], width: int, height: int, stroke_width: float
) -> str:
    def d_of(pts: Polyline) -> str:
        out: list[str] = []
        for k, (x, y) in enumerate(pts):
            out.append(("M" if k == 0 else "L") + "%.1f %.1f" % (x, y))
        return "".join(out)

    lines: list[str] = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" '
        'viewBox="0 0 %d %d">' % (width, height, width, height),
        '<g fill="none" stroke="#000000" stroke-width="%g" '
        'stroke-linecap="round" stroke-linejoin="round">' % stroke_width,
    ]
    for pts in polylines:
        if len(pts) >= 2:
            lines.append('<path d="%s"/>' % d_of(pts))
    lines.append("</g>")
    lines.append("</svg>")
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# Pipeline — a fluent builder that wires the step functions together
# ----------------------------------------------------------------------------
class Pipeline:
    """Builds and runs the centerline pipeline with a fluent, chainable API.

    The step functions above are the pure "how"; this class is the "orchestration": it
    carries the intermediate state between stages and the configuration, so a run reads
    top-to-bottom as the pipeline itself:

        svg = (Pipeline()
                   .load("in.png")
                   .to_mask().distance_transform().thin().cleanup()
                   .trace().prune().smooth().to_svg()
                   .result())

    Every config setter and every stage returns `self`. The read-only properties expose
    the intermediate state (used by `render.py` for the optional step images).
    """

    def __init__(self) -> None:
        # --- configuration (with sensible defaults) ---
        self._threshold: int = 128  # luminance below this = foreground
        self._stroke_width: float | None = None  # None -> auto (2 * median distance)
        self._prune_factor: float = (
            1.5  # drop spurs shorter than factor * median radius
        )
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
        self._polylines: list[Polyline] | None = None
        self._svg: str | None = None
        self._stroke_used: float | None = None

    # ---- configuration (builder setters) ----
    def threshold(self, value: int) -> Pipeline:
        self._threshold = value
        return self

    def stroke_width(self, value: float | None) -> Pipeline:
        """Force a fixed stroke width; None keeps the auto (distance-transform) estimate."""
        self._stroke_width = value
        return self

    def prune_factor(self, value: float) -> Pipeline:
        self._prune_factor = value
        return self

    # ---- stages (each mutates state, returns self) ----
    def load(self, path: str) -> Pipeline:
        with open(path, "rb") as fh:
            return self.load_bytes(fh.read())

    def load_bytes(self, data: bytes) -> Pipeline:
        self._w, self._h, self._channels, self._raw = decode_png(data)
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

    def smooth(
        self,
        rdp_eps: float = 2.0,
        chaikin_iters: int = 2,
        junction_radius: float | None = None,
    ) -> Pipeline:
        """Turn each branch into its final polyline, fixing the two places the raw medial
        axis is systematically wrong for pen strokes:

        1. STRAIGHT STROKES BEND AT THEIR ENDS: near an angled end cap and near a junction
           the medial axis hooks sideways, so a chord through the endpoints is rotated off
           the true stroke axis. `_fit_straight` fits the axis to the stable interior only
           and projects the endpoints onto it -> the stroke is dead straight at the true
           angle. Curves/L-shapes fail its tolerance and get corner-aware Chaikin instead.
        2. JUNCTIONS: the node pixel sits wherever thinning left it, not where the stroke
           axes actually cross. We re-solve each junction as the least-squares intersection
           of the fitted axes of its straight arms, then snap every arm's end to that node
           — so all arms meet, straight, at one exact point."""
        r = (
            junction_radius
            if junction_radius is not None
            else self._median_radius() * 2.0
        )
        W = self._W

        def pix_xy(i: int) -> Point:
            return (float(i % W - 1), float(i // W - 1))

        # ---- pass 1: per-branch polyline + axis fit ----
        entries: list[dict] = []  # {"pts": Polyline, "line": (c, d) | None}
        jends: list[tuple[int, int, int]] = []  # (entry idx, end 0|1, junction pixel)
        for b in self._branches:
            pts = branch_to_points(b, W)
            closed = len(b) >= 3 and b[0] == b[-1]
            if closed:
                entries.append(
                    {"pts": smooth_polyline(pts, rdp_eps, chaikin_iters), "line": None}
                )
                continue
            sj = b[0] if self._deg.get(b[0], 0) >= 3 else None
            ej = b[-1] if self._deg.get(b[-1], 0) >= 3 else None
            pts = _straighten_junction_ends(pts, sj is not None, ej is not None, r)
            fit = _fit_straight(pts, trim=r)
            if fit is not None:
                p0, p1, line = fit
                entries.append({"pts": [p0, p1], "line": line})
            else:
                entries.append(
                    {"pts": smooth_polyline(pts, rdp_eps, chaikin_iters), "line": None}
                )
            k = len(entries) - 1
            if sj is not None:
                jends.append((k, 0, sj))
            if ej is not None:
                jends.append((k, 1, ej))

        # ---- pass 2: re-solve each junction node and snap the arms to it ----
        # cluster junction pixels that sit within a few px of each other into one node
        pixels = sorted({j for _, _, j in jends})
        parent = {p: p for p in pixels}

        def find(p: int) -> int:
            while parent[p] != p:
                parent[p] = parent[parent[p]]
                p = parent[p]
            return p

        for a_i, p in enumerate(pixels):
            for q in pixels[a_i + 1 :]:
                if math.dist(pix_xy(p), pix_xy(q)) <= 4.0:
                    parent[find(p)] = find(q)

        groups: dict[int, list[tuple[int, int, int]]] = {}
        for e, end, j in jends:
            groups.setdefault(find(j), []).append((e, end, j))

        for members in groups.values():
            fallback = (
                sum(pix_xy(j)[0] for _, _, j in members) / len(members),
                sum(pix_xy(j)[1] for _, _, j in members) / len(members),
            )
            lines = [
                entries[e]["line"]
                for e, _, _ in members
                if entries[e]["line"] is not None
            ]
            node = _lsq_node(lines, fallback) if len(lines) >= 2 else fallback
            if math.dist(node, fallback) > r:  # degenerate intersection: stay local
                node = fallback
            for e, end, _ in members:
                pts = entries[e]["pts"]
                if end == 0:
                    pts[0] = node
                else:
                    pts[-1] = node

        self._polylines = [en["pts"] for en in entries]
        return self

    def to_svg(self) -> Pipeline:
        # if .smooth() was skipped, emit the raw branch polylines as-is
        if self._polylines is None:
            self._polylines = [branch_to_points(b, self._W) for b in self._branches]
        self._stroke_used = (
            self._stroke_width
            if self._stroke_width is not None
            else round(2 * self._median_radius(), 1)
        )
        self._svg = to_svg(self._polylines, self._w, self._h, self._stroke_used)
        return self

    def save(self, out_path: str) -> Pipeline:
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        with open(out_path, "w") as fh:
            fh.write(self._svg)
        return self

    # ---- outputs / stats ----
    def result(self) -> str:
        if self._svg is None:
            raise RuntimeError("call .to_svg() before .result()")
        return self._svg

    def stats(self) -> dict[str, object]:
        return {
            "fg": sum(self._mask) if self._mask else 0,
            "skeleton": len(self._skel) if self._skel else 0,
            "branches": len(self._branches) if self._branches else 0,
            "stroke_width": self._stroke_used,
        }

    # ---- read-only access to intermediate state (used by render.py) ----
    @property
    def image_size(self) -> tuple[int, int]:
        return (self._w, self._h)  # real width, height (without the 1-px padding)

    @property
    def stride(self) -> int:
        return self._W  # padded row width; index = (y+1)*W + (x+1)

    @property
    def mask(self) -> Grid | None:
        return self._mask

    @property
    def dt(self) -> list[float] | None:
        return self._dt

    @property
    def skeleton(self) -> Skeleton | None:
        return self._skel

    @property
    def branches(self) -> list[Branch] | None:
        return self._branches

    @property
    def polylines(self) -> list[Polyline] | None:
        return self._polylines

    # ---- helper ----
    def _median_radius(self) -> float:
        radii = sorted(self._dt[i] for i in self._skel)
        return radii[len(radii) // 2] if radii else 22.5
