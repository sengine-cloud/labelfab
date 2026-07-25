"""Wire format for the Phomemo D30.

Everything here is reverse-engineered. The one externally-verified fact is the
print header emitted by ``polskafan/phomemo_d30``, which was captured from the
Android "Print Master" app:

    1f1124001b401d7630000c004001

That string is pinned in the tests and should never change meaning. Do not
"clean up" the vendor prefix; it is opaque and empirically required.
"""

from __future__ import annotations

#: Vendor framing. ``24`` looks like an opcode with ``00`` as its argument, but
#: nothing is known for certain. It precedes every print job.
VENDOR_PREFIX = bytes.fromhex("1f112400")

#: ESC @ -- the standard ESC/POS "initialise printer" command.
ESC_INIT = b"\x1b\x40"

#: GS v 0, mode 0: print a raster bit image, normal size.
GS_V0 = b"\x1d\x76\x30\x00"

#: Session setup, captured from the Android app. Each packet is written and
#: flushed separately; the printer is picky about framing, so they are not
#: concatenated into one write.
INIT_PACKETS: tuple[bytes, ...] = tuple(
    bytes.fromhex(h)
    for h in (
        "1f1138",
        "1f11121f1113",
        "1f1109",
        "1f1111",
        "1f1119",
        "1f1107",
        "1f110a1f110202",
    )
)

#: ``yL/yH`` is a 16-bit little-endian line count, so one frame tops out here.
#: At 203dpi that is 8.2m -- longer than a 20ft (6.1m) roll, which is what makes
#: whole-batch strip printing possible in a single frame.
MAX_FRAME_LINES = 0xFFFF
MAX_FRAME_BYTES_PER_LINE = 0xFFFF


def print_header(width_bytes: int, height_px: int) -> bytes:
    """Build the frame header for a raster ``width_bytes`` wide, ``height_px`` tall."""
    if not 0 < width_bytes <= MAX_FRAME_BYTES_PER_LINE:
        raise ValueError(f"width_bytes {width_bytes} out of range")
    if not 0 < height_px <= MAX_FRAME_LINES:
        raise ValueError(
            f"{height_px} lines exceeds the 16-bit yL/yH field (max {MAX_FRAME_LINES}, "
            f"about {MAX_FRAME_LINES / 7.992:.0f}mm); split the strip"
        )
    return (
        VENDOR_PREFIX
        + ESC_INIT
        + GS_V0
        + width_bytes.to_bytes(2, "little")
        + height_px.to_bytes(2, "little")
    )
