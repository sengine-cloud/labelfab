"""Vertical labels: a QR across the whole tape, short centred lines, one orientation."""

from __future__ import annotations

import pytest
import zxingcpp
from PIL import Image

from labelfab.contract import LabelSpec, TapeSpec, mm_to_px
from labelfab.render import LayoutOverflow, QrTooDense, RenderConfig, render_label
from labelfab.render.presets import PresetContext
from labelfab.render.vertical import (
    DESC_PT,
    INV_PT,
    SIDE_PAD_PX,
    fit_line,
    scaled_qr,
    stock_item_vertical,
)

TAPE = TapeSpec(width_mm=12.0, kind="continuous", length_mm=30.0)
TEXT_W = 96 - 2 * SIDE_PAD_PX
PAYLOAD = "https://sngn.top/i/PA39"
CFG = RenderConfig(
    qr_base_url="https://sngn.top/i/",
    qr_quiet_zone=1,
    preset_aliases={"stock_item": "stock_item_vertical"},
)


def _label(**vars_: str) -> LabelSpec:
    base = {"code": "PA39", "title": "INV-PA39 · A1533", "sub": "iPhone 5s"}
    return LabelSpec(preset="stock_item", vars={**base, **vars_})


# --------------------------------------------------------------------------- #
# The QR spans the tape
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("quiet", [1, 2])
def test_the_qr_fills_the_whole_width_and_still_decodes(quiet):
    img = scaled_qr(PAYLOAD, 96, quiet)
    assert img.size == (96, 96)
    assert [r.text for r in zxingcpp.read_barcodes(img)] == [PAYLOAD]


@pytest.mark.parametrize("quiet", [1, 2])
def test_the_code_itself_is_scaled_up_not_just_centred_in_the_width(quiet):
    """A whole-pixel module size leaves 75px of code in 96 (3px x 25 modules). Scaling
    to the width is the point, so the ink must span the modules' share of it."""
    img = scaled_qr(PAYLOAD, 96, quiet)
    cols = [x for x in range(96) if any(img.getpixel((x, y)) == 0 for y in range(96))]
    ink = cols[-1] - cols[0] + 1
    modules = 25  # a 23-character payload is a version 2 code
    assert ink >= modules * 96 / (modules + 2 * quiet) - 2
    assert ink > 75, "no better than the old integer-module code"


def test_the_quiet_zone_stays_inside_the_width():
    """Running the modules to the tape edge leaves no quiet zone: the first and last
    columns have to be white."""
    img = scaled_qr(PAYLOAD, 96, 1)
    for x in (0, 95):
        assert all(img.getpixel((x, y)) == 255 for y in range(96))


def test_a_payload_too_dense_for_the_width_is_refused_not_printed_unreadable():
    with pytest.raises(QrTooDense):
        scaled_qr("x" * 400, 96, 1)


# --------------------------------------------------------------------------- #
# Width-fitting text
# --------------------------------------------------------------------------- #


def test_a_code_shrinks_to_fit_before_anything_else_happens_to_it():
    font, text = fit_line("INV-PA39", bold=True, condensed=False, width=TEXT_W, pt_range=INV_PT)
    assert text == "INV-PA39"
    assert font.getlength(text) <= TEXT_W


def test_a_description_that_cannot_fit_is_cut_with_an_ellipsis():
    font, text = fit_line(
        "Apple iPod classic 6th generation", bold=False, condensed=True, width=TEXT_W, pt_range=DESC_PT
    )
    assert text.endswith("…")
    assert "\n" not in text
    assert font.getlength(text) <= TEXT_W


def test_the_description_floor_keeps_small_letters_open_at_one_bit():
    """Below 13px the 1-bit threshold fills in the counters of o and e ("iPod" -> "iPcd")."""
    from labelfab.render.fonts import pt_to_px

    assert pt_to_px(DESC_PT[0]) >= 13


def test_a_short_description_keeps_the_larger_size():
    short, _ = fit_line("iPhone 5s", bold=False, condensed=True, width=TEXT_W, pt_range=DESC_PT)
    long_, _ = fit_line(
        "Apple iPod classic 6th generation", bold=False, condensed=True, width=TEXT_W, pt_range=DESC_PT
    )
    assert short.size > long_.size


# --------------------------------------------------------------------------- #
# The preset reads what the InvenTree plugin already sends
# --------------------------------------------------------------------------- #


def _stock(**vars_: str):
    return stock_item_vertical(vars_, PresetContext(qr_base_url="https://sngn.top/i/", qr_quiet_zone=1))


