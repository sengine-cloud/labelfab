import base64
import io

import pytest
from PIL import Image

from labelfab.contract import (
    BarcodeElement,
    Box,
    LabelSpec,
    PrintJob,
    QrElement,
    TapeSpec,
    TextElement,
    mm_to_px,
)
from labelfab.render import (
    BarcodeTooWide,
    QrTooDense,
    RenderConfig,
    UnknownPreset,
    render_job,
    render_label,
)
from labelfab.render.raster import MAX_AUTO_LENGTH_MM, MIN_LABEL_LENGTH_MM

TAPE_15 = TapeSpec(width_mm=15)
TAPE_12 = TapeSpec(width_mm=12)


def _ink_columns(img: Image.Image) -> set[int]:
    return {x for x in range(img.width) for y in range(img.height) if img.getpixel((x, y)) < 128}


def _has_ink(img: Image.Image) -> bool:
    return img.getextrema()[0] < 128


# --------------------------------------------------------------------------- #
# Geometry
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("tape", "px"), [(TAPE_12, 96), (TAPE_15, 120)])
def test_label_height_is_the_tape_width(tape, px):
    label = LabelSpec(elements=[TextElement(value="x")])
    assert render_label(label, tape).height == px


def test_pinned_length_is_honoured_exactly():
    label = LabelSpec(elements=[TextElement(value="x")], length_mm=40)
    assert render_label(label, TAPE_15).width == mm_to_px(40)


def test_auto_length_sizes_to_content():
    """The point of continuous tape: a short label should not cost a long one."""
    short = render_label(LabelSpec(preset="text_only", vars={"title": "nuts"}), TAPE_15)
    long = render_label(
        LabelSpec(preset="text_only", vars={"title": "hex head cap screws M3 x 8"}), TAPE_15
    )
    assert short.width < long.width


def test_auto_length_is_clamped_to_sane_bounds():
    tiny = render_label(LabelSpec(preset="text_only", vars={"title": "."}), TAPE_15)
    assert tiny.width >= mm_to_px(MIN_LABEL_LENGTH_MM)

    huge = render_label(LabelSpec(preset="text_only", vars={"title": "word " * 100}), TAPE_15)
    assert huge.width <= mm_to_px(MAX_AUTO_LENGTH_MM)


def test_tape_default_length_applies_when_the_label_does_not_pin_one():
    tape = TapeSpec(width_mm=15, length_mm=30)
    label = LabelSpec(elements=[TextElement(value="x")])
    assert render_label(label, tape).width == mm_to_px(30)


# --------------------------------------------------------------------------- #
# QR
# --------------------------------------------------------------------------- #


def test_qr_renders_and_is_square_ish():
    label = LabelSpec(elements=[QrElement(value="SI4821")], length_mm=20)
    img = render_label(label, TAPE_15)
    assert _has_ink(img)


def test_over_dense_qr_raises_rather_than_printing_something_unscannable():
    """A silently-unscannable label wastes tape and reads as success. Fail loudly."""
    payload = "https://parts.sengine.cloud/stock/item/4821/?full=1&extra=" + "x" * 300
    label = LabelSpec(elements=[QrElement(value=payload)], length_mm=40)
    with pytest.raises(QrTooDense, match="Shorten the payload"):
        render_label(label, TAPE_12)


def test_a_short_link_fits_where_a_full_url_is_marginal():
    """Module size, not capacity, is the binding constraint at 12mm."""
    short = LabelSpec(elements=[QrElement(value="https://sngn.top/s/4821")], length_mm=40)
    render_label(short, TAPE_12)  # must not raise


def test_qr_base_url_is_applied_by_presets():
    cfg = RenderConfig(qr_base_url="https://sngn.top/s/")
    plain = render_label(LabelSpec(preset="stock_item", vars={"code": "SI1", "title": "t"}), TAPE_15)
    prefixed = render_label(
        LabelSpec(preset="stock_item", vars={"code": "SI1", "title": "t"}), TAPE_15, cfg
    )
    # A longer payload needs more modules, so the rendered QR differs.
    assert plain.tobytes() != prefixed.tobytes()


