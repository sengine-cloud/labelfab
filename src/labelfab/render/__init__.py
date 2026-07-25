"""Turn a validated job into printable bitmaps."""

from __future__ import annotations

from dataclasses import dataclass

from PIL import Image

from labelfab.contract import Box, LabelSpec, PrintJob, TapeSpec
from labelfab.render import presets as _presets
from labelfab.render.elements import build
from labelfab.render.errors import (
    BarcodeTooWide,
    FontUnavailable,
    LayoutOverflow,
    QrTooDense,
    RenderError,
    UnknownPreset,
)
from labelfab.render.raster import DeviceRaster, compose, concat_strip, to_bilevel, to_device

__all__ = [
    "BarcodeTooWide",
    "DeviceRaster",
    "FontUnavailable",
    "LayoutOverflow",
    "QrTooDense",
    "RenderConfig",
    "RenderError",
    "UnknownPreset",
    "compose",
    "concat_strip",
    "rasterise",
    "render_job",
    "render_label",
    "to_bilevel",
    "to_device",
]


@dataclass(frozen=True, slots=True)
class RenderConfig:
    """Render-side knobs. Everything here is hardware- or taste-dependent."""

    qr_base_url: str = ""
    threshold: int = 128
    rotation: int = 270
    mirror: bool = False
    separator_mm: float = 2.0


def _tree(label: LabelSpec, cfg: RenderConfig) -> Box:
    if label.preset is not None:
        ctx = _presets.PresetContext(qr_base_url=cfg.qr_base_url)
        return _presets.get(label.preset)(label.vars, ctx)
    children = list(label.elements or [])
    if len(children) == 1 and isinstance(children[0], Box):
        return children[0]
    return Box(direction="row", children=children)


def render_label(
    label: LabelSpec,
    tape: TapeSpec,
    cfg: RenderConfig | None = None,
) -> Image.Image:
    """Render one label to a landscape greyscale image (x along the tape)."""
    cfg = cfg or RenderConfig()
    length = label.length_mm if label.length_mm != "auto" else tape.length_mm
    return compose(build(_tree(label, cfg)), tape, length)


def render_job(job: PrintJob, cfg: RenderConfig | None = None) -> list[Image.Image]:
    """Render every label of a job, expanding copies in place.

    Copies stay adjacent because someone peeling a strip expects them grouped;
    interleaving would make a run of five identical bin labels unusable.
    """
    cfg = cfg or RenderConfig()
    out: list[Image.Image] = []
    for label in job.labels:
        img = render_label(label, job.tape, cfg)
        out.extend([img] * label.copies)
    return out


def rasterise(images: list[Image.Image], cfg: RenderConfig | None = None) -> list[DeviceRaster]:
    """Pack landscape images into device-oriented frames, one per image."""
    cfg = cfg or RenderConfig()
    return [
        to_device(im, rotation=cfg.rotation, mirror=cfg.mirror, threshold=cfg.threshold) for im in images
    ]
