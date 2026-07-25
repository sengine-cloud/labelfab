"""Concrete drawables and the contract-node -> drawable factory.

Everything draws black-on-white in *landscape* orientation (x runs along the tape,
y across it). Inversion and rotation happen once, later, in ``raster``.
"""

from __future__ import annotations

import base64
import binascii
import io
from dataclasses import dataclass

import qrcode
from PIL import Image, ImageDraw, ImageFont
from qrcode.constants import (
    ERROR_CORRECT_H,
    ERROR_CORRECT_L,
    ERROR_CORRECT_M,
    ERROR_CORRECT_Q,
)

from labelfab.contract import (
    PX_PER_MM,
    BarcodeElement,
    Box,
    QrElement,
    RawPngElement,
    TextElement,
    mm_to_px,
)
from labelfab.render.errors import (
    BarcodeTooWide,
    LayoutOverflow,
    QrTooDense,
    RenderError,
)
from labelfab.render.fonts import load as load_font
from labelfab.render.fonts import pt_to_px
from labelfab.render.layout import BoxLayout, Drawable, Rect

#: Below this, module edges blur on thermal media and phone cameras stop decoding.
MIN_QR_MODULE_PX = 2

#: Human-readable line under a barcode. Small and condensed: it is a fallback for
#: a human squinting at a smudged label, not the primary carrier.
HRI_PT = 6.0
HRI_GAP_MM = 0.5

_EC = {"L": ERROR_CORRECT_L, "M": ERROR_CORRECT_M, "Q": ERROR_CORRECT_Q, "H": ERROR_CORRECT_H}


# --------------------------------------------------------------------------- #
# QR
# --------------------------------------------------------------------------- #


