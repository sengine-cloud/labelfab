"""Worker: the strip/discrete asymmetry, offline grace and crash recovery."""

from __future__ import annotations

import pytest
from conftest import make_job

from labelfab.device import INIT_PACKETS, print_preamble
from labelfab.device.escpos import GS_V0


def _first_body_byte() -> int:
    """Offset of the first raster byte: session setup, then the print preamble."""
    return sum(len(p) for p in INIT_PACKETS) + len(print_preamble(15, 320, density=2))


def test_strip_is_one_frame(harness):
    h = harness()
    h.submit(make_job("j", n_labels=3, flush=True))
    assert h.frames == 1  # three labels, one GS v 0 -- the whole economy of strip mode
    result = h.publisher.results[-1]
    assert result.state == "completed"
    assert h.spool.printed_labels("j") == {0, 1, 2}


def test_size_trigger_flushes_without_explicit_flush(harness):
    h = harness(max_labels=2, max_length_mm=10_000)
    h.submit(make_job("j", n_labels=2))  # no flush; count trigger fires
    assert h.frames == 1
    assert h.publisher.results[-1].state == "completed"


def test_nothing_prints_until_idle_then_flushes(harness, clock):
    h = harness(max_labels=100, max_wait_s=30, max_length_mm=10_000)
    h.submit(make_job("j", n_labels=1))
    assert h.frames == 0  # still pending
    assert not h.publisher.results
    clock.advance(30)
    h.worker.tick()
    assert h.frames == 1
    assert h.publisher.results[-1].state == "completed"


def test_configured_gap_tape_forces_discrete(harness):
    # The loaded media is the agent's own knowledge: configuring die-cut (gap) tape
    # makes a strip-mode job print discretely -- one frame per label -- regardless of
    # what the job claimed, because a multi-label frame would print across the gaps.
    h = harness(max_labels=100, max_length_mm=10_000)
    h.config.tape.kind = "gap"
    h.config.tape.length_mm = 30.0
    h.submit(make_job("j", n_labels=3, batch_mode="strip"))
    assert h.frames == 3
    assert h.publisher.results[-1].state == "completed"


def test_strip_partial_failure_is_terminal_and_flagged(harness):
    h = harness()
    # Cut the link once the preamble is out and a few body bytes have gone: the frame
    # header committed the printer to a length it will never receive, so tape has
    # moved. Derived rather than hardcoded -- the preamble grew when PRINT_MULTI and
    # EXIT_COMPRESS_MODE were added, and a literal offset silently stopped meaning
    # "mid-body" and started meaning "mid-preamble".
    h.fail_after_bytes = _first_body_byte() + 4
    h.submit(make_job("j", n_labels=3, flush=True))
    assert h.frames == 1  # one attempt, no auto-retry -- tape may have moved
    result = h.publisher.results[-1]
    assert result.state == "failed"
    assert result.partial_tape_consumed is True


def test_discrete_prints_a_frame_per_label(harness):
    h = harness()
    h.submit(make_job("j", n_labels=3, batch_mode="discrete"))
    assert h.frames == 3
    assert h.publisher.results[-1].state == "completed"


def test_recovery_skips_already_printed_labels(harness):
    h = harness()
    h.spool.try_insert(make_job("j", n_labels=3, batch_mode="discrete"))
    h.spool.label_printed("j", 0)
    h.spool.label_printed("j", 1)  # a crash left these done
    h.worker.submit("j")
    assert h.frames == 1  # only label 2 reprints
    assert h.publisher.results[-1].state == "completed"


def test_offline_returns_job_to_queue_not_failed(harness, clock):
    h = harness()
    h.offline = True
    h.submit(make_job("j", n_labels=1, flush=True))
    assert not h.publisher.results  # never failed
    assert h.spool.queued() == ["j"]  # still there to retry
    assert h.publisher.statuses[-1].state == "disconnected"

    h.offline = False
    clock.advance(20)  # past retry_interval_s
    h.worker.retry_queued()
    assert h.publisher.results[-1].state == "completed"


def test_wrong_model_is_rejected(harness):
    h = harness()
    h.submit(make_job("j", require_model="phomemo-m110", flush=True))
    assert h.frames == 0
    assert h.publisher.results[-1].state == "rejected"


def test_dedupe_key_skips_a_second_job(harness):
    h = harness()
    h.submit(make_job("a", n_labels=1, flush=True, dedupe_key="bin-a4"))
    assert h.publisher.results[-1].state == "completed"
    frames_after_a = h.frames

    h.submit(make_job("b", idempotency_key="b", n_labels=1, flush=True, dedupe_key="bin-a4"))
    assert h.frames == frames_after_a  # nothing new printed
    result = h.publisher.results[-1]
    assert result.state == "completed"
    assert result.labels[0].state == "skipped_duplicate"


def _counting_factory(harness, error: Exception):
    """A printer_factory that fails connect with ``error`` and counts the attempts."""
    from labelfab.device import FakeTransport, PhomemoD30
    from labelfab.device.d30 import D30Config

    attempts: list[int] = []

    class _Failing(FakeTransport):
        def open(self) -> None:
            attempts.append(1)
            raise error

    def factory() -> PhomemoD30:
        return PhomemoD30(_Failing(), D30Config(pace_factor=0.0), sleep=lambda _s: None)

    harness.worker.printer_factory = factory
    return attempts


