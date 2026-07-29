"""The print worker: jobs in, tape (or a captured raster) out.

This is where the strip/discrete asymmetry the plan hinges on actually lives.

* **Strip mode** buffers labels into a coalescer and prints the whole batch as one
  ``GS v 0`` frame. Its retry unit is the *entire strip*: once tape has moved there
  is no read channel to ask how far it got, so a mid-strip failure is marked
  ``partial_tape_consumed`` and **never auto-retried** -- retrying would silently
  double-print whatever already came out. Retry *before* the first byte is free.

* **Discrete mode** prints one frame per label and commits after each, so a crash
  reprints at most one label. Its retry unit is the single label.

Reconnection policy lives here rather than in the device class, because the decision
needs the job deadline and the batch mode -- neither of which belongs in a driver.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from labelfab.agent.coalescer import Batch, Coalescer, PendingLabel
from labelfab.agent.config import Config
from labelfab.agent.publisher import Publisher
from labelfab.agent.spool import Spool
from labelfab.contract import JobResult, LabelResult, PrinterStatus, PrintJob, TapeSpec
from labelfab.device.d30 import MODEL, PhomemoD30
from labelfab.device.errors import D30Error
from labelfab.render import RenderConfig, concat_strip, render_label, to_device
from labelfab.render.errors import RenderError

log = logging.getLogger("labelfab.worker")

#: Backoff between reconnect attempts within one send, in seconds.
_BACKOFF_S = (1.0, 3.0, 9.0)


@dataclass
class _JobAcc:
    """Running tally for one in-flight job, across however many batches it spans."""

    job: PrintJob
    #: label index -> copies that should print (0 for render-failed / skipped labels)
    expected: dict[int, int] = field(default_factory=dict)
    done: dict[int, int] = field(default_factory=dict)
    failed: dict[int, int] = field(default_factory=dict)
    #: Terminal per-label states decided at render time (skipped_duplicate / failed).
    render_states: dict[int, LabelResult] = field(default_factory=dict)
    partial_tape: bool = False

    @property
    def total_expected(self) -> int:
        return sum(self.expected.values())

    @property
    def settled(self) -> int:
        return sum(self.done.values()) + sum(self.failed.values())

    @property
    def is_complete(self) -> bool:
        return self.settled >= self.total_expected


class PrintWorker:
    def __init__(
        self,
        config: Config,
        spool: Spool,
        publisher: Publisher,
        printer_factory: Callable[[], PhomemoD30],
        *,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        max_attempts: int = 3,
        retry_interval_s: float = 15.0,
    ) -> None:
        self.config = config
        self.spool = spool
        self.publisher = publisher
        self.printer_factory = printer_factory
        self.clock = clock
        self.sleep = sleep
        self.max_attempts = max_attempts
        #: Minimum gap between re-attempts of a job stalled on an offline printer.
        self.retry_interval_s = retry_interval_s
        #: job_id -> last stall time, so a napping printer is retried, not busy-looped.
        self._stalled: dict[str, float] = {}
        #: Latest device serial learned from the BLE feedback channel, if any.
        self._device_serial: str | None = None
        self.coalescer = Coalescer(
            max_wait_s=config.strip.max_wait_s,
            max_length_mm=config.strip.max_length_mm,
            max_labels=config.strip.max_labels,
            separator_mm=config.strip.separator_mm,
            clock=clock,
        )
        self._acc: dict[str, _JobAcc] = {}

    # -- render config ------------------------------------------------------ #

    def _render_cfg(self) -> RenderConfig:
        return RenderConfig(
            qr_base_url=self.config.render.qr_base_url,
            threshold=self.config.render.threshold,
            rotation=self.config.tape.rotation,
            mirror=self.config.tape.mirror,
            separator_mm=self.config.strip.separator_mm,
        )

    def _loaded_tape(self) -> TapeSpec:
        """The media in the printer, per the agent's [tape] config. Authoritative over
        whatever a job claims -- a producer cannot know what tape was last loaded."""
        return TapeSpec(
            width_mm=self.config.tape.width_mm,
            kind=self.config.tape.kind,  # type: ignore[arg-type]
            length_mm=self.config.tape.length_mm,
        )

    # -- public entry points ------------------------------------------------ #

    def submit(self, job_id: str) -> None:
        """Render a spooled job and either buffer it (strip) or print it (discrete)."""
        job = self.spool.load(job_id)
        if job.printer.require_model and job.printer.require_model != MODEL:
            self._reject(job, f"this agent drives {MODEL}, not {job.printer.require_model}")
            return

        self.spool.mark(job_id, "printing")
        acc = _JobAcc(job=job)
        self._acc[job_id] = acc

        # The loaded media is the agent's own knowledge, not the producer's, so the
        # configured tape overrides the job's geometry (width, kind, fixed length).
        tape = self._loaded_tape()

        already = self.spool.printed_labels(job_id)  # crash-recovery: skip finished labels
        cfg = self._render_cfg()
        queued: list[tuple[int, object]] = []
        for idx, label in enumerate(job.labels):
            if idx in already:
                acc.render_states[idx] = LabelResult(index=idx, state="printed")
                continue
            dedupe = f"{self.config.agent.printer_id}:{label.dedupe_key}" if label.dedupe_key else None
            if dedupe and self.spool.is_recent_dedupe(dedupe):
                acc.render_states[idx] = LabelResult(index=idx, state="skipped_duplicate")
                continue
            try:
                image = render_label(label, tape, cfg)
            except RenderError as exc:
                acc.render_states[idx] = LabelResult(
                    index=idx, state="failed", error=str(exc), retryable=False
                )
                continue
            acc.expected[idx] = label.copies
            for _ in range(label.copies):
                queued.append((idx, image))
            if dedupe:
                self.spool.record_dedupe(dedupe, job_id)

        if acc.total_expected == 0:  # everything skipped, deduped or render-failed
            self._finalize(job_id)
            return

        discrete = job.options.batch_mode == "discrete" or tape.kind == "gap"
        if discrete:
            self._flush()  # preserve arrival order relative to any pending strip
            self._run_discrete(job_id, queued)
            return

        for idx, image in queued:
            width = tape.width_mm
            if not self.coalescer.accepts(width):
                self._flush()  # a different tape width cannot share this frame
            self.coalescer.add(PendingLabel(job_id, idx, image), width)  # type: ignore[arg-type]
            if self.coalescer.is_full():
                self._flush()
        if job.options.flush:
            self._flush()
        self._maybe_finalize(job_id)

    def tick(self) -> None:
        """Called by the run loop when the idle window may have expired."""
        if self.coalescer.idle_expired():
            self._flush()
        self.retry_queued()

    def retry_queued(self) -> None:
        """Re-submit jobs stalled on an offline printer, once the retry gap passes.

        A printer that has auto-slept is the dominant real failure, so a stalled job
        is returned to the queue rather than failed and re-attempted here -- never
        faster than ``retry_interval_s``, so a dead printer is polled, not spun on.
        """
        now = self.clock()
        for job_id in self.spool.queued():
            if job_id in self._acc:
                continue  # already in flight
            last = self._stalled.get(job_id, 0.0)
            if now - last < self.retry_interval_s:
                continue
            self._stalled[job_id] = now
            try:
                self.submit(job_id)
            except KeyError:
                pass

    def flush(self) -> None:
        """Force out the pending strip -- the ``cmd`` topic or ``labelfab flush``."""
        self._flush()

    def seconds_until_idle_flush(self) -> float | None:
        return self.coalescer.seconds_until_idle()

    def recover(self) -> None:
        """On boot, re-submit anything left queued or mid-print by a crash."""
        for job_id in self.spool.queued():
            try:
                self.submit(job_id)
            except KeyError:
                pass

    # -- flushing and printing ---------------------------------------------- #

    def _flush(self) -> None:
        batch = self.coalescer.flush()
        if batch is not None:
            self._print_strip(batch)

    def _print_strip(self, batch: Batch) -> None:
        strip = concat_strip(batch.images, self.config.strip.separator_mm)
        raster = to_device(
            strip,
            rotation=self.config.tape.rotation,
            mirror=self.config.tape.mirror,
            threshold=self.config.render.threshold,
        )
        self._publish_status("printing", pending=len(batch.labels))
        outcome = self._send(raster, is_strip=True)

        if not outcome.ok and not outcome.wrote:
            # Never touched tape -- the printer is unreachable, most likely asleep.
            # Return every job in the strip to the queue; do not fail them.
            self._stall(batch.job_ids)
            return

        for pl in batch.labels:
            if outcome.ok:
                self._credit_copy(pl.job_id, pl.label_index)
            else:
                self._fail_copy(pl.job_id, pl.label_index)
                self._acc[pl.job_id].partial_tape = True  # tape moved, unknowable amount
        for job_id in batch.job_ids:
            self._maybe_finalize(job_id)
        self._publish_status("idle")

    def _run_discrete(self, job_id: str, queued: list[tuple[int, object]]) -> None:
        for index, image in queued:
            raster = to_device(
                image,  # type: ignore[arg-type]
                rotation=self.config.tape.rotation,
                mirror=self.config.tape.mirror,
                threshold=self.config.render.threshold,
            )
            self._publish_status("printing", pending=1)
            outcome = self._send(raster, is_strip=False)
            if not outcome.ok and not outcome.wrote:
                # Offline before this label printed: requeue the whole job. Recovery
                # skips the labels already committed, so it resumes, never reprints.
                self._stall([job_id])
                return
            if outcome.ok:
                self._credit_copy(job_id, index)
            else:
                self._fail_copy(job_id, index)  # wrote but failed: one label lost
        self._maybe_finalize(job_id)
        self._publish_status("idle")

    def _stall(self, job_ids: list[str]) -> None:
        now = self.clock()
        for job_id in job_ids:
            self.spool.requeue(job_id)
            self._acc.pop(job_id, None)
            self._stalled[job_id] = now
        self._publish_status("disconnected")

    # -- the retrying send -------------------------------------------------- #

    @dataclass(frozen=True, slots=True)
    class _Send:
        ok: bool
        wrote: bool  # did we begin writing the frame? (tape may have moved)
        error: str = ""

    def _send(self, raster, *, is_strip: bool) -> _Send:
        """Connect and print one frame, retrying only where it is safe to.

        A connect failure never touched tape, so it is retried freely. A failure
        *during* the frame means the head may already be moving: for a strip that is
        terminal, for a discrete label it is a safe single-label reprint.
        """
        last = ""
        for attempt in range(self.max_attempts):
            printer = self.printer_factory()
            try:
                printer.connect()
            except D30Error as exc:  # never wrote a byte
                printer.close()
                last = str(exc)
                if attempt + 1 < self.max_attempts:
                    self.sleep(_BACKOFF_S[min(attempt, len(_BACKOFF_S) - 1)])
                    continue
                return self._Send(ok=False, wrote=False, error=last)
            try:
                printer.print_raster(raster)
            except D30Error as exc:
                printer.close()
                last = str(exc)
                if is_strip:
                    return self._Send(ok=False, wrote=True, error=last)  # terminal
                if exc.retryable and attempt + 1 < self.max_attempts:
                    self.sleep(_BACKOFF_S[min(attempt, len(_BACKOFF_S) - 1)])
                    continue
                return self._Send(ok=False, wrote=True, error=last)
            self._capture_feedback(printer)
            printer.close()
            return self._Send(ok=True, wrote=True)
        return self._Send(ok=False, wrote=False, error=last or "no attempt made")

    def _capture_feedback(self, printer: PhomemoD30) -> None:
        """Read the device feedback channel (BLE notify), if present, after a print."""
        fb = printer.feedback
        if fb is None:
            return
        if fb.serial:
            self._device_serial = fb.serial
        log.info("device feedback: %s", fb.summary())

    # -- result accounting -------------------------------------------------- #

    def _credit_copy(self, job_id: str, index: int) -> None:
        acc = self._acc[job_id]
        acc.done[index] = acc.done.get(index, 0) + 1
        if acc.done[index] >= acc.expected.get(index, 0):
            self.spool.label_printed(job_id, index)  # commit: recovery won't reprint it
        self.publisher.publish_progress(job_id, sum(acc.done.values()), acc.total_expected)

    def _fail_copy(self, job_id: str, index: int) -> None:
        acc = self._acc[job_id]
        acc.failed[index] = acc.failed.get(index, 0) + 1

    def _maybe_finalize(self, job_id: str) -> None:
        acc = self._acc.get(job_id)
        if acc is not None and acc.is_complete:
            self._finalize(job_id)

    def _finalize(self, job_id: str) -> None:
        acc = self._acc.pop(job_id)
        labels: list[LabelResult] = []
        any_printed = any_failed = False
        for idx in range(len(acc.job.labels)):
            if idx in acc.render_states:
                lr = acc.render_states[idx]
                if lr.state == "printed":
                    any_printed = True
                elif lr.state == "failed":
                    any_failed = True
                labels.append(lr)
                continue
            done = acc.done.get(idx, 0)
            expected = acc.expected.get(idx, 0)
            if done >= expected and expected > 0:
                labels.append(LabelResult(index=idx, state="printed", copies_done=done))
                any_printed = True
            else:
                any_failed = True
                if done > 0:
                    any_printed = True
                labels.append(
                    LabelResult(
                        index=idx,
                        state="failed",
                        copies_done=done,
                        error="strip aborted before all copies printed",
                        retryable=False,
                    )
                )

        if any_printed and any_failed:
            state = "partial"
        elif any_failed:
            state = "failed"
        else:
            state = "completed"

        result = JobResult(
            job_id=job_id,
            state=state,
            partial_tape_consumed=acc.partial_tape,
            labels=labels,
        )
        self.spool.finish(job_id, result)
        self.publisher.publish_result(result)

    def _reject(self, job: PrintJob, reason: str) -> None:
        result = JobResult(job_id=job.job_id, state="rejected", error=reason)
        self.spool.finish(job.job_id, result)
        self.publisher.publish_result(result)

    # -- status ------------------------------------------------------------- #

    def _publish_status(self, state: str, *, pending: int = 0) -> None:
        self.publisher.publish_status(
            PrinterStatus(
                printer_id=self.config.agent.printer_id,
                state=state,  # type: ignore[arg-type]
                model=MODEL,
                serial=self._device_serial,
                tape_width_mm=self.config.tape.width_mm,
                pending_labels=pending,
            )
        )
