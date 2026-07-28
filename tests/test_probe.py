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
