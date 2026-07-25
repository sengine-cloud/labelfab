"""Command line entry point.

Everything here works without a printer. ``preview`` in particular is meant to be
the fast loop: edit a preset, see the label in the terminal, repeat.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from PIL import Image

from labelfab.contract import PrintJob, job_schema
from labelfab.render import RenderConfig, concat_strip, render_job, to_device


def _load_job(path: str) -> PrintJob:
    raw = sys.stdin.read() if path == "-" else Path(path).read_text()
    return PrintJob.model_validate_json(raw)


def _config(args) -> RenderConfig:
    return RenderConfig(
        qr_base_url=args.qr_base_url,
        threshold=args.threshold,
        rotation=args.rotation,
        mirror=args.mirror,
        separator_mm=args.separator_mm,
    )


def _build(args) -> Image.Image:
    job = _load_job(args.job)
    cfg = _config(args)
    images = render_job(job, cfg)
    if args.discrete or len(images) == 1:
        return images[0] if len(images) == 1 else concat_strip(images, cfg.separator_mm)
    return concat_strip(images, cfg.separator_mm)


def _ansi(img: Image.Image, scale: int) -> str:
    """Render to half-block characters: two image rows per terminal line.

    Colour codes are only emitted when the pair changes. Per-pixel escapes would
    turn a 900px strip into ~70KB of terminal output for ~450 visible columns.
    """
    if scale > 1:
        img = img.resize((max(1, img.width // scale), max(1, img.height // scale)))
    grey = img.convert("L")
    px = grey.load()
    lines = []
    for y in range(0, grey.height, 2):
        row: list[str] = []
        current: tuple[int, int] | None = None
        for x in range(grey.width):
            top = px[x, y] < 128
            bottom = px[x, y + 1] < 128 if y + 1 < grey.height else False
            # Foreground paints the upper half-block, background the lower.
            pair = (30 if top else 97, 40 if bottom else 107)
            if pair != current:
                row.append(f"\x1b[{pair[0]};{pair[1]}m")
                current = pair
            row.append("▀")
        lines.append("".join(row) + "\x1b[0m")
    return "\n".join(lines)


def cmd_render(args) -> int:
    img = _build(args)
    if args.device_orientation:
        cfg = _config(args)
        raster = to_device(img, rotation=cfg.rotation, mirror=cfg.mirror, threshold=cfg.threshold)
        img = Image.frombytes("1", (raster.width_px, raster.height_px), raster.data)
    img.save(args.output)
    print(
        f"{args.output}: {img.width}x{img.height}px "
        f"({img.width / 7.992:.1f} x {img.height / 7.992:.1f}mm)",
        file=sys.stderr,
    )
    return 0


def cmd_preview(args) -> int:
    img = _build(args)
    print(_ansi(img, args.scale))
    print(f"{img.width / 7.992:.1f}mm of tape", file=sys.stderr)
    return 0


def cmd_schema(args) -> int:
    print(json.dumps(job_schema(), indent=2))
    return 0


def _add_render_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("job", help="path to a job JSON file, or - for stdin")
    p.add_argument("--qr-base-url", default="", help="prefix codes with a short-link base")
    p.add_argument("--threshold", type=int, default=128, help="1-bit cutoff (0-255)")
    p.add_argument("--rotation", type=int, default=270, choices=[0, 90, 180, 270])
    p.add_argument("--mirror", action="store_true")
    p.add_argument("--separator-mm", type=float, default=2.0)
    p.add_argument(
        "--discrete",
        action="store_true",
        help="do not join labels into one strip (wastes a leader/trailer per label)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="labelfab", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    render = sub.add_parser("render", help="render a job to a PNG")
    _add_render_flags(render)
    render.add_argument("-o", "--output", default="label.png")
    render.add_argument(
        "--device-orientation",
        action="store_true",
        help="emit the exact 1-bit, rotated image the printer receives",
    )
    render.set_defaults(func=cmd_render)

    preview = sub.add_parser("preview", help="draw a job in the terminal")
    _add_render_flags(preview)
    preview.add_argument("--scale", type=int, default=2, help="downsample factor for width")
    preview.set_defaults(func=cmd_preview)

    schema = sub.add_parser("schema", help="print the job JSON Schema")
    schema.set_defaults(func=cmd_schema)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except Exception as exc:  # surface a clean message, not a traceback
        print(f"labelfab: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
