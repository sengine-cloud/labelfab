"""Shared fixtures for the agent tests.

Everything runs against a ``FakeTransport`` and an injected clock, so the full path
-- a job arriving, coalescing into a strip, and the bytes that would hit the printer
-- is exercised with no hardware and no wall time.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from labelfab.agent import Config, PrintWorker, RecordingPublisher, Spool
from labelfab.contract import LabelSpec, PrinterRef, PrintJob, PrintOptions, TapeSpec
from labelfab.device import FakeTransport, PhomemoD30
from labelfab.device.d30 import D30Config
from labelfab.device.escpos import GS_V0


class Clock:
    """A hand-cranked clock, so idle windows and retries are deterministic."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_config(**strip: float) -> Config:
    cfg = Config()
    cfg.tape.width_mm = 12.0  # 96px head, byte-aligned
    cfg.device.raster_width_px = 96
    cfg.device.pace_factor = 0.0
    cfg.strip.separator_mm = 2.0
    for key, value in strip.items():
        setattr(cfg.strip, key, value)
    return cfg


def make_job(
    job_id: str,
    *,
    idempotency_key: str | None = None,
    n_labels: int = 1,
    copies: int = 1,
    batch_mode: str = "strip",
    flush: bool = False,
    tape_width_mm: float = 12.0,
    tape_kind: str = "continuous",
    require_model: str | None = None,
    dedupe_key: str | None = None,
    value: str = "hi",
) -> PrintJob:
    from labelfab.contract import TextElement

    labels = [
        LabelSpec(elements=[TextElement(value=f"{value}{i}")], copies=copies, dedupe_key=dedupe_key)
        for i in range(n_labels)
    ]
    return PrintJob(
        job_id=job_id,
        idempotency_key=idempotency_key or job_id,
        printer=PrinterRef(id="d30-workshop", require_model=require_model),
        tape=TapeSpec(width_mm=tape_width_mm, kind=tape_kind),
        options=PrintOptions(batch_mode=batch_mode, flush=flush),
        labels=labels,
    )


def count_frames(transport: FakeTransport) -> int:
    """Each ``GS v 0`` is exactly one printed frame -- the whole point of strip mode."""
    return bytes(transport.buf).count(GS_V0)


class WorkerHarness:
    def __init__(self, tmp_path, clock: Clock, **strip: float) -> None:
        self.clock = clock
        self.config = make_config(**strip)
        self.spool = Spool(tmp_path / "spool.db", clock=clock)
        self.publisher = RecordingPublisher()
        #: Flip to a byte count to simulate a mid-frame disconnect; None = healthy.
        self.fail_after_bytes: int | None = None
        #: Raise on connect (printer asleep) when True.
        self.offline = False
        self.transports: list[FakeTransport] = []
        self.worker = PrintWorker(
            self.config,
            self.spool,
            self.publisher,
            self._factory,
            clock=clock,
            sleep=lambda _s: None,
        )

    def _factory(self) -> PhomemoD30:
        fail = 0 if self.offline else self.fail_after_bytes
        transport = FakeTransport(fail_after_bytes=fail)
        self.transports.append(transport)
        return PhomemoD30(transport, D30Config(pace_factor=0.0), sleep=lambda _s: None)

    @property
    def last_transport(self) -> FakeTransport:
        return self.transports[-1]

    @property
    def frames(self) -> int:
        """Total ``GS v 0`` frames across every send this harness performed."""
        return sum(bytes(t.buf).count(GS_V0) for t in self.transports)

    def submit(self, job: PrintJob) -> None:
        self.spool.try_insert(job)
        self.worker.submit(job.job_id)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def harness(tmp_path, clock: Clock) -> Callable[..., WorkerHarness]:
    def build(**strip: float) -> WorkerHarness:
        return WorkerHarness(tmp_path, clock, **strip)

    return build