@dataclass
class QrDraw:
    spec: QrElement
    flex: float = 0.0

    def _modules(self) -> int:
        qr = qrcode.QRCode(
            error_correction=_EC[self.spec.ec],
            border=self.spec.quiet_zone,
            box_size=1,
        )
        qr.add_data(self.spec.value)
        qr.make(fit=True)
        return qr.modules_count + 2 * self.spec.quiet_zone

    def measure(self, avail_w: int, avail_h: int) -> tuple[int, int]:
        total = self._modules()
        box = min(avail_w, avail_h) // total
        if box < MIN_QR_MODULE_PX:
            raise QrTooDense(
                f"{len(self.spec.value)}-char payload needs {total} modules "
                f"(incl. quiet zone) but only {min(avail_w, avail_h)}px are available, "
                f"giving {box}px/module. Shorten the payload, use a short-link "
                f"redirector, or lower quiet_zone/ec."
            )
        side = box * total
        return side, side

    def draw(self, img: Image.Image, d: ImageDraw.ImageDraw, box: Rect) -> None:
        total = self._modules()
        module_px = min(box.w, box.h) // total
        if module_px < MIN_QR_MODULE_PX:
            raise QrTooDense(
                f"QR needs {total} modules in {min(box.w, box.h)}px -> {module_px}px/module"
            )
        qr = qrcode.QRCode(
            error_correction=_EC[self.spec.ec],
            border=self.spec.quiet_zone,
            box_size=module_px,
        )
        qr.add_data(self.spec.value)
        qr.make(fit=True)
        # box_size is an exact integer multiplier, so this never resamples.
        code = qr.make_image(fill_color="black", back_color="white").get_image().convert("L")
        side = code.size[0]
        img.paste(code, (box.x + (box.w - side) // 2, box.y + (box.h - side) // 2))


# --------------------------------------------------------------------------- #
# Barcode
# --------------------------------------------------------------------------- #


@dataclass
class BarcodeDraw:
    spec: BarcodeElement
    flex: float = 0.0

    def _bars(self, bar_height_mm: float) -> Image.Image:
        """Bars only. The human-readable line is drawn separately, see ``_render``."""
        import barcode
        from barcode.writer import ImageWriter

        try:
            sym = barcode.get(self.spec.symbology, self.spec.value, writer=ImageWriter())
        except Exception as exc:  # barcode raises bare Exception subclasses
            raise RenderError(f"{self.spec.symbology} cannot encode {self.spec.value!r}: {exc}") from exc
        # dpi=203 makes python-barcode's millimetre geometry land on exact device px.
        return sym.render(
            {
                "module_width": self.spec.module_width_mm,
                "module_height": max(2.0, bar_height_mm),
                "quiet_zone": 2.0,
                "write_text": False,
                "dpi": 203,
                "background": "white",
                "foreground": "black",
            }
        ).convert("L")

    def _hri_font(self):
        return load_font(condensed=True, size_px=pt_to_px(HRI_PT))

    def _hri_height(self) -> int:
        if not self.spec.hri:
            return 0
        font = self._hri_font()
        ascent, descent = font.getmetrics()
        return ascent + descent + mm_to_px(HRI_GAP_MM)

    def _render(self, height_px: int) -> Image.Image:
        """Render bars plus our own human-readable line, fitted to ``height_px``.

        python-barcode's ``write_text`` is not usable here: at 203dpi its writer
        places the text *over* the bars, and it resolves a system font by name,
        which would make output depend on what fonts the host happens to have.
        Drawing the line here keeps typography consistent with the rest of the
        label and keeps the package self-contained.
        """
        hri_px = self._hri_height()
        avail_mm = max(2.0, (height_px - hri_px) / PX_PER_MM)
        # The writer adds ~1.9mm of its own vertical margin on top of module_height.
        probe = 10.0
        margin_mm = self._bars(probe).size[1] / PX_PER_MM - probe
        bars = self._bars(avail_mm - margin_mm)

        if not self.spec.hri:
            return bars

        font = self._hri_font()
        out = Image.new("L", (bars.width, bars.height + hri_px), color=255)
        out.paste(bars, (0, 0))
        d = ImageDraw.Draw(out)
        text = self.spec.value
        d.text(
            ((out.width - font.getlength(text)) / 2, bars.height + mm_to_px(HRI_GAP_MM)),
            text,
            font=font,
            fill=0,
        )
        return out

    def measure(self, avail_w: int, avail_h: int) -> tuple[int, int]:
        if avail_h <= 0:
            return 0, 0
        rendered = self._render(avail_h)
        w, h = rendered.size
        if w > avail_w:
            raise BarcodeTooWide(
                f"{self.spec.symbology} of {self.spec.value!r} is {w / PX_PER_MM:.1f}mm wide "
                f"at the {self.spec.module_width_mm}mm narrow-bar width, but only "
                f"{avail_w / PX_PER_MM:.1f}mm are available. Shorten the payload or "
                f"lengthen the label -- narrowing the bars further would go below the "
                f"scannable floor."
            )
        return w, min(h, avail_h)

    def draw(self, img: Image.Image, d: ImageDraw.ImageDraw, box: Rect) -> None:
        rendered = self._render(box.h)
        if rendered.size[1] > box.h:
            rendered = rendered.crop((0, 0, rendered.size[0], box.h))
        img.paste(rendered, (box.x, box.y + (box.h - rendered.size[1]) // 2))


# --------------------------------------------------------------------------- #
# Text
# --------------------------------------------------------------------------- #


def _wrap(text: str, font: ImageFont.FreeTypeFont, width: int) -> list[str]:
    """Greedy wrap, breaking over-long words rather than overflowing."""
    lines: list[str] = []
    for paragraph in text.split("\n"):
        current = ""
        for word in paragraph.split():
            trial = f"{current} {word}".strip()
            if not current or font.getlength(trial) <= width:
                current = trial
                continue
            if font.getlength(word) > width:
                # A single unbreakable token; split it at the pixel boundary.
                if current:
                    lines.append(current)
                    current = ""
                for ch in word:
                    if font.getlength(current + ch) > width and current:
                        lines.append(current)
                        current = ch
                    else:
                        current += ch
            else:
                lines.append(current)
                current = word
        lines.append(current)
    return [ln for ln in lines] or [""]


def _line_height(font: ImageFont.FreeTypeFont) -> int:
    ascent, descent = font.getmetrics()
    return ascent + descent


@dataclass
class TextDraw:
    spec: TextElement
    flex: float = 1.0

    def _fit(self, width: int, height: int) -> tuple[ImageFont.FreeTypeFont, list[str]]:
        """Binary search the largest point size whose wrap fits the box."""
        lo, hi = self.spec.min_pt, self.spec.max_pt
        best: tuple[ImageFont.FreeTypeFont, list[str]] | None = None
        for _ in range(24):
            if hi - lo <= 0.2:
                break
            mid = (lo + hi) / 2
            font = load_font(bold=self.spec.bold, condensed=self.spec.condensed, size_px=pt_to_px(mid))
            lines = _wrap(self.spec.value, font, width)
            if len(lines) <= self.spec.max_lines and len(lines) * _line_height(font) <= height:
                best, lo = (font, lines), mid
            else:
                hi = mid
        if best is not None:
            return best

        # Nothing fit. Truncate at the floor rather than overflowing the label.
        font = load_font(
            bold=self.spec.bold, condensed=self.spec.condensed, size_px=pt_to_px(self.spec.min_pt)
        )
        max_lines = max(1, min(self.spec.max_lines, height // max(1, _line_height(font))))
        lines = _wrap(self.spec.value, font, width)
        if len(lines) > max_lines:
            lines = lines[:max_lines]
            while lines[-1] and font.getlength(lines[-1] + "…") > width:
                lines[-1] = lines[-1][:-1]
            lines[-1] += "…"
        if not lines or height < _line_height(font):
            raise LayoutOverflow(
                f"text {self.spec.value!r} cannot be placed in {width}x{height}px "
                f"even at {self.spec.min_pt}pt"
            )
        return font, lines

    def measure(self, avail_w: int, avail_h: int) -> tuple[int, int]:
        """Natural size: the point size the height allows, wrapped to max_lines.

        This is what gives an auto-length label a sensible length -- a flex element
        that reported zero would collapse a text-only label to nothing.
        """
        if avail_w <= 0 or avail_h <= 0:
            return 0, 0
        per_line = avail_h / self.spec.max_lines
        pt = min(self.spec.max_pt, max(self.spec.min_pt, per_line * 72.0 / 203.0 * 0.95))
        font = load_font(bold=self.spec.bold, condensed=self.spec.condensed, size_px=pt_to_px(pt))
        single = int(font.getlength(self.spec.value.replace("\n", " ")))
        # Approximate a balanced wrap across the allowed number of lines.
        natural_w = min(avail_w, max(1, -(-single // self.spec.max_lines)))
        lines = _wrap(self.spec.value, font, natural_w)
        return natural_w, min(avail_h, len(lines) * _line_height(font))

    def draw(self, img: Image.Image, d: ImageDraw.ImageDraw, box: Rect) -> None:
        if box.w <= 0 or box.h <= 0:
            return
        font, lines = self._fit(box.w, box.h)
        lh = _line_height(font)
        y = box.y + max(0, (box.h - lh * len(lines)) // 2)
        for line in lines:
            if self.spec.align == "center":
                x = box.x + int((box.w - font.getlength(line)) // 2)
            elif self.spec.align == "right":
                x = box.x + int(box.w - font.getlength(line))
            else:
                x = box.x
            d.text((x, y), line, font=font, fill=0)
            y += lh


# --------------------------------------------------------------------------- #
# Raw PNG
# --------------------------------------------------------------------------- #


@dataclass
class RawPngDraw:
    spec: RawPngElement
    flex: float = 1.0

    def _open(self) -> Image.Image:
        try:
            raw = base64.b64decode(self.spec.data_b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise RenderError(f"raw_png payload is not valid base64: {exc}") from exc
        try:
            return Image.open(io.BytesIO(raw)).convert("L")
        except OSError as exc:
            raise RenderError(f"raw_png payload is not a readable image: {exc}") from exc

    def measure(self, avail_w: int, avail_h: int) -> tuple[int, int]:
        src = self._open()
        if self.spec.fit == "stretch":
            return avail_w, avail_h
        scale = min(avail_w / src.width, avail_h / src.height)
        if self.spec.fit == "cover":
            scale = max(avail_w / src.width, avail_h / src.height)
        return max(1, int(src.width * scale)), max(1, int(src.height * scale))

    def draw(self, img: Image.Image, d: ImageDraw.ImageDraw, box: Rect) -> None:
        src = self._open()
        w, h = self.measure(box.w, box.h)
        # NEAREST for line art keeps 1-bit sources crisp; dithered photos ask for it.
        resample = Image.Resampling.LANCZOS if self.spec.dither else Image.Resampling.NEAREST
        resized = src.resize((max(1, w), max(1, h)), resample)
        if self.spec.dither:
            resized = resized.convert("1").convert("L")
        img.paste(resized, (box.x + (box.w - w) // 2, box.y + (box.h - h) // 2))


# --------------------------------------------------------------------------- #
# Factory
# --------------------------------------------------------------------------- #

_LEAVES = {
    QrElement: QrDraw,
    BarcodeElement: BarcodeDraw,
    TextElement: TextDraw,
    RawPngElement: RawPngDraw,
}


def build(node) -> Drawable:
    """Turn a validated contract node into a drawable tree."""
    if isinstance(node, Box):
        return BoxLayout(
            children=[build(c) for c in node.children],
            direction=node.direction,
            gap_px=mm_to_px(node.gap_mm),
            padding_px=mm_to_px(node.padding_mm),
            align=node.align,
            flex=node.flex,
        )
    cls = _LEAVES.get(type(node))
    if cls is None:  # pragma: no cover - the contract union makes this unreachable
        raise RenderError(f"no drawable for {type(node).__name__}")
    return cls(node, flex=node.flex)
