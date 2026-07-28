"""Coalescer: flush on each of the five triggers, and on nothing else."""

from __future__ import annotations

from conftest import Clock

from labelfab.agent import Coalescer, PendingLabel
from labelfab.contract import mm_to_px
from labelfab.render.raster import canvas


def _label(job="j", idx=0, length_mm=10.0):
    img = canvas(mm_to_px(length_mm), mm_to_px(12))
    return PendingLabel(job_id=job, label_index=idx, image=img)


def test_count_trigger():
    c = Coalescer(max_labels=2, max_length_mm=10_000, clock=Clock())
    c.add(_label(idx=0), 12.0)
    assert not c.is_full()
    c.add(_label(idx=1), 12.0)
    assert c.is_full()


def test_length_trigger():
    c = Coalescer(max_labels=100, max_length_mm=25.0, clock=Clock())
    c.add(_label(length_mm=10), 12.0)
    assert not c.is_full()
    c.add(_label(length_mm=20), 12.0)  # 10 + 2 sep + 20 > 25
    assert c.is_full()


def test_idle_trigger():
    clock = Clock()
    c = Coalescer(max_wait_s=30, clock=clock)
    c.add(_label(), 12.0)
    assert not c.idle_expired()
    clock.advance(30)
    assert c.idle_expired()


def test_geometry_change_rejected_until_flush():
    c = Coalescer(clock=Clock())
    c.add(_label(), 12.0)
    assert not c.accepts(15.0)  # a different width cannot share the frame
    c.flush()
    assert c.accepts(15.0)


def test_flush_empties_and_returns_batch():
    c = Coalescer(clock=Clock())
    c.add(_label(job="a", idx=0), 12.0)
    c.add(_label(job="a", idx=1), 12.0)
    batch = c.flush()
    assert batch is not None and len(batch.labels) == 2
    assert batch.job_ids == ["a"]
    assert c.is_empty
    assert c.flush() is None  # nothing left
