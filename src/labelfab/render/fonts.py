"""Bundled font access.

The fonts ship inside the wheel so a label renders identically on a developer
laptop, in CI and on the workshop box. Resolving system fonts would make golden
tests machine-dependent.
"""

from __future__ import annotations

from functools import lru_cache
from importlib import resources

from PIL import ImageFont

from labelfab.contract import DPI
from labelfab.render.errors import FontUnavailable

_FACES = {
    (False, False): "DejaVuSans.ttf",
    (True, False): "DejaVuSans-Bold.ttf",
    (False, True): "DejaVuSansCondensed.ttf",
    (True, True): "DejaVuSansCondensed-Bold.ttf",
}


def pt_to_px(pt: float) -> int:
    """Typographic points to device pixels at the printer's resolution."""
    return max(1, int(round(pt * DPI / 72.0)))


def px_to_pt(px: float) -> float:
    return px * 72.0 / DPI


@lru_cache(maxsize=256)
def load(*, bold: bool = False, condensed: bool = False, size_px: int = 24) -> ImageFont.FreeTypeFont:
    """Load a bundled face at a pixel size.

    Cached because the auto-fit binary search asks for ~10 sizes per text element.
    """
    name = _FACES[(bold, condensed)]
    try:
        path = resources.files("labelfab.render._fonts") / name
        with resources.as_file(path) as p:
            return ImageFont.truetype(str(p), size=size_px)
    except (OSError, ModuleNotFoundError) as exc:  # pragma: no cover - packaging fault
        raise FontUnavailable(
            f"bundled font {name!r} is missing from the installed package; "
            "the wheel was built without its font data"
        ) from exc
