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

# The device package is otherwise imported lazily to keep `preview` snappy, but the
# parser needs these at build time; they cost ~23ms on a ~160ms import, which is not
# enough to be worth a function-local import.
from labelfab.device.transport import DEFAULT_TRANSPORT, TRANSPORTS
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


def cmd_decode(args) -> int:
    """Turn captured printer bytes back into an image.

    This is what proves the wire format: a preview shows what was intended, this
    shows what the printer would actually receive.
    """
    from labelfab.device import decode_frames

    stream = Path(args.capture).read_bytes()
    frames = decode_frames(stream, rotation=args.rotation, mirror=args.mirror)
    if not frames:
        print("labelfab: no GS v 0 frame found in this capture", file=sys.stderr)
        return 1

    for i, frame in enumerate(frames):
        out = args.output if len(frames) == 1 else f"{Path(args.output).stem}-{i}.png"
        frame.image.save(out)
        print(
            f"{out}: frame at byte {frame.offset}, {frame.width_px}x{frame.height_px}px "
            f"({frame.width_bytes} bytes/line, {frame.height_px / 7.992:.1f}mm of tape)",
            file=sys.stderr,
        )
    if len(frames) > 1:
        print(
            f"note: {len(frames)} frames -- this batch printed discretely and paid a "
            f"leader/trailer feed per label",
            file=sys.stderr,
        )
    return 0


def _open_transport(args):
    from labelfab.device import AFBluetoothTransport, BleTransport, FakeTransport, SerialTransport

    if args.transport == "fake":
        return FakeTransport()
    if args.transport == "serial":
        return SerialTransport(port=args.port)
    if not args.mac:
        raise SystemExit(f"labelfab: --mac is required for the {args.transport} transport")
    if args.transport == "ble":
        return BleTransport(mac=args.mac, write_uuid=args.ble_write_uuid, adapter=args.adapter)
    return AFBluetoothTransport(mac=args.mac, channel=args.channel)


def _stack(raster, copies: int):
    """Repeat one raster ``copies`` times into a single frame -- a strip of clones.

    Same width means the packed rows just concatenate, so this is exactly what a real
    N-label strip looks like on the wire: one header, N labels' worth of body.
    """
    from labelfab.render.raster import DeviceRaster

    return DeviceRaster(
        width_px=raster.width_px,
        height_px=raster.height_px * copies,
        data=raster.data * copies,
    )


