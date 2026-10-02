"""Composition, 1-bit conversion and packing to the printer's wire format.

Pipeline, in order, and the order matters:

    landscape canvas (L, white bg) -> invert -> threshold -> mode "1"
      -> transpose to device orientation -> tobytes()

Labels are composed **landscape** (x along the tape, y across it) because that is
the orientation a human reads and reasons about. The single rotation to device
orientation happens once, at the end.
"""

from __future__ import annotations

from dataclasses import dataclass

from PIL import Image, ImageDraw, ImageOps

from labelfab.contract import PX_PER_MM, TapeSpec, mm_to_px
from labelfab.render.layout import Rect

#: Headroom given to an auto-length label while measuring. The contract caps a
#: label at 200mm, so nothing legitimate is clipped by measuring at that size.
MAX_AUTO_LENGTH_MM = 200.0
MIN_LABEL_LENGTH_MM = 8.0

_ROTATIONS = {
    0: None,
    90: Image.Transpose.ROTATE_90,
    180: Image.Transpose.ROTATE_180,
    270: Image.Transpose.ROTATE_270,
}


@dataclass(frozen=True, slots=True)
class DeviceRaster:
    """A packed, device-oriented bitmap ready for a ``GS v 0`` frame."""

    width_px: int
    height_px: int
    data: bytes

    @property
    def width_bytes(self) -> int:
        return self.width_px // 8

    def __post_init__(self) -> None:
        if self.width_px % 8:
            raise ValueError(
                f"raster width {self.width_px}px is not a whole number of bytes; "
                "the wire format packs 8 pixels per byte with no row padding"
            )
        expected = self.width_bytes * self.height_px
        if len(self.data) != expected:
            raise ValueError(f"raster is {len(self.data)}B, expected {expected}B")

    @property
    def length_mm(self) -> float:
        return self.height_px / PX_PER_MM


def canvas(length_px: int, tape_px: int) -> Image.Image:
    """A blank landscape label: white background, black ink."""
    return Image.new("L", (max(1, length_px), max(1, tape_px)), color=255)


def compose(drawable, tape: TapeSpec, length_mm: float | str) -> Image.Image:
    """Lay a drawable tree onto a landscape canvas.

    ``length_mm == "auto"`` measures the content and sizes the label to it, which is
    the whole point of continuous tape: a short text label should not cost 40mm.
    """
    tape_px = mm_to_px(tape.width_mm)
    if length_mm == "auto":
        measured, _ = drawable.measure(mm_to_px(MAX_AUTO_LENGTH_MM), tape_px)
        length_px = max(mm_to_px(MIN_LABEL_LENGTH_MM), min(measured, mm_to_px(MAX_AUTO_LENGTH_MM)))
    else:
        length_px = mm_to_px(float(length_mm))

    img = canvas(length_px, tape_px)
    draw = ImageDraw.Draw(img)
    drawable.draw(img, draw, Rect(0, 0, img.width, img.height))
    return img


def compose_portrait(
    drawable, tape: TapeSpec, length_mm: float | str, *, flip: bool = False
) -> Image.Image:
    """Lay a drawable out upright across the tape, then turn it landscape.

    The tape width is the label's width and the length runs down it, so the content reads
    across the tape. The strip leaves the printer leading end first; by default the top
    of the label is that end, so it reads upright held with the leading end up. ``flip``
    puts the top at the other end.

    ``length_mm == "auto"`` sizes the label to its content. A fixed length shorter than
    the content is an error rather than a clipped label.
    """
    from labelfab.render.errors import LayoutOverflow

    width_px = mm_to_px(tape.width_mm)
    _, natural = drawable.measure(width_px, mm_to_px(MAX_AUTO_LENGTH_MM))
    if length_mm == "auto":
        length_px = max(1, natural)
    else:
        length_px = mm_to_px(float(length_mm))
        if natural > length_px:
            raise LayoutOverflow(
                f"vertical label needs {natural / PX_PER_MM:.1f}mm but length_mm is "
                f"{float(length_mm):.1f}; leave it at 'auto' or allow more"
            )
    img = canvas(width_px, length_px)
    drawable.draw(img, ImageDraw.Draw(img), Rect(0, 0, width_px, length_px))
    return img.transpose(Image.Transpose.ROTATE_270 if flip else Image.Transpose.ROTATE_90)