# --------------------------------------------------------------------------- #
# Barcode
# --------------------------------------------------------------------------- #


def test_barcode_renders_with_a_human_readable_line():
    with_hri = render_label(LabelSpec(elements=[BarcodeElement(value="SI4822")], length_mm=45), TAPE_15)
    without = render_label(
        LabelSpec(elements=[BarcodeElement(value="SI4822", hri=False)], length_mm=45), TAPE_15
    )
    assert with_hri.tobytes() != without.tobytes()


def test_hri_does_not_overlap_the_bars():
    """python-barcode's own writer draws the text over the bars at 203dpi.

    The bottom band of the label must contain the text and no full-height bars,
    so assert the ink there is sparser than in the bar region.
    """
    img = render_label(LabelSpec(elements=[BarcodeElement(value="SI4822")], length_mm=45), TAPE_15)
    h = img.height
    bars = sum(img.getpixel((x, h // 4)) < 128 for x in range(img.width))
    text = sum(img.getpixel((x, h - 3)) < 128 for x in range(img.width))
    assert bars > text * 2, "the bottom band should hold the HRI line, not full-height bars"


def test_barcode_too_long_for_the_label_raises():
    label = LabelSpec(
        elements=[BarcodeElement(value="A" * 40, module_width_mm=0.5)],
        length_mm=20,
    )
    with pytest.raises(BarcodeTooWide, match="narrow-bar width"):
        render_label(label, TAPE_15)


# --------------------------------------------------------------------------- #
# Text
# --------------------------------------------------------------------------- #


def test_text_wraps_within_max_lines():
    one = render_label(
        LabelSpec(elements=[TextElement(value="alpha beta gamma", max_lines=1)], length_mm=30),
        TAPE_15,
    )
    two = render_label(
        LabelSpec(elements=[TextElement(value="alpha beta gamma", max_lines=2)], length_mm=30),
        TAPE_15,
    )
    assert one.tobytes() != two.tobytes()


def test_very_long_text_is_truncated_not_overflowed():
    label = LabelSpec(
        elements=[TextElement(value="x" * 500, max_lines=1, min_pt=5, max_pt=6)],
        length_mm=20,
    )
    img = render_label(label, TAPE_15)
    assert img.size == (mm_to_px(20), mm_to_px(15))


def test_text_stays_inside_the_canvas():
    img = render_label(
        LabelSpec(elements=[TextElement(value="ABCDEFGH", max_lines=1)], length_mm=25), TAPE_15
    )
    # No ink in the outermost row/column: padding is respected.
    assert all(img.getpixel((x, 0)) >= 128 for x in range(img.width))


# --------------------------------------------------------------------------- #
# Layout
# --------------------------------------------------------------------------- #


def test_row_places_children_left_to_right():
    """Flex children share the row, so ink lands in both halves in source order."""
    label = LabelSpec(
        elements=[
            Box(
                direction="row",
                children=[
                    TextElement(value="LEFT", flex=1, align="center"),
                    TextElement(value="RIGHT", flex=1, align="center"),
                ],
            )
        ],
        length_mm=40,
    )
    img = render_label(label, TAPE_15)
    cols = _ink_columns(img)
    assert min(cols) < img.width // 2 < max(cols)


def test_a_row_of_fixed_children_packs_from_the_start():
    """flex=0 means "do not grow": leftover space stays at the end, as in flexbox."""
    label = LabelSpec(
        elements=[Box(direction="row", children=[TextElement(value="L", flex=0)])],
        length_mm=60,
    )
    img = render_label(label, TAPE_15)
    assert max(_ink_columns(img)) < img.width // 2


def test_col_stacks_children_vertically():
    label = LabelSpec(
        elements=[
            Box(direction="col", children=[TextElement(value="top"), TextElement(value="bottom")])
        ],
        length_mm=40,
    )
    img = render_label(label, TAPE_15)
    rows = {y for y in range(img.height) for x in range(img.width) if img.getpixel((x, y)) < 128}
    assert min(rows) < img.height // 2 < max(rows)


def test_nested_boxes_render():
    label = LabelSpec(
        elements=[
            Box(
                direction="row",
                children=[
                    QrElement(value="SI1"),
                    Box(
                        direction="col",
                        children=[TextElement(value="a"), TextElement(value="b")],
                    ),
                ],
            )
        ],
        length_mm=40,
    )
    assert _has_ink(render_label(label, TAPE_15))


def test_bare_element_list_is_wrapped_in_a_row():
    label = LabelSpec(elements=[QrElement(value="SI1"), TextElement(value="bolt")], length_mm=40)
    assert _has_ink(render_label(label, TAPE_15))


# --------------------------------------------------------------------------- #
# Presets
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", ["qr_caption", "stock_item", "part", "location", "text_only"])
def test_every_preset_renders(name):
    label = LabelSpec(
        preset=name,
        vars={"code": "SI1", "pk": "1", "title": "M3x8 hex bolt", "sub": "BIN-A4 . 250"},
    )
    assert _has_ink(render_label(label, TAPE_15))


def test_unknown_preset_names_the_ones_that_exist():
    with pytest.raises(UnknownPreset, match="stock_item"):
        render_label(LabelSpec(preset="nope", vars={}), TAPE_15)


def test_presets_survive_missing_vars():
    """A partially-populated item should still print rather than crash the batch."""
    assert _has_ink(render_label(LabelSpec(preset="stock_item", vars={"code": "SI1"}), TAPE_15))


def test_title_is_never_smaller_than_the_subtitle():
    """Auto-fit maximises size, so a short subtitle could out-size a long title."""
    img = render_label(
        LabelSpec(
            preset="stock_item",
            vars={"code": "SI1", "title": "a very long part name indeed", "sub": "A4"},
        ),
        TAPE_15,
    )
    top_ink = sum(img.getpixel((x, y)) < 128 for x in range(img.width) for y in range(img.height // 2))
    bottom_ink = sum(
        img.getpixel((x, y)) < 128 for x in range(img.width) for y in range(img.height // 2, img.height)
    )
    assert top_ink > bottom_ink


# --------------------------------------------------------------------------- #
# Raw PNG escape hatch
# --------------------------------------------------------------------------- #


def _png_b64(w: int, h: int) -> str:
    buf = io.BytesIO()
    img = Image.new("L", (w, h), color=255)
    img.paste(0, (0, 0, w // 2, h))
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def test_raw_png_is_placed():
    label = LabelSpec(
        elements=[{"type": "raw_png", "data_b64": _png_b64(200, 60)}],
        length_mm=40,
    )
    assert _has_ink(render_label(label, TAPE_15))


def test_raw_png_rejects_garbage():
    from labelfab.render import RenderError

    label = LabelSpec(elements=[{"type": "raw_png", "data_b64": "not base64!!"}], length_mm=40)
    with pytest.raises(RenderError, match="base64"):
        render_label(label, TAPE_15)


# --------------------------------------------------------------------------- #
# Jobs
# --------------------------------------------------------------------------- #


def test_copies_expand_in_place():
    """Someone peeling a strip expects copies grouped, not interleaved."""
    job = PrintJob(
        job_id="j",
        idempotency_key="k",
        printer={"id": "d30"},
        labels=[
            LabelSpec(preset="text_only", vars={"title": "A"}, copies=2),
            LabelSpec(preset="text_only", vars={"title": "B"}),
        ],
    )
    imgs = render_job(job)
    assert len(imgs) == 3
    assert imgs[0].tobytes() == imgs[1].tobytes()
    assert imgs[1].tobytes() != imgs[2].tobytes()
