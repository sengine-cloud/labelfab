"""The print agent: MQTT/dir job sources, a durable spool, the strip coalescer and
the print worker.

The device and render layers know nothing about queues; everything time-, retry- and
delivery-shaped lives here. The pieces are deliberately separable and clock-injected
so the whole path -- a job arriving to a strip on tape -- is exercisable with a fake
transport and no wall clock.
"""

from __future__ import annotations

from labelfab.agent.coalescer import Batch, Coalescer, PendingLabel
from labelfab.agent.config import Config, load
from labelfab.agent.publisher import NullPublisher, Publisher, RecordingPublisher
from labelfab.agent.spool import InsertResult, Outcome, Spool
from labelfab.agent.worker import PrintWorker

__all__ = [
    "Batch",
    "Coalescer",
    "Config",
    "InsertResult",
    "NullPublisher",
    "Outcome",
    "PendingLabel",
    "PrintWorker",
    "Publisher",
    "RecordingPublisher",
    "Spool",
    "load",
]
