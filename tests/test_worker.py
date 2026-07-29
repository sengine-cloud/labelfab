"""Worker: the strip/discrete asymmetry, offline grace and crash recovery."""

from __future__ import annotations

from conftest import make_job

from labelfab.device import INIT_PACKETS, print_preamble


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
