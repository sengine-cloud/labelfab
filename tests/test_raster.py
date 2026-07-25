import pytest
from PIL import Image, ImageDraw

from labelfab.contract import TapeSpec, mm_to_px
from labelfab.render.raster import DeviceRaster, concat_strip, to_bilevel, to_device


def _canvas(w: int, h: int) -> Image.Image:
    return Image.new("L", (w, h), color=255)


# --------------------------------------------------------------------------- #
# Packing. `Image.tobytes()` on mode "1" is assumed to be the wire format, so
# pin it against a hand-computed bitmap.
# --------------------------------------------------------------------------- #


def test_packing_is_msb_first_row_major():
    img = _canvas(16, 2)
    px = img.load()
    for x in range(0, 16, 2):
        px[x, 0] = 0  # ink on even columns of row 0
    for x in range(1, 16, 2):
        px[x, 1] = 0  # ink on odd columns of row 1

    raster = to_device(img, rotation=0)

    assert (raster.width_px, raster.height_px) == (16, 2)
    assert raster.width_bytes == 2
    # Ink -> bit 1, most significant bit is the leftmost pixel.
    assert raster.data.hex() == "aaaa5555"


def test_ink_becomes_bit_one():
    """The thermal raster burns on 1, but PIL's convert("1") maps white to 1."""
    black = Image.new("L", (8, 1), color=0)
    white = Image.new("L", (8, 1), color=255)
    assert to_device(black, rotation=0).data == b"\xff"
    assert to_device(white, rotation=0).data == b"\x00"


def test_bilevel_rejects_nothing_and_accepts_mode_1_input():
    """ImageOps.invert raises on mode "1"; to_bilevel must convert to L first."""
    already_binary = Image.new("1", (8, 1), color=1)
    assert to_bilevel(already_binary).size == (8, 1)


def test_no_dithering_by_default():
    """Floyd-Steinberg on QR modules destroys the finder patterns.

    A flat mid-grey must come out uniformly one colour, not speckled.
    """
    grey = Image.new("L", (64, 8), color=200)
    assert len(to_bilevel(grey).getcolors()) == 1


def test_dithering_is_available_when_asked_for():
    grey = Image.new("L", (64, 8), color=128)
    assert len(to_bilevel(grey, dither=True).getcolors()) > 1


# --------------------------------------------------------------------------- #
# Orientation
# --------------------------------------------------------------------------- #


def test_rotation_swaps_the_axes():
    landscape = _canvas(320, 96)
    raster = to_device(landscape, rotation=270)
    assert (raster.width_px, raster.height_px) == (96, 320)


def test_default_rotation_prints_the_leftmost_label_first():
    """Under the default 270, landscape x=0 must map to the first line printed."""
    img = _canvas(16, 8)
    ImageDraw.Draw(img).rectangle([0, 0, 1, 7], fill=0)  # ink at the left edge only

    raster = to_device(img, rotation=270)

    first_row = raster.data[: raster.width_bytes]
    last_row = raster.data[-raster.width_bytes :]
    assert first_row == b"\xff", "leftmost landscape column should be the first printed line"
    assert last_row == b"\x00"


def test_mirror_flips_across_the_tape():
    img = _canvas(16, 8)
    ImageDraw.Draw(img).rectangle([0, 0, 1, 3], fill=0)
    plain = to_device(img, rotation=270)
    mirrored = to_device(img, rotation=270, mirror=True)
    assert plain.data != mirrored.data


@pytest.mark.parametrize("rotation", [1, 45, 360, -90])
def test_invalid_rotation_is_rejected(rotation):
    with pytest.raises(ValueError, match="rotation must be one of"):
        to_device(_canvas(8, 8), rotation=rotation)


# --------------------------------------------------------------------------- #
# DeviceRaster invariants
# --------------------------------------------------------------------------- #


def test_raster_width_must_be_byte_aligned():
    with pytest.raises(ValueError, match="not a whole number of bytes"):
        DeviceRaster(width_px=100, height_px=1, data=b"\x00" * 13)


def test_raster_length_is_checked():
    with pytest.raises(ValueError, match="expected"):
        DeviceRaster(width_px=96, height_px=10, data=b"\x00")


@pytest.mark.parametrize("width_mm", [12, 15])
def test_both_tape_widths_pack_without_row_padding(width_mm):
    tape_px = mm_to_px(width_mm)
    raster = to_device(_canvas(40, tape_px), rotation=270)
    assert len(raster.data) == raster.width_bytes * raster.height_px


# --------------------------------------------------------------------------- #
# Strips
# --------------------------------------------------------------------------- #


def test_strip_length_is_the_sum_of_labels_plus_separators():
    labels = [_canvas(100, 120), _canvas(50, 120), _canvas(75, 120)]
    sep_mm = 2.0
    strip = concat_strip(labels, sep_mm)
    assert strip.width == 100 + 50 + 75 + mm_to_px(sep_mm) * 2
    assert strip.height == 120


def test_strip_draws_a_cut_tick_between_labels_but_not_at_the_ends():
    strip = concat_strip([_canvas(40, 32), _canvas(40, 32)], 2.0)
    columns_with_ink = {
        x for x in range(strip.width) if any(strip.getpixel((x, y)) == 0 for y in range(32))
    }
    assert len(columns_with_ink) == 1
    assert 40 < next(iter(columns_with_ink)) < 40 + mm_to_px(2.0)


def test_strip_refuses_mismatched_tape_widths():
    with pytest.raises(ValueError, match="share the tape width"):
        concat_strip([_canvas(40, 96), _canvas(40, 120)])


def test_strip_refuses_to_be_empty():
    with pytest.raises(ValueError, match="zero labels"):
        concat_strip([])


def test_a_strip_is_one_frame_not_many():
    """The entire point of strip mode: N labels cost one leader/trailer, not N."""
    labels = [_canvas(mm_to_px(40), mm_to_px(15)) for _ in range(20)]
    raster = to_device(concat_strip(labels, 2.0))
    assert raster.width_px == mm_to_px(15)
    assert raster.height_px == 20 * mm_to_px(40) + 19 * mm_to_px(2)
    # A 16-bit height field caps a frame at 65535 lines; a full roll is well under.
    assert raster.height_px < 65535


def test_a_full_roll_still_fits_one_frame():
    """20ft of 15mm tape is ~6.1m; the yL/yH field allows 8.2m."""
    assert mm_to_px(6100) < 65535


class TestTapeGeometry:
    def test_defaults_are_fifteen_mil(self):
        assert TapeSpec().width_mm == 15.0

    @pytest.mark.parametrize(("mm", "width_bytes"), [(12, 12), (15, 15)])
    def test_width_bytes_match_the_protocol_field(self, mm, width_bytes):
        raster = to_device(_canvas(40, mm_to_px(mm)), rotation=270)
        assert raster.width_bytes == width_bytes
