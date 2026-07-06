"""
centerline_svg — trace the centerline (medial axis) of a solid shape in a PNG and emit
an SVG of thin stroked paths following it. Pure standard library.

Quick use:

    import centerline_svg
    svg = centerline_svg.png_to_svg("arrow.png")          # from a path
    svg = centerline_svg.png_to_svg(open("a.png", "rb").read())  # from bytes

    # optional: also dump one debug PNG per pipeline stage
    svg = centerline_svg.png_to_svg("arrow.png", steps_dir="steps/")

For fine control, use the fluent `Pipeline` directly (see centerline_svg.core).
"""

from __future__ import annotations

import os
from typing import Union

from .core import Pipeline, decode_png, read_png

__version__ = "0.1.0"
__all__ = ["png_to_svg", "Pipeline", "decode_png", "read_png", "__version__"]

Source = Union[str, "os.PathLike[str]", bytes, bytearray]


def png_to_svg(
    source: Source,
    *,
    threshold: int = 128,
    stroke_width: float | None = None,
    prune_factor: float = 1.5,
    rdp_eps: float = 2.0,
    chaikin_iters: int = 2,
    steps_dir: str | None = None,
) -> str:
    """Convert a PNG (a solid dark shape on a light background) into an SVG string whose
    paths trace the shape's centerline.

    Parameters
    ----------
    source        : path (str / os.PathLike) OR the raw PNG bytes.
    threshold     : luminance < threshold counts as foreground (ink). Default 128.
    stroke_width  : output stroke width in px; None -> auto (2 x median stroke radius).
    prune_factor  : drop spur branches shorter than prune_factor x median radius.
    rdp_eps       : Ramer-Douglas-Peucker tolerance (px) used while smoothing.
    chaikin_iters : Chaikin corner-cutting passes for genuinely curved strokes.
    steps_dir     : if given, ALSO write one debug PNG per stage into this folder
                    (display flag). None (default) = no visualization, SVG only.

    Returns the SVG document as a string.
    """
    p = (
        Pipeline()
        .threshold(threshold)
        .stroke_width(stroke_width)
        .prune_factor(prune_factor)
    )
    if isinstance(source, (bytes, bytearray)):
        p.load_bytes(bytes(source))
        base = "image"
    else:
        path = os.fspath(source)
        p.load(path)
        base = os.path.splitext(os.path.basename(path))[0]

    if steps_dir is None:
        # fast path: no visualization
        (
            p.to_mask()
            .distance_transform()
            .thin()
            .cleanup()
            .trace()
            .prune()
            .smooth(rdp_eps=rdp_eps, chaikin_iters=chaikin_iters)
            .to_svg()
        )
        return p.result()

    # display flag on: run stage by stage, dumping a PNG after each
    from . import render as _r  # imported lazily so the fast path stays dependency-free

    p.to_mask()
    _r.dump_step(steps_dir, base, "1_mask", _r.render_mask(p))
    p.distance_transform()
    _r.dump_step(steps_dir, base, "2_distance", _r.render_dt(p))
    p.thin()
    _r.dump_step(steps_dir, base, "3_skeleton", _r.render_skeleton(p, p.skeleton))
    p.cleanup()
    _r.dump_step(steps_dir, base, "4_cleanup", _r.render_skeleton(p, p.skeleton))
    p.trace().prune()
    _r.dump_step(steps_dir, base, "5_branches", _r.render_branches(p))
    p.smooth(rdp_eps=rdp_eps, chaikin_iters=chaikin_iters)
    _r.dump_step(steps_dir, base, "6_smoothed", _r.render_polylines(p))
    p.to_svg()
    return p.result()