def cmd_probe(args) -> int:
    """Hardware bring-up. Every sub-mode answers one config constant."""
    from labelfab.device import D30Config, PhomemoD30

    # Pace sweep needs a fresh pace_factor per pass, so it manages its own printers
    # and returns before the shared one below.
    if args.pace_sweep:
        for pf in [float(x) for x in args.pace_sweep.split(",")]:
            printer = PhomemoD30(_open_transport(args), config=D30Config(pace_factor=pf))
            with printer:
                printer.print_raster(printer.self_test(args.width_px, 3200))
            print(
                f"printed a 3200-line strip at pace_factor={pf} -- the lowest value that "
                f"prints clean, plus 50%, is the setting",
                file=sys.stderr,
            )
        return 0

    printer = PhomemoD30(_open_transport(args), config=D30Config(pace_factor=args.pace_factor))
    capture = None

    with printer:
        if args.strip:
            unit = printer.self_test(args.width_px, args.length_px)
            printer.print_raster(_stack(unit, args.strip))
            print(
                f"printed {args.strip} labels as ONE strip ({args.strip * args.length_px} lines).",
                file=sys.stderr,
            )
            if args.measure_waste:
                for _ in range(args.strip):
                    printer.print_raster(unit)
                print(
                    f"then printed the same {args.strip} discretely. Measure the tape each "
                    f"run used: the difference is the per-label leader/trailer, i.e. "
                    f"separator_mm and the whole case for strip mode.",
                    file=sys.stderr,
                )
        elif args.self_test:
            raster = printer.self_test(args.width_px, args.length_px)
            printer.print_raster(raster)
            print(
                f"printed a {args.width_px}x{args.length_px} alignment pattern. Check: "
                f"is the border complete on all four sides (head width), is the solid "
                f"block top-left (rotation/mirror), are the tick rules evenly spaced?",
                file=sys.stderr,
            )
        elif args.width_sweep:
            for width in [int(w) for w in args.width_sweep.split(",")]:
                if width % 8:
                    print(f"skipping {width}px: not byte-aligned", file=sys.stderr)
                    continue
                printer.print_raster(printer.self_test(width, 120))
                print(f"printed at {width}px ({width / 7.992:.1f}mm)", file=sys.stderr)
        elif args.length_sweep:
            for lines in [int(n) for n in args.length_sweep.split(",")]:
                printer.print_raster(printer.self_test(args.width_px, lines))
                print(f"printed {lines} lines ({lines / 7.992:.0f}mm)", file=sys.stderr)
        elif args.head_width:
            import time as _time

            from labelfab.device.protocol import LABEL_WIDTH

            # Free first: a read-only query that costs no tape. Untested -- it is in
            # the vendor tables and no vendor app sends it, so we do not know which
            # tag it answers with (or whether it answers at all). Report whatever
            # arrives rather than looking up a name we are only guessing.
            before = len(printer.feedback.frames)
            printer.transport.write(LABEL_WIDTH())
            printer.transport.flush()
            _time.sleep(0.5)
            new = printer.feedback.frames[before:]
            if new:
                print(
                    "LABEL_WIDTH answered: " + ", ".join(f"{f} (tag 0x{f.tag:02x})" for f in new),
                    file=sys.stderr,
                )
            else:
                print(
                    "LABEL_WIDTH returned nothing within 500ms -- falling back to the "
                    "staircase. (Expected: no vendor app sends this opcode.)",
                    file=sys.stderr,
                )
            if printer.feedback.unknown_tags:
                print(
                    f"undecoded tags seen: "
                    f"{ {hex(t): n for t, n in printer.feedback.unknown_tags.items()} }",
                    file=sys.stderr,
                )

            raster = printer.head_width_probe(args.width_px)
            printer.print_raster(raster)
            print(
                f"printed a {raster.width_px}px staircase, {raster.width_px // 8} steps.\n"
                f"Note: We have definitively verified the D30 head is 96 dots (12mm) max.\n"
                f"If you passed >96, this will have failed with print_cancelled (0x0B).",
                file=sys.stderr,
            )
        else:
            print(
                "nothing to do; pass --self-test, --head-width, --width-sweep or --length-sweep",
                file=sys.stderr,
            )
            return 2

        if args.transport == "fake":
            capture = bytes(printer.transport.buf)

    if capture is not None and args.capture_to:
        Path(args.capture_to).write_bytes(capture)
        print(f"captured {len(capture)}B to {args.capture_to}", file=sys.stderr)
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

    dec = sub.add_parser(
        "decode",
        help="rebuild an image from captured printer bytes",
        description="Proves the wire format: what the printer receives, not what "
        "was intended. Catches inverted bits, row padding and wrong rotation.",
    )
    dec.add_argument("capture", help="file of raw bytes sent to the printer")
    dec.add_argument("-o", "--output", default="decoded.png")
    dec.add_argument("--rotation", type=int, default=270, choices=[0, 90, 180, 270])
    dec.add_argument("--mirror", action="store_true")
    dec.set_defaults(func=cmd_decode)

    probe = sub.add_parser("probe", help="hardware bring-up patterns")
    probe.add_argument("--transport", default=DEFAULT_TRANSPORT, choices=list(TRANSPORTS))
    probe.add_argument("--mac", help="printer Bluetooth address")
    probe.add_argument("--channel", type=int, default=1, help="RFCOMM channel (afbluetooth)")
    probe.add_argument("--port", default="/dev/rfcomm0", help="for --transport serial")
    from labelfab.device.ble import DEFAULT_WRITE_UUID

    probe.add_argument(
        "--ble-write-uuid", default=DEFAULT_WRITE_UUID, help="GATT write characteristic (ble)"
    )
    probe.add_argument("--adapter", default=None, help="Bluetooth adapter, e.g. hci1 (ble)")
    probe.add_argument("--width-px", type=int, default=96, help="96 for 12mm and 15mm tapes")
    probe.add_argument("--length-px", type=int, default=320)
    probe.add_argument("--pace-factor", type=float, default=1.2)
    probe.add_argument("--capture-to", help="write the byte stream here (fake transport)")
    probe.add_argument(
        "--self-test",
        action="store_true",
        help="border + rules: answers head width, offset, rotation and mirror at once",
    )
    probe.add_argument(
        "--head-width",
        action="store_true",
        help="measure the head: a self-reading staircase, one step per byte. "
        "Verified to be 96 dots (12mm) max.",
    )
    probe.add_argument("--width-sweep", help="comma-separated pixel widths, e.g. 80,88,96")
    probe.add_argument("--length-sweep", help="comma-separated line counts, e.g. 320,1600,6400")
    probe.add_argument(
        "--pace-sweep",
        help="comma-separated pace_factors, e.g. 1.2,1.0,0.8,0.6; finds where a long strip garbles",
    )
    probe.add_argument(
        "--strip",
        type=int,
        help="print N alignment labels as one strip; with --measure-waste, also N discretely",
    )
    probe.add_argument("--measure-waste", action="store_true", help="pairs with --strip")
    probe.set_defaults(func=cmd_probe)

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
