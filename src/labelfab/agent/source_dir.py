"""Filesystem job source.

The offline escape hatch: ``cp job.json /var/spool/labelfab/`` prints even when the
broker, the cluster or the internet is down. About fifty lines, and it turns a bench
with no network into a working label printer.

A processed file is moved into ``done/`` (or ``rejected/``) rather than deleted, so a
misprint can be inspected and re-dropped. Job-level idempotency still applies: dropping
the same file twice replays the cached result and prints nothing.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from labelfab.agent.spool import Outcome, Spool
from labelfab.contract import PrintJob

log = logging.getLogger("labelfab.dir")


class DirSource:
    def __init__(self, spool_dir: Path, spool: Spool, enqueue: Callable[[str], None]) -> None:
        self.dir = Path(spool_dir)
        self.spool = spool
        self.enqueue = enqueue
        self.done = self.dir / "done"
        self.rejected = self.dir / "rejected"
        for d in (self.dir, self.done, self.rejected):
            d.mkdir(parents=True, exist_ok=True)

    def poll(self) -> int:
        """Ingest any new ``*.json`` files. Returns how many jobs were enqueued."""
        enqueued = 0
        for path in sorted(self.dir.glob("*.json")):
            try:
                job = PrintJob.model_validate_json(path.read_text())
            except Exception as exc:
                log.warning("rejecting %s: %s", path.name, exc)
                path.rename(self.rejected / path.name)
                continue
            try:
                result = self.spool.try_insert(job)
            except Exception:
                log.exception("spool insert failed for %s", path.name)
                continue  # leave the file in place to retry next poll
            path.rename(self.done / path.name)
            if result.outcome is Outcome.NEW:
                self.enqueue(job.job_id)
                enqueued += 1
        return enqueued
