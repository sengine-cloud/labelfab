"""Wire format for the Phomemo D30.

The opcode table now lives in :mod:`labelfab.device.protocol`; this module is the
print-path view of it, kept as the import site the rest of the package already uses.

The externally-verified fact remains the print header captured from the vendor app:

    1f1124001b401d7630000c004001

What changed is that it is no longer opaque. It decodes as:

    1f 11 24 00   LEFT_MARGIN = 0
    1b 40         ESC @ (init)
    1d 76 30 00   GS v 0
    0c 00         12 bytes per line = 96 dots
    40 01         320 lines

The ``00`` is *computed* -- ``head_width_bytes - width_bytes`` -- and is 0 only
because that capture was a full-width label. Hardcoding it, which this module used to
do, puts every narrower label hard against one edge. The pinned string is unchanged
for 12-byte rasters, which is why the regression test still passes.
"""

from __future__ import annotations

from labelfab.device.protocol import (
    DENSITIES,
    DENSITY_HEAVY,
    DENSITY_LIGHT,
    DENSITY_MEDIUM,
    HEAD_WIDTH_BYTES,
    INIT_PRINTER,
    LEFT_MARGIN,
    MAX_FRAME_BYTES_PER_LINE,
    MAX_FRAME_LINES,
    PRINT_IMAGE,
    left_margin_bytes,
    print_preamble,
    raster_header,
    session_setup,
)

#: ``LEFT_MARGIN`` with a zero argument. Retained under its historical name because
#: it is what the pinned capture shows; prefer ``LEFT_MARGIN(n)`` for anything real.
VENDOR_PREFIX = LEFT_MARGIN(0)

#: ESC @ -- the standard ESC/POS "initialise printer" command.
ESC_INIT = INIT_PRINTER.opcode

#: GS v 0, mode 0: print a raster bit image, normal size.
GS_V0 = PRINT_IMAGE.opcode

#: Connect-time sequence. Historically a fixed 7-tuple written one packet at a time
#: because the printer was believed "picky about framing"; the captures show the
#: Android app coalescing four queries into a single write and the printer answering
#: all four in order, so that belief is unsupported. Density is now a parameter
#: rather than being baked into the last packet as a fixed ``02``.
INIT_PACKETS: tuple[bytes, ...] = session_setup(density=DENSITY_MEDIUM)

__all__ = [
    "DENSITIES",
    "DENSITY_HEAVY",
    "DENSITY_LIGHT",
    "DENSITY_MEDIUM",
    "ESC_INIT",
    "GS_V0",
    "HEAD_WIDTH_BYTES",
    "INIT_PACKETS",
    "MAX_FRAME_BYTES_PER_LINE",
    "MAX_FRAME_LINES",
    "VENDOR_PREFIX",
    "left_margin_bytes",
    "print_header",
    "print_preamble",
    "raster_header",
    "session_setup",
]


def print_header(
    width_bytes: int,
    height_px: int,
    *,
    head_width_bytes: int = HEAD_WIDTH_BYTES,
) -> bytes:
    """Build the frame header for a raster ``width_bytes`` wide, ``height_px`` tall.

    Emits ``LEFT_MARGIN``, ``ESC @`` and the ``GS v 0`` header, matching the captured
    Android sequence byte for byte at full width. The margin is computed, so a
    narrower raster is letterboxed the way the vendor does it rather than pinned to
    the left edge.
    """
    return (
        LEFT_MARGIN(left_margin_bytes(width_bytes, head_width_bytes))
        + ESC_INIT
        + raster_header(width_bytes, height_px)
    )
