"""Versioned wire contract between label producers and the print agent.

Everything a producer needs to know lives in this module. The JSON Schema in
``contracts/job-v1.schema.json`` is generated from these models and diffed in CI,
so a change here is a deliberate, visible cross-repo contract change.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

CONTRACT_VERSION = 1

#: Device resolution. Every millimetre in this contract is converted with this.
DPI = 203
PX_PER_MM = DPI / 25.4  # 7.9921...

#: Nominal print speed, used to pace writes and to wait out a print.
PRINT_SPEED_MM_S = 60.0


def mm_to_px(mm: float) -> int:
    """Millimetres to device pixels, rounded to nearest."""
    return int(round(mm * PX_PER_MM))


def px_to_mm(px: int) -> float:
    """Device pixels to millimetres."""
    return px / PX_PER_MM


class Base(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --------------------------------------------------------------------------- #
# Elements
# --------------------------------------------------------------------------- #

#: A label's length may be pinned in millimetres or sized to its content.
LengthMm = Annotated[float, Field(gt=0, le=200)] | Literal["auto"]


class QrElement(Base):
    type: Literal["qr"] = "qr"
    value: str = Field(min_length=1, max_length=1024)
    #: Error correction. ``M`` is the right default: ``L`` is fragile on thermal
    #: media that smudges, ``Q``/``H`` cost modules we do not have room for.
    ec: Literal["L", "M", "Q", "H"] = "M"
    #: Quiet zone in modules. The QR spec mandates 4; going below it is a real
    #: scannability risk and is only allowed because 96px tape is genuinely tight.
    quiet_zone: Annotated[int, Field(ge=1, le=8)] = 4
    flex: Annotated[float, Field(ge=0)] = 0.0


class BarcodeElement(Base):
    type: Literal["barcode"] = "barcode"
    value: str = Field(min_length=1, max_length=256)
    symbology: Literal["code128", "code39", "ean13"] = "code128"
    #: Narrow-bar width. 0.25mm == 2 device px is the practical scanner floor.
    module_width_mm: Annotated[float, Field(ge=0.25, le=1.0)] = 0.3
    #: Human-readable interpretation printed under the bars.
    hri: bool = True
    flex: Annotated[float, Field(ge=0)] = 0.0


class TextElement(Base):
    type: Literal["text"] = "text"
    value: str = Field(max_length=512)
    max_lines: Annotated[int, Field(ge=1, le=6)] = 2
    bold: bool = False
    condensed: bool = False
    align: Literal["left", "center", "right"] = "left"
    min_pt: Annotated[float, Field(ge=3, le=48)] = 5.0
    max_pt: Annotated[float, Field(ge=3, le=48)] = 28.0
    flex: Annotated[float, Field(ge=0)] = 1.0

    @model_validator(mode="after")
    def _pt_range_ordered(self) -> TextElement:
        if self.min_pt > self.max_pt:
            raise ValueError(f"min_pt ({self.min_pt}) exceeds max_pt ({self.max_pt})")
        return self


class RawPngElement(Base):
    """Escape hatch: a pre-rendered image, base64-encoded.

    This is how a producer that already owns a renderer (InvenTree's own label
    templates, say) bypasses the preset system without the agent needing to know.
    """

    type: Literal["raw_png"] = "raw_png"
    data_b64: str = Field(min_length=1)
    fit: Literal["contain", "cover", "stretch"] = "contain"
    #: Floyd-Steinberg. Only ever correct for photographs; it destroys QR modules
    #: and thin glyphs, which is why every other element type cannot enable it.
    dither: bool = False
    flex: Annotated[float, Field(ge=0)] = 1.0


Element = Annotated[
    QrElement | BarcodeElement | TextElement | RawPngElement,
    Field(discriminator="type"),
]


class Box(Base):
    """A layout container. Nestable; the leaves are Elements."""

    type: Literal["box"] = "box"
    direction: Literal["row", "col"] = "row"
    children: list[Element | Box] = Field(min_length=1)
    gap_mm: Annotated[float, Field(ge=0, le=10)] = 0.6
    padding_mm: Annotated[float, Field(ge=0, le=10)] = 0.5
    align: Literal["start", "center", "end", "stretch"] = "center"
    flex: Annotated[float, Field(ge=0)] = 1.0


Node = Annotated[Element | Box, Field(union_mode="left_to_right")]


# --------------------------------------------------------------------------- #
# Labels
# --------------------------------------------------------------------------- #


class LabelSpec(Base):
    """One physical label. Either names a preset or carries an explicit tree."""

    #: Named layout function registered in ``labelfab.render.presets``.
    preset: str | None = Field(default=None, max_length=64)
    #: Substitution values consumed by the preset.
    vars: dict[str, str] = Field(default_factory=dict)
    #: Explicit layout, used instead of a preset.
    elements: list[Node] | None = None

    copies: Annotated[int, Field(ge=1, le=50)] = 1
    length_mm: LengthMm = "auto"
    #: Suppress reprints of the same physical label across jobs. Distinct from
    #: ``idempotency_key``, which deduplicates whole job *deliveries*.
    dedupe_key: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def _exactly_one_source(self) -> LabelSpec:
        if (self.preset is None) == (self.elements is None):
            raise ValueError("a label needs exactly one of 'preset' or 'elements'")
        if self.preset is None and self.vars:
            raise ValueError("'vars' is only meaningful alongside 'preset'")
        return self


# --------------------------------------------------------------------------- #
# Job envelope
# --------------------------------------------------------------------------- #


class TapeSpec(Base):
    width_mm: Annotated[float, Field(ge=6, le=15)] = 15.0
    #: Default length when a label does not pin its own.
    length_mm: LengthMm = "auto"
    #: ``gap`` media is die-cut and the firmware aligns to the die, which forbids
    #: strip mode (a multi-label frame would print straight across the gaps).
    kind: Literal["continuous", "gap"] = "continuous"


class PrintOptions(Base):
    #: ``strip`` concatenates labels into one raster so the leader/trailer feed is
    #: paid once per batch instead of once per label. See docs/STRIPS.md.
    batch_mode: Literal["strip", "discrete"] = "strip"
    #: Bypass the coalescing window and print as soon as this job is rendered.
    flush: bool = False
    on_error: Literal["continue", "abort"] = "continue"
    deadline_s: Annotated[int, Field(ge=30, le=86400)] = 900


class PrinterRef(Base):
    id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]*$")
    #: Refuse the job if the agent drives a different model.
    require_model: str | None = Field(default=None, max_length=64)


class PrintJob(Base):
    v: Literal[1] = 1
    job_id: str = Field(min_length=1, max_length=64)
    #: Stable across redeliveries of the *same* job, distinct for every deliberate
    #: reprint. Producers should embed the job's own ULID, not just the item id.
    idempotency_key: str = Field(min_length=1, max_length=256)
    printer: PrinterRef
    tape: TapeSpec = TapeSpec()
    options: PrintOptions = PrintOptions()
    labels: list[LabelSpec] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def _strip_needs_continuous(self) -> PrintJob:
        if self.options.batch_mode == "strip" and self.tape.kind == "gap":
            raise ValueError(
                "batch_mode 'strip' requires continuous tape; die-cut media aligns to "
                "the gap sensor and a multi-label frame would print across the gaps"
            )
        return self


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #

LabelState = Literal["pending", "printed", "failed", "skipped_duplicate"]
JobState = Literal["completed", "partial", "failed", "stalled", "rejected"]


class LabelResult(Base):
    index: int
    state: LabelState
    copies_done: int = 0
    error: str | None = None
    retryable: bool = False


class JobResult(Base):
    v: Literal[1] = 1
    job_id: str
    state: JobState
    #: True when this is a replay of an already-processed idempotency key.
    duplicate: bool = False
    #: Set when a strip aborted mid-write. There is no read channel, so the amount
    #: of tape already consumed is unknowable; a human decides whether to re-queue.
    partial_tape_consumed: bool = False
    labels: list[LabelResult] = Field(default_factory=list)
    error: str | None = None


class PrinterStatus(Base):
    """Retained on ``<prefix>/<printer_id>/status``; the LWT publishes ``disconnected``."""

    v: Literal[1] = 1
    printer_id: str
    #: Still omits NO_MEDIA / PAPER_JAM, but no longer because they are unobservable:
    #: the printer reports media state on both transports and pushes it unsolicited
    #: (``0x06`` bit 0). Only bit 0 is decoded and jam has no known encoding, so the
    #: enum stays narrow until we can distinguish the cases rather than guess.
    state: Literal["idle", "printing", "disconnected", "error"]
    model: str | None = None
    #: Device serial, as reported by the printer. Available on both transports --
    #: SPP answers queries too; we simply never read the socket before.
    serial: str | None = None
    tape_width_mm: float | None = None
    pending_labels: int = 0
    error: str | None = None


def job_schema() -> dict[str, Any]:
    """The JSON Schema committed to ``contracts/job-v1.schema.json``."""
    return PrintJob.model_json_schema(mode="serialization")