def test_a_permanent_connect_failure_is_not_retried(harness):
    """A malformed device.mac fails once; the retry would re-read the same bad string.

    The backoff exists for a printer that has auto-powered-off, not for a typo.
    """
    from labelfab.device.errors import D30ConfigError

    h = harness()
    attempts = _counting_factory(h, D30ConfigError("cannot address the printer: nope"))
    h.submit(make_job("j", n_labels=1, flush=True))
    assert len(attempts) == 1


def test_a_transient_connect_failure_still_gets_every_attempt(harness):
    """The counterpart: an asleep printer must keep its full retry budget."""
    from labelfab.device.errors import D30ConnectError

    h = harness()
    attempts = _counting_factory(h, D30ConnectError("printer is asleep"))
    h.submit(make_job("j", n_labels=1, flush=True))
    assert len(attempts) == h.worker.max_attempts


def _reporting_factory(harness, *frames: str, media: str = "1a0689"):
    """A printer that volunteers real status frames on connect, as the D30 does."""
    from labelfab.device import FakeTransport, PhomemoD30
    from labelfab.device.d30 import D30Config

    class _Reporting(FakeTransport):
        def open(self) -> None:
            super().open()
            self.inject(bytes.fromhex("1a08" + b"Q223P4C31420105".hex()))
            self.inject(bytes.fromhex("1a07020102"))
            self.inject(bytes.fromhex("1a0464"))
            self.inject(bytes.fromhex("1a2f01a1"))
            self.inject(bytes.fromhex(media))
            for extra in frames:
                self.inject(bytes.fromhex(extra))

    def factory() -> PhomemoD30:
        transport = _Reporting(fail_after_bytes=harness.fail_after_bytes)
        harness.transports.append(transport)
        return PhomemoD30(transport, D30Config(pace_factor=0.0), sleep=lambda _s: None)

    harness.worker.printer_factory = factory


def test_status_carries_what_the_printer_reported(harness):
    """InvenTree should learn firmware/battery/voltage/media, not just idle-vs-printing."""
    h = harness()
    _reporting_factory(h)
    h.submit(make_job("j", n_labels=1, flush=True))

    status = h.publisher.statuses[-1]
    assert status.state == "idle"
    assert status.serial == "Q223P4C31420105"
    assert status.firmware == "2.1.2"
    assert status.battery_pct == 100
    assert status.voltage_v == 4.17
    assert status.media_ok is True
    assert status.error is None


def test_a_media_fault_publishes_error_not_idle(harness):
    """A printed batch with the media bit clear must not settle as healthy."""
    h = harness()
    _reporting_factory(h, media="1a0688")  # bit0 clear
    h.submit(make_job("j", n_labels=1, flush=True))

    status = h.publisher.statuses[-1]
    assert status.state == "error"
    assert status.media_ok is False
    assert "media not ready" in (status.error or "")


def test_a_fault_clears_on_the_next_good_batch(harness):
    """Otherwise a transient media error latches and the printer looks broken forever."""
    h = harness()
    _reporting_factory(h, media="1a0688")
    h.submit(make_job("a", n_labels=1, flush=True))
    assert h.publisher.statuses[-1].state == "error"

    _reporting_factory(h, media="1a0689")
    h.submit(make_job("b", idempotency_key="b", n_labels=1, flush=True))
    settled = h.publisher.statuses[-1]
    assert settled.state == "idle"
    assert settled.error is None
    assert settled.media_ok is True


def test_a_fault_is_captured_even_when_the_print_fails(harness):
    """The fault is usually *why* it failed, so closing without reading it loses it."""
    h = harness()
    h.fail_after_bytes = 40  # die mid-frame
    _reporting_factory(h, media="1a0688")
    h.submit(make_job("j", n_labels=1, flush=True))

    status = h.publisher.statuses[-1]
    assert status.state == "error"
    assert "media not ready" in (status.error or "")


def test_render_is_capped_at_the_print_head(harness):
    """15mm tape with a 96-dot head must not render 120px: the printer refuses it.

    Verified on hardware -- a 120px raster came back print_cancelled (0x0B) and printed
    nothing, while the same label at 96px printed. device.raster_width_px was declared
    and used nowhere, so the shipped 15mm default cancelled every job.
    """
    from labelfab.contract import PX_PER_MM

    h = harness()
    h.config.tape.width_mm = 15.0
    h.config.device.raster_width_px = 96
    assert h.worker._loaded_tape().width_mm == pytest.approx(96 / PX_PER_MM)

    h.submit(make_job("j", n_labels=1, flush=True))
    body = bytes(h.last_transport.buf)
    # GS v 0 then xL xH: bytes per line must be the head's 12, not the tape's 15.
    idx = body.index(GS_V0) + len(GS_V0)
    assert body[idx] == 12, f"raster is {body[idx]} bytes/line, head is 12"


def test_narrower_tape_than_the_head_is_left_alone(harness):
    """12mm head, 6mm tape: cap must not widen anything."""
    h = harness()
    h.config.tape.width_mm = 6.0
    h.config.device.raster_width_px = 96
    assert h.worker._loaded_tape().width_mm == 6.0