def concat_strip(
    labels: list[Image.Image], separator_mm: float = 2.0, *, bookends: bool = False
) -> Image.Image:
    """Join landscape labels end to end into a single strip.

    This is what makes strip mode worth having: one raster means one leader and one
    trailer feed for the whole batch instead of one per label. A thin tick line in
    each separator marks where to cut.

    ``bookends=True`` frames the strip with a separator at each end, so the first and
    last things the head prints are cut marks rather than a label's own margin or blank
    padding. The tape already past the head when a print starts (what the previous
    print's feed left) cannot be pulled back; cutting at the opening tick is what leaves
    it off the strip, and the closing tick marks the cut at the other end.
    """
    if not labels:
        raise ValueError("cannot build a strip from zero labels")
    if len({im.height for im in labels}) != 1:
        raise ValueError("every label in a strip must share the tape width")

    sep = mm_to_px(separator_mm)
    height = labels[0].height
    separators = len(labels) + 1 if bookends else len(labels) - 1
    total = sum(im.width for im in labels) + sep * separators

    strip = Image.new("L", (total, height), color=255)
    draw = ImageDraw.Draw(strip)

    def _separator(at: int) -> int:
        if sep >= 3:
            tick = at + sep // 2
            draw.line([(tick, 0), (tick, height - 1)], fill=0, width=1)
        return at + sep

    x = _separator(0) if bookends else 0
    for i, im in enumerate(labels):
        strip.paste(im, (x, 0))
        x += im.width
        if i < len(labels) - 1 or bookends:
            x = _separator(x)
    return strip


def to_bilevel(img: Image.Image, threshold: int = 128, dither: bool = False) -> Image.Image:
    """Invert and binarise a landscape canvas.

    Two traps live here:

    * ``ImageOps.invert`` raises on mode ``"1"``, so the convert to ``"L"`` is not
      redundant.
    * ``convert("1")`` dithers by default. Floyd-Steinberg on a QR at 2px/module
      destroys the finder patterns and the label silently stops scanning, so the
      default is an explicit threshold with dithering off.

    Inversion is needed because ``convert("1")`` maps white to bit 1, while the
    thermal raster wants bit 1 to mean *burn*, i.e. black.
    """
    grey = img.convert("L")
    inverted = ImageOps.invert(grey)
    if dither:
        return inverted.convert("1")
    return inverted.point(lambda p: 255 if p >= threshold else 0).convert("1", dither=Image.Dither.NONE)


def to_device(
    landscape: Image.Image,
    *,
    rotation: int = 270,
    mirror: bool = False,
    threshold: int = 128,
    dither: bool = False,
) -> DeviceRaster:
    """Binarise, rotate into device orientation, and pack.

    ``rotation`` and ``mirror`` are configuration rather than constants: which way
    the head scans relative to the feed is empirical, and a first print that comes
    out upside-down should be a config edit, not a code change.
    """
    if rotation not in _ROTATIONS:
        raise ValueError(f"rotation must be one of {sorted(_ROTATIONS)}, got {rotation}")

    bilevel = to_bilevel(landscape, threshold=threshold, dither=dither)
    op = _ROTATIONS[rotation]
    if op is not None:
        bilevel = bilevel.transpose(op)
    if mirror:
        bilevel = bilevel.transpose(Image.Transpose.FLIP_LEFT_RIGHT)

    # Mode "1" already packs row-major, 8px/byte, MSB first -- exactly the wire
    # format. Both tape widths are byte-aligned, so there is no row padding.
    return DeviceRaster(width_px=bilevel.width, height_px=bilevel.height, data=bilevel.tobytes())
