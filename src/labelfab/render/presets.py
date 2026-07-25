"""Named label layouts.

Presets are Python functions, not a config format. A producer sends
``{"preset": "stock_item", "vars": {...}}`` and layout ownership stays here, so
moving the QR is a small pull request rather than a redeploy of every producer.

A preset receives the job's ``vars`` and returns a contract ``Box``. Missing keys
resolve to an empty string so a partially-populated item still prints.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from labelfab.contract import Box, QrElement, TextElement
from labelfab.render.errors import UnknownPreset

PresetFn = Callable[[Mapping[str, str], "PresetContext"], Box]


class PresetContext:
    """Render-side configuration a preset may consult."""

    def __init__(self, qr_base_url: str = "") -> None:
        self.qr_base_url = qr_base_url

    def qr_value(self, code: str) -> str:
        """Prefer a short redirector: QR module size, not capacity, is the limit.

        At 12mm the tape allows about 2 device pixels per module, so a long URL
        prints at the very floor of what a phone camera resolves. A short link
        buys back several module sizes.
        """
        return f"{self.qr_base_url}{code}" if self.qr_base_url else code


def _g(v: Mapping[str, str], *keys: str) -> str:
    for k in keys:
        if v.get(k):
            return v[k]
    return ""


#: Caps that keep the visual hierarchy stable. Auto-fit maximises point size, so a
#: short secondary string would otherwise out-size a long primary one and the label
#: would read upside-down in importance.
_TITLE_MAX_PT = 16.0
_SUB_MAX_PT = 8.0


def qr_caption(v: Mapping[str, str], ctx: PresetContext) -> Box:
    """QR on the left, one or two lines of text filling the rest."""
    return Box(
        direction="row",
        gap_mm=1.0,
        children=[
            QrElement(value=ctx.qr_value(_g(v, "code", "pk"))),
            Box(
                direction="col",
                align="start",
                gap_mm=0.3,
                padding_mm=0.0,
                children=[
                    TextElement(
                        value=_g(v, "title"),
                        max_lines=2,
                        bold=True,
                        max_pt=_TITLE_MAX_PT,
                        flex=2.0,
                    ),
                    TextElement(
                        value=_g(v, "sub"),
                        max_lines=1,
                        condensed=True,
                        max_pt=_SUB_MAX_PT,
                        flex=1.0,
                    ),
                ],
            ),
        ],
    )


def stock_item(v: Mapping[str, str], ctx: PresetContext) -> Box:
    """An InvenTree stock item: QR, part name, then location and quantity."""
    return qr_caption(v, ctx)


def part(v: Mapping[str, str], ctx: PresetContext) -> Box:
    """A part definition: QR, full name, then IPN or description."""
    return qr_caption(v, ctx)


def location(v: Mapping[str, str], ctx: PresetContext) -> Box:
    """A storage location: big name, small path, QR on the right for scanning in."""
    return Box(
        direction="row",
        gap_mm=1.0,
        children=[
            Box(
                direction="col",
                align="start",
                gap_mm=0.3,
                padding_mm=0.0,
                children=[
                    TextElement(
                        value=_g(v, "title"), max_lines=1, bold=True, max_pt=_TITLE_MAX_PT, flex=2.0
                    ),
                    TextElement(
                        value=_g(v, "sub"),
                        max_lines=2,
                        condensed=True,
                        max_pt=_SUB_MAX_PT,
                        flex=1.0,
                    ),
                ],
            ),
            QrElement(value=ctx.qr_value(_g(v, "code", "pk"))),
        ],
    )


def text_only(v: Mapping[str, str], ctx: PresetContext) -> Box:
    """No code, just words. The cheapest label in tape terms."""
    return Box(
        direction="col",
        gap_mm=0.3,
        children=[
            TextElement(value=_g(v, "title"), max_lines=2, bold=True, align="center"),
        ],
    )


PRESETS: dict[str, PresetFn] = {
    "qr_caption": qr_caption,
    "stock_item": stock_item,
    "part": part,
    "location": location,
    "text_only": text_only,
}


def get(name: str) -> PresetFn:
    try:
        return PRESETS[name]
    except KeyError:
        raise UnknownPreset(f"unknown preset {name!r}; this agent has {sorted(PRESETS)}") from None
