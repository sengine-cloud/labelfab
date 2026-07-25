"""Proof that what we print can actually be read back.

Everything here decodes the image reconstructed from the **packed device bytes**,
not the pre-raster canvas. That is the only way to catch an inverted bit order, a
row-padding off-by-one or a wrong rotation, all of which look fine in a preview and
produce an unreadable label on tape.
"""

import pytest
from PIL import Image

from labelfab.contract import BarcodeElement, LabelSpec, QrElement, TapeSpec
from labelfab.render import QrTooDense, RenderConfig, render_label, to_device

zxingcpp = pytest.importorskip("zxingcpp", reason="zxing-cpp is needed to decode")

pytestmark = pytest.mark.scan

TAPE_12 = TapeSpec(width_mm=12)
TAPE_15 = TapeSpec(width_mm=15)


def _through_the_wire(label: LabelSpec, tape: TapeSpec, cfg: RenderConfig | None = None) -> Image.Image:
    """Render, pack to device bytes, then rebuild an image from those bytes."""
    landscape = render_label(label, tape, cfg)
    raster = to_device(landscape, rotation=(cfg or RenderConfig()).rotation)
    rebuilt = Image.frombytes("1", (raster.width_px, raster.height_px), raster.data)
    # Ink is bit 1 in the wire format; invert back to black-on-white for the decoder.
    from PIL import ImageOps

    return ImageOps.invert(rebuilt.convert("L"))


def _decode(img: Image.Image) -> str | None:
    result = zxingcpp.read_barcode(img)
    return result.text if result else None


def _as_phone_camera(img: Image.Image, factor: float = 0.5) -> Image.Image:
    """Approximate a hand-held scan: resample down and back up.

    A code that only decodes at 1:1 will not survive a real phone, so this is the
    test that matters.
    """
    small = img.resize(
        (max(1, int(img.width * factor)), max(1, int(img.height * factor))),
        Image.Resampling.BILINEAR,
    )
    return small.resize(img.size, Image.Resampling.BILINEAR)


# --------------------------------------------------------------------------- #
# QR
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "payload",
    [
        "SI4821",
        "https://sngn.top/s/4821",
        "https://parts.sengine.cloud/stock/item/4821/",
    ],
)
def test_qr_survives_the_whole_pipeline_on_15mm(payload):
    label = LabelSpec(elements=[QrElement(value=payload)], length_mm=25)
    assert _decode(_through_the_wire(label, TAPE_15)) == payload


@pytest.mark.parametrize("payload", ["SI4821", "https://sngn.top/s/4821"])
def test_qr_survives_on_the_narrower_12mm_tape(payload):
    label = LabelSpec(elements=[QrElement(value=payload)], length_mm=25)
    assert _decode(_through_the_wire(label, TAPE_12)) == payload


@pytest.mark.parametrize(
    "payload",
    ["SI4821", "https://sngn.top/s/4821"],
)
def test_qr_still_decodes_at_phone_camera_resolution(payload):
    label = LabelSpec(elements=[QrElement(value=payload)], length_mm=25)
    blurred = _as_phone_camera(_through_the_wire(label, TAPE_15))
    assert _decode(blurred) == payload


#: Measured density cliff, half-resolution (phone-camera proxy) decode.
#: Everything below decodes fine at 1:1 -- the cliff is about *degraded* reads,
#: which is the case that matters in a workshop. This table is the evidence for
#: the short-link recommendation in the README; if it changes, update both.
DENSITY_CLIFF = [
    (12, "SI4821", True),
    (12, "https://sngn.top/s/4821", False),
    (12, "https://parts.sengine.cloud/stock/item/4821/", False),
    (15, "SI4821", True),
    (15, "https://sngn.top/s/4821", True),
    (15, "https://parts.sengine.cloud/stock/item/4821/", False),
]


@pytest.mark.parametrize(("tape_mm", "payload", "survives_degraded"), DENSITY_CLIFF)
def test_density_cliff_is_where_we_think_it_is(tape_mm, payload, survives_degraded):
    """Pin the payload lengths that survive a degraded scan on each tape width.

    A failure here is not necessarily a bug -- it may mean layout got more or less
    generous. Either way the README's guidance needs revisiting, which is the point.
    """
    label = LabelSpec(elements=[QrElement(value=payload)], length_mm=25)
    tape = TapeSpec(width_mm=tape_mm)

    assert _decode(_through_the_wire(label, tape)) == payload, "should always decode at 1:1"

    degraded = _decode(_as_phone_camera(_through_the_wire(label, tape)))
    assert (degraded == payload) is survives_degraded


def test_presets_produce_scannable_codes():
    cfg = RenderConfig(qr_base_url="https://sngn.top/s/")
    label = LabelSpec(
        preset="stock_item",
        vars={"code": "SI4821", "title": "M3x8 hex bolt DIN 933", "sub": "BIN-A4 . 250"},
    )
    assert _decode(_through_the_wire(label, TAPE_15, cfg)) == "https://sngn.top/s/SI4821"


def test_the_unscannable_case_raises_instead_of_printing():
    """The negative half of the contract: refuse rather than waste tape."""
    label = LabelSpec(elements=[QrElement(value="x" * 400)], length_mm=25)
    with pytest.raises(QrTooDense):
        _through_the_wire(label, TAPE_12)


# --------------------------------------------------------------------------- #
# Barcode
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("payload", "length_mm"),
    [("SI4822", 45), ("BIN-A4-01", 55)],
)
def test_code128_survives_the_whole_pipeline(payload, length_mm):
    """Code128 is dense: ~4.5mm per character at the 0.3mm narrow-bar width."""
    label = LabelSpec(elements=[BarcodeElement(value=payload)], length_mm=length_mm)
    assert _decode(_through_the_wire(label, TAPE_15)) == payload


def test_code128_still_decodes_at_phone_camera_resolution():
    label = LabelSpec(elements=[BarcodeElement(value="SI4822")], length_mm=45)
    blurred = _as_phone_camera(_through_the_wire(label, TAPE_15))
    assert _decode(blurred) == "SI4822"