def test_the_plugins_title_is_split_into_inv_code_and_ipn():
    item = _stock(code="PA39", title="INV-PA39 · A1533", sub="iPhone 5s")
    assert (item.inv, item.ipn, item.description) == ("INV-PA39", "A1533", "iPhone 5s")
    assert item.qr_payload == PAYLOAD


def test_a_part_without_an_ipn_has_no_second_line():
    item = _stock(code="PA44", title="INV-PA44", sub="Lumia 1020")
    assert item.ipn == ""
    assert len(item._lines(96)) == 2  # INV code and description only


def test_explicit_vars_win_over_the_title():
    item = _stock(
        code="PA39", title="INV-PA39 · A1533", inv="INV-SI7", ipn="X1", description="long text"
    )
    assert (item.inv, item.ipn, item.description) == ("INV-SI7", "X1", "long text")


def test_only_the_first_line_of_the_description_is_used():
    item = _stock(code="PA39", title="INV-PA39", sub="first line\nsecond line\nthird")
    texts = [ln.text for ln in item._lines(96)]
    assert texts[-1] == "first line"  # whole, not cut: the later lines never reach the fit


# --------------------------------------------------------------------------- #
# Rendering: alias, length, orientation
# --------------------------------------------------------------------------- #


def test_the_alias_turns_stock_item_into_the_vertical_layout():
    plain = render_label(
        _label(), TAPE, RenderConfig(qr_base_url="https://sngn.top/i/", qr_quiet_zone=1)
    )
    vertical = render_label(_label(), TAPE, CFG)
    assert plain.size != vertical.size
    assert vertical.height == 96, "tape width is the label's width"


def test_a_vertical_label_is_as_long_as_its_content_not_the_pinned_tape_length():
    img = render_label(_label(), TAPE, CFG)  # TAPE pins 30mm; four stacked lines do not need it
    assert img.width != mm_to_px(30.0)
    assert img.width < mm_to_px(30.0)


def test_a_fixed_length_too_short_for_the_content_is_an_error():
    short = LabelSpec(preset="stock_item", vars=_label().vars, length_mm=10.0)
    with pytest.raises(LayoutOverflow, match="vertical label needs"):
        render_label(short, TAPE, CFG)


def test_a_fixed_length_longer_than_the_content_is_honoured():
    long = LabelSpec(preset="stock_item", vars=_label().vars, length_mm=40.0)
    assert render_label(long, TAPE, CFG).width == mm_to_px(40.0)


def test_the_top_of_the_label_is_the_leading_end_by_default():
    """Held with the leading end up, the label reads upright: the QR is at the top."""
    img = render_label(_label(), TAPE, CFG)
    upright = img.transpose(Image.Transpose.ROTATE_270)  # leading end (x = 0) to the top
    qr_area = upright.crop((0, 0, 96, 96))
    assert [r.text for r in zxingcpp.read_barcodes(qr_area)] == [PAYLOAD]


def test_flip_puts_the_top_at_the_trailing_end():
    normal = render_label(_label(), TAPE, CFG)
    flipped = render_label(
        _label(),
        TAPE,
        RenderConfig(
            qr_base_url="https://sngn.top/i/",
            qr_quiet_zone=1,
            preset_aliases={"stock_item": "stock_item_vertical"},
            vertical_flip=True,
        ),
    )
    assert flipped.tobytes() == normal.transpose(Image.Transpose.ROTATE_180).tobytes()


def test_no_blank_padding_above_the_code_or_below_the_text():
    """The cut ticks sit at the label edges, so the label itself carries no margin of
    its own beyond the QR's quiet zone."""
    upright = render_label(_label(), TAPE, CFG).transpose(Image.Transpose.ROTATE_270)
    rows = [y for y in range(upright.height) if any(upright.getpixel((x, y)) < 128 for x in range(96))]
    assert rows[0] <= 4, "the QR's own quiet zone is the only margin on top"
    assert upright.height - 1 - rows[-1] <= 4, "descenders end within a pixel or two of the edge"


def test_lines_are_stacked_tightly():
    """Stacking on ink height instead of font height is what shrinks the interval."""
    from labelfab.render.vertical import LINE_GAP_PX

    item = _stock(code="PA39", title="INV-PA39 · A1533", sub="iPhone 5s")
    lines = item._lines(96)
    _, measured = item.measure(96, 10_000)
    assert measured == 96 + sum(LINE_GAP_PX + ln.height for ln in lines)
    assert LINE_GAP_PX <= 1
    assert all(ln.height < sum(ln.font.getmetrics()) for ln in lines)  # ink box, not ascent + descent
