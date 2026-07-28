"""Strip coalescer.

Strips only pay off if jobs accumulate, so the agent buffers rendered labels into a
*pending strip* and flushes it on the first of five triggers:

* idle -- ``max_wait_s`` elapsed since the most recent label with nothing new;
* length -- the accumulated strip reached ``max_length_mm``;
* count -- ``max_labels`` labels buffered;
* geometry -- a label arrived on a different tape width (it cannot share a frame);
* explicit -- a job set ``flush: true`` or an operator sent the ``cmd`` topic.

This class owns only the accumulation and the size/idle predicates. The worker
decides *what* to do on a flush, because that needs the printer and the spool, which
have no business here -- keeping the coalescer a pure, clock-injected unit that tests
can drive without either.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from PIL import Image

from labelfab.contract import PX_PER_MM


@dataclass(frozen=True, slots=True)
class PendingLabel:
    """One rendered copy waiting to join a strip, tagged for result attribution."""

    job_id: str
    label_index: int
    image: Image.Image  # landscape, greyscale; x runs along the tape


@dataclass(frozen=True, slots=True)
class Batch:
    """A flushed strip: the labels to concatenate and the width they printed on."""

    labels: tuple[PendingLabel, ...]
    tape_width_mm: float

    @property
    def images(self) -> list[Image.Image]:
        return [pl.image for pl in self.labels]

    @property
    def job_ids(self) -> list[str]:
        seen: dict[str, None] = {}
        for pl in self.labels:
            seen.setdefault(pl.job_id, None)
        return list(seen)


@dataclass
class Coalescer:
    max_wait_s: float = 30.0
    max_length_mm: float = 300.0
    max_labels: int = 24
    separator_mm: float = 2.0
    clock: Callable[[], float] = time.time

    _pending: list[PendingLabel] = field(default_factory=list, init=False)
    _width_mm: float | None = field(default=None, init=False)
    _last_add_at: float | None = field(default=None, init=False)

    @property
    def is_empty(self) -> bool:
        return not self._pending

    @property
    def count(self) -> int:
        return len(self._pending)

    def accepts(self, width_mm: float) -> bool:
        """A label may join only an empty strip or one on the same tape width."""
        return self._width_mm is None or self._width_mm == width_mm

    def length_mm(self) -> float:
        if not self._pending:
            return 0.0
        px = sum(pl.image.width for pl in self._pending)
        seps = self.separator_mm * (len(self._pending) - 1)
        return px / PX_PER_MM + seps

    def add(self, label: PendingLabel, width_mm: float) -> None:
        if not self.accepts(width_mm):
            raise ValueError("geometry change: flush the pending strip before adding")
        self._width_mm = width_mm
        self._pending.append(label)
        self._last_add_at = self.clock()

    def is_full(self) -> bool:
        """A size trigger has fired; the caller should flush now."""
        return self.count >= self.max_labels or self.length_mm() >= self.max_length_mm

    def idle_expired(self) -> bool:
        if self._last_add_at is None or not self._pending:
            return False
        return (self.clock() - self._last_add_at) >= self.max_wait_s

    def seconds_until_idle(self) -> float | None:
        """How long the print loop may block before it must flush on idle."""
        if self._last_add_at is None or not self._pending:
            return None
        return max(0.0, self.max_wait_s - (self.clock() - self._last_add_at))

    def flush(self) -> Batch | None:
        """Pop the pending strip. ``None`` when there is nothing to print."""
        if not self._pending:
            return None
        batch = Batch(labels=tuple(self._pending), tape_width_mm=self._width_mm or 0.0)
        self._pending = []
        self._width_mm = None
        self._last_add_at = None
        return batch
