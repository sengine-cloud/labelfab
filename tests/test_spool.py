"""Spool: the commit-before-ack correctness core."""

from __future__ import annotations

from conftest import Clock, make_job

from labelfab.agent import DeviceSnapshot, Outcome, Spool
from labelfab.contract import JobResult, LabelResult


def test_new_then_duplicate_replays_cached_result(tmp_path):
    clock = Clock()
    spool = Spool(tmp_path / "s.db", clock=clock)
    job = make_job("j1", idempotency_key="key-1")

    first = spool.try_insert(job)
    assert first.outcome is Outcome.NEW

    result = JobResult(job_id="j1", state="completed", labels=[LabelResult(index=0, state="printed")])
    spool.finish("j1", result)

    again = spool.try_insert(job)  # redelivery of the same key
    assert again.outcome is Outcome.DUPLICATE
    assert again.cached is not None and again.cached.state == "completed"


def test_queued_lists_unfinished_oldest_first(tmp_path):
    clock = Clock()
    spool = Spool(tmp_path / "s.db", clock=clock)
    spool.try_insert(make_job("a"))
    clock.advance(1)
    spool.try_insert(make_job("b"))
    assert spool.queued() == ["a", "b"]
    spool.finish("a", JobResult(job_id="a", state="completed"))
    assert spool.queued() == ["b"]


def test_label_printed_survives_and_dedups(tmp_path):
    clock = Clock()
    spool = Spool(tmp_path / "s.db", clock=clock)
    spool.try_insert(make_job("j", n_labels=3))
    spool.label_printed("j", 0)
    spool.label_printed("j", 2)
    spool.label_printed("j", 0)  # idempotent
    assert spool.printed_labels("j") == {0, 2}


def test_device_snapshot_survives_the_process(tmp_path):
    """The printer only talks during a print, so this is the only way a restarted agent
    knows anything about the hardware it drives."""
    path = tmp_path / "s.db"
    snap = DeviceSnapshot(serial="Q223P4C31420105", firmware="2.1.2", media_ok=True, seen_at=1000.0)

    first = Spool(path)
    assert first.device_snapshot() == DeviceSnapshot()  # nothing learned yet
    first.save_device_snapshot(snap)
    first.close()

    assert Spool(path).device_snapshot() == snap


def test_the_latest_device_snapshot_replaces_the_last(tmp_path):
    spool = Spool(tmp_path / "s.db")
    spool.save_device_snapshot(DeviceSnapshot(battery_pct=100, seen_at=1000.0))
    spool.save_device_snapshot(DeviceSnapshot(battery_pct=88, seen_at=2000.0))
    assert spool.device_snapshot().battery_pct == 88


def test_an_unreadable_device_snapshot_does_not_take_the_agent_down(tmp_path):
    """A status field is cosmetic; refusing to boot over one would not be."""
    spool = Spool(tmp_path / "s.db")
    spool.save_device_snapshot(DeviceSnapshot(serial="X"))
    spool._db.execute("UPDATE device_state SET payload = 'not json' WHERE id = 1")
    assert spool.device_snapshot() == DeviceSnapshot()


def test_dedupe_recorded_and_purged(tmp_path):
    clock = Clock()
    spool = Spool(tmp_path / "s.db", clock=clock)
    spool.record_dedupe("bin-a4", "j1")
    assert spool.is_recent_dedupe("bin-a4")
    clock.advance(100)
    assert spool.purge_dedupe(ttl_s=50) == 1
    assert not spool.is_recent_dedupe("bin-a4")
