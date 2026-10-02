"""Vertical (portrait) labels: the content reads across the tape and stacks along it.

The head is 96 dots wide, so a vertical label is 12mm wide and exactly as long as what
is on it: a QR spanning the whole width, then short centred lines. Everything here is
drawn upright on a portrait canvas; ``raster.compose_portrait`` turns the result into
the landscape orientation (x along the tape) the rest of the pipeline works in.

These layouts are not contract trees. A producer asks for one by preset name and sends
only vars, so the contract does not grow, and the width-fitting a 12mm column needs
(shrink a code to fit before it can wrap or truncate) stays out of the generic text
element that horizontal presets depend on.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

import qrcode
from PIL import Image, ImageDraw, ImageFont
from qrcode.constants import ERROR_CORRECT_M

from labelfab.render.errors import QrTooDense
from labelfab.render.fonts import load as load_font
from labelfab.render.fonts import pt_to_px
from labelfab.render.layout import Rect
from labelfab.render.presets import PresetContext

#: Same floor as the horizontal QR: below this, module edges blur on thermal media.
MIN_QR_MODULE_PX = 2

#: Text keeps this far from each tape edge. The QR does not: it owns the full width.
SIDE_PAD_PX = 4
#: Between stacked lines. Lines are stacked on their ink height, not the font's
#: ascent + descent, so this is the whole of the visible interval.
LINE_GAP_PX = 1

INV_PT = (5.0, 11.0)  # (min, max): the INV code, bold, shrunk until it fits the width
IPN_PT = (5.0, 9.0)  # the part number, regular
#: The description: one line, shrunk to fit, then cut with an ellipsis. The floor is 4.5pt
#: (13px) because below it the printer's 1-bit threshold fills in the counters of o and e:
#: at 4pt (11px) "iPod touch" prints as "iPcd tcuch".
DESC_PT = (4.5, 5.5)

_ELLIPSIS = "…"


def scaled_qr(payload: str, width_px: int, quiet_modules: int) -> Image.Image:
    """A QR, with its quiet zone, filling exactly ``width_px`` x ``width_px``.

    At the printer's resolution a whole-pixel module size cannot fill the tape: 3px
    modules leave 75px of code in 96. So the module size is allowed to be fractional,
    each module snapping to whole pixels (3px and 4px mixed). A decoder samples module
    centres, so the uneven widths do not matter; the quiet zone does, which is why it
    stays inside the width instead of the code running to the tape edge: with no quiet
    zone, none of the degraded test cases decoded.
    """
    qr = qrcode.QRCode(border=0, box_size=1, error_correction=ERROR_CORRECT_M)
    qr.add_data(payload)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    n = len(matrix)
    total = n + 2 * quiet_modules
    module_px = width_px / total
    if module_px < MIN_QR_MODULE_PX:
        raise QrTooDense(
            f"{len(payload)}-char payload needs {total} modules (incl. quiet zone) across "
            f"{width_px}px, giving {module_px:.2f}px/module. Shorten the payload, use a "
            f"short-link redirector, or lower the quiet zone."
        )

    def edge(i: int) -> int:
        return round(i * width_px / total)

    img = Image.new("L", (width_px, width_px), color=255)
    d = ImageDraw.Draw(img)
    for r, row in enumerate(matrix):
        for c, dark in enumerate(row):
            if dark:
                x0, x1 = edge(c + quiet_modules), edge(c + quiet_modules + 1)
                y0, y1 = edge(r + quiet_modules), edge(r + quiet_modules + 1)
                d.rectangle([x0, y0, x1 - 1, y1 - 1], fill=0)
    return img


def fit_line(
    text: str, *, bold: bool, condensed: bool, width: int, pt_range: tuple[float, float]
) -> tuple[ImageFont.FreeTypeFont, str]:
    """The largest size in ``pt_range`` at which ``text`` fits on one line.

    If it does not fit even at the smallest size, it is cut and ends in an ellipsis.
    A code is never wrapped or split: ``INV-PA39`` stays one token or the size drops.
    """
    lo, hi = pt_range
    for size in range(pt_to_px(hi), pt_to_px(lo) - 1, -1):
        font = load_font(bold=bold, condensed=condensed, size_px=size)
        if font.getlength(text) <= width:
            return font, text
    font = load_font(bold=bold, condensed=condensed, size_px=pt_to_px(lo))
    while text and font.getlength(text + _ELLIPSIS) > width:
        text = text[:-1]
    return font, text.rstrip() + _ELLIPSIS


def _ink_box(font: ImageFont.FreeTypeFont) -> tuple[int, int]:
    """Top and bottom of a typical line's ink, relative to where ``text`` is drawn.

    From the top of a capital to the bottom of a descender. Using this instead of the
    font's ascent + descent drops the built-in leading, which at these sizes is most of
    the gap between lines.
    """
    top = font.getbbox("H")[1]
    bottom = font.getbbox("Hgjpqy")[3]
    return top, bottom


@dataclass
class _Line:
    text: str
    font: ImageFont.FreeTypeFont

    @property
    def top(self) -> int:
        return _ink_box(self.font)[0]

    @property
    def height(self) -> int:
        top, bottom = _ink_box(self.font)
        return bottom - top


@dataclass
class VerticalStockItem:
    """QR on top, then the INV code, the IPN (when there is one) and the description."""

    qr_payload: str
    inv: str
    ipn: str
    description: str
    quiet_zone: int
    flex: float = 0.0

    def _lines(self, width: int) -> list[_Line]:
        text_w = max(1, width - 2 * SIDE_PAD_PX)
        lines: list[_Line] = []
        if self.inv:
            font, text = fit_line(self.inv, bold=True, condensed=False, width=text_w, pt_range=INV_PT)
            lines.append(_Line(text, font))
        if self.ipn:
            font, text = fit_line(self.ipn, bold=False, condensed=False, width=text_w, pt_range=IPN_PT)
            lines.append(_Line(text, font))
        if self.description:
            # The first line of the description, no more: a 12mm column holds a dozen
            # characters, so the rest would be an unreadable paragraph.
            first = self.description.splitlines()[0] if self.description.strip() else ""
            font, text = fit_line(first, bold=False, condensed=True, width=text_w, pt_range=DESC_PT)
            lines.append(_Line(text, font))
        return lines

    def measure(self, avail_w: int, avail_h: int) -> tuple[int, int]:
        height = avail_w  # the QR is square and spans the width
        lines = self._lines(avail_w)
        height += sum(LINE_GAP_PX + ln.height for ln in lines)
        return avail_w, min(avail_h, height)

    def draw(self, img: Image.Image, d: ImageDraw.ImageDraw, box: Rect) -> None:
        code = scaled_qr(self.qr_payload, box.w, self.quiet_zone)
        img.paste(code, (box.x, box.y))
        y = box.y + code.height
        for ln in self._lines(box.w):
            y += LINE_GAP_PX
            x = box.x + int((box.w - ln.font.getlength(ln.text)) // 2)
            d.text((x, y - ln.top), ln.text, font=ln.font, fill=0)
            y += ln.height


VerticalFn = Callable[[Mapping[str, str], PresetContext], VerticalStockItem]


def _first(v: Mapping[str, str], *keys: str) -> str:
    for k in keys:
        if v.get(k):
            return v[k]
    return ""


def stock_item_vertical(v: Mapping[str, str], ctx: PresetContext) -> VerticalStockItem:
    """An InvenTree stock item or part, standing up.

    Takes explicit ``inv``, ``ipn`` and ``description`` vars, and falls back to what the
    InvenTree plugin already sends: a ``title`` of ``"INV-PA39 · A1533"`` (the code, then
    the IPN when the part has one) and a ``sub`` that is the description. A part with no
    IPN simply has no second line.
    """
    code = _first(v, "code", "pk")
    inv, ipn = _first(v, "inv"), _first(v, "ipn")
    if not inv:
        head, _, tail = _first(v, "title").partition("·")
        inv = head.strip()
        ipn = ipn or tail.strip()
    return VerticalStockItem(
        qr_payload=ctx.qr_value(code),
        inv=inv or code,
        ipn=ipn,
        description=_first(v, "description", "sub").strip(),
        quiet_zone=ctx.qr_quiet_zone,
    )


PRESETS: dict[str, VerticalFn] = {
    "stock_item_vertical": stock_item_vertical,
}
