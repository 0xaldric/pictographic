"""Command-line interface: `centerline-svg in.png out.svg [--steps DIR]`."""

from __future__ import annotations

import argparse
import os
import sys

from . import __version__, png_to_svg


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="centerline-svg",
        description="Trace the centerline (medial axis) of a solid PNG shape into an SVG.",
    )
    ap.add_argument("input", help="input PNG file, or a directory of PNGs")
    ap.add_argument("output", help="output SVG file, or a directory for batch mode")
    ap.add_argument(
        "--threshold", type=int, default=128, help="ink luminance cutoff (0-255)"
    )
    ap.add_argument(
        "--stroke-width", type=float, default=None, help="fixed stroke width (px)"
    )
    ap.add_argument(
        "--prune-factor", type=float, default=1.5, help="spur pruning strength"
    )
    ap.add_argument("--rdp-eps", type=float, default=2.0, help="RDP tolerance (px)")
    ap.add_argument("--chaikin", type=int, default=2, help="Chaikin smoothing passes")
    ap.add_argument(
        "--steps",
        metavar="DIR",
        default=None,
        help="also dump one debug PNG per pipeline stage into DIR",
    )
    ap.add_argument("--version", action="version", version="%(prog)s " + __version__)
    return ap


def _convert_file(inp: str, outp: str, args: argparse.Namespace) -> None:
    svg = png_to_svg(
        inp,
        threshold=args.threshold,
        stroke_width=args.stroke_width,
        prune_factor=args.prune_factor,
        rdp_eps=args.rdp_eps,
        chaikin_iters=args.chaikin,
        steps_dir=args.steps,
    )
    os.makedirs(os.path.dirname(outp) or ".", exist_ok=True)
    with open(outp, "w") as fh:
        fh.write(svg)
    print("wrote", outp)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if os.path.isdir(args.input):
        os.makedirs(args.output, exist_ok=True)
        for name in sorted(os.listdir(args.input)):
            if name.lower().endswith(".png"):
                _convert_file(
                    os.path.join(args.input, name),
                    os.path.join(args.output, name[:-4] + ".svg"),
                    args,
                )
    else:
        _convert_file(args.input, args.output, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
