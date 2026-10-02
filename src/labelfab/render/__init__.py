"""Turn a validated job into printable bitmaps."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from PIL import Image

from labelfab.contract import Box, LabelSpec, PrintJob, TapeSpec
from labelfab.render import presets as _presets
from labelfab.render import vertical as _vertical
from labelfab.render.elements import build
from labelfab.render.errors import (
    BarcodeTooWide,
    FontUnavailable,
    LayoutOverflow,
    QrTooDense,
    RenderError,
    UnknownPreset,
)
from labelfab.render.raster import (
    DeviceRaster,
    compose,
    compose_portrait,
    concat_strip,
    to_bilevel,
    to_device,
)

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
    "compose_portrait",
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
    qr_quiet_zone: int = 4
    threshold: int = 128
    rotation: int = 270
    mirror: bool = False
    separator_mm: float = 2.0
    #: Preset renames applied before lookup, so a producer that always asks for
    #: ``stock_item`` can be given the vertical layout from the agent's config alone.
    preset_aliases: Mapping[str, str] = field(default_factory=dict)
    #: Put the top of a vertical label at the trailing end of the strip instead of the leading one.
    vertical_flip: bool = False


def _preset(label: LabelSpec, cfg: RenderConfig) -> str | None:
    """The preset to render, after the agent's aliases."""
    if label.preset is None:
        return None
    return cfg.preset_aliases.get(label.preset, label.preset)


def _context(cfg: RenderConfig) -> _presets.PresetContext:
    return _presets.PresetContext(qr_base_url=cfg.qr_base_url, qr_quiet_zone=cfg.qr_quiet_zone)


def _tree(label: LabelSpec, cfg: RenderConfig) -> Box:
    name = _preset(label, cfg)
    if name is not None:
        return _presets.get(name)(label.vars, _context(cfg))
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
    name = _preset(label, cfg)
    if name in _vertical.PRESETS:
        # A vertical label is as long as its content. The agent's pinned tape length is
        # the horizontal layouts' fixed pitch and would not fit four stacked lines.
        drawable = _vertical.PRESETS[name](label.vars, _context(cfg))
        return compose_portrait(drawable, tape, label.length_mm, flip=cfg.vertical_flip)
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
