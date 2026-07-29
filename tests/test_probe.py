"""Probe bring-up helpers: a strip of clones is still one frame."""

from __future__ import annotations

from labelfab.cli import _stack
from labelfab.device.d30 import PhomemoD30
from labelfab.device.escpos import GS_V0
from labelfab.device.transport import FakeTransport


def test_stack_is_one_frame_of_n_labels():
    printer = PhomemoD30(FakeTransport(), sleep=lambda _s: None)
    unit = printer.self_test(96, 200)
    strip = _stack(unit, 5)
    assert strip.height_px == 200 * 5
    assert strip.data == unit.data * 5

    with printer:
        printer.print_raster(strip)
    assert bytes(printer.transport.buf).count(GS_V0) == 1  # one header for the whole strip


# --------------------------------------------------------------------------- #
# Head-width probe. A width sweep cannot settle 96-vs-120 because the failure is
# silent, so this pattern has to be self-reading.
# --------------------------------------------------------------------------- #


def _bits(raster):
    w = raster.width_px
    return lambda x, y: (raster.data[y * (w // 8) + x // 8] >> (7 - (x % 8))) & 1


def test_staircase_has_one_step_per_byte_of_width():
    from labelfab.device import FakeTransport, PhomemoD30

    r = PhomemoD30(FakeTransport()).head_width_probe(120)
    assert r.width_px == 120
    bit = _bits(r)
    # Column i is inked at y=0; count distinct 8px columns that start filled.
    starts = [x for x in range(0, r.width_px, 8) if bit(x, 0)]
    assert len(starts) == r.width_px // 8 == 15


def test_step_height_encodes_column_index():
    """So a truncated print says *which* end was cut, not just how much."""
    from labelfab.device import FakeTransport, PhomemoD30

    r = PhomemoD30(FakeTransport()).head_width_probe(120, step_px=8)
    bit = _bits(r)
    heights = []
    for i in range(r.width_px // 8):
        x = i * 8
        heights.append(sum(1 for y in range(r.height_px) if bit(x, y)))
    assert heights == sorted(heights), "steps must increase monotonically"
    assert len(set(heights)) == len(heights), "every step must be distinguishable"


def test_bottom_bar_spans_the_full_width():
    """Straight = clean truncation, diagonal = bytes-per-line mismatch."""
    from labelfab.device import FakeTransport, PhomemoD30

    r = PhomemoD30(FakeTransport()).head_width_probe(120)
    bit = _bits(r)
    assert all(bit(x, r.height_px - 1) for x in range(r.width_px))


def test_probe_width_must_be_byte_aligned():
    import pytest

    from labelfab.device import D30GeometryError, FakeTransport, PhomemoD30

    with pytest.raises(D30GeometryError, match="not a whole number of bytes"):
        PhomemoD30(FakeTransport()).head_width_probe(100)
