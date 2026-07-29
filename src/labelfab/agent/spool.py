"""Durable job spool.

The correctness core of the agent. The rule that makes broker redelivery safe is
**commit before PUBACK**: a job is parsed, validated, inserted and committed here,
and only then does the source acknowledge it to the broker. A crash in that window
leaves the job un-acked, so the broker redelivers it -- exactly what we want.

SQLite in WAL mode with ``synchronous=FULL``: the threat model is a bench power-cut,
not throughput (the write rate is about one job a second), so durability wins.

Two kinds of de-duplication live here and they are not the same thing:

* ``idempotency_key`` deduplicates whole *job deliveries*. A redelivered job replays
  its cached result and prints nothing.
* ``dedupe_key`` deduplicates individual *labels* across different jobs, so a "print
  bin A4" that fires twice an hour apart does not waste tape.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from labelfab.contract import JobResult, PrintJob

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id          TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    payload         TEXT NOT NULL,
    state           TEXT NOT NULL,
    result          TEXT,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_state_created ON jobs (state, created_at);

CREATE TABLE IF NOT EXISTS dedupe (
    dedupe_key TEXT PRIMARY KEY,
    job_id     TEXT NOT NULL,
    printed_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS printed_labels (
    job_id      TEXT NOT NULL,
    label_index INTEGER NOT NULL,
    printed_at  REAL NOT NULL,
    PRIMARY KEY (job_id, label_index)
);
"""


class Outcome(Enum):
    """What ``try_insert`` did with a delivery."""

    NEW = "new"  # freshly spooled; hand it to the print loop
    DUPLICATE = "duplicate"  # already processed; replay the cached result


@dataclass(frozen=True, slots=True)
class InsertResult:
    outcome: Outcome
    #: Present only for a DUPLICATE whose original run already produced a result.
    cached: JobResult | None = None


class Spool:
    def __init__(self, path: Path | str, *, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        # ``check_same_thread=False``: paho's network thread inserts, the print loop
        # reads. Every write goes through one connection guarded by SQLite's own
        # locking, and our writes are short, so a single connection is simplest.
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.executescript(SCHEMA)

    def close(self) -> None:
        self._db.close()

    # -- delivery intake ---------------------------------------------------- #

    def try_insert(self, job: PrintJob) -> InsertResult:
        """Spool a validated job, or report it as an already-seen delivery.

        Returns before any printing happens. The caller PUBACKs on ``NEW`` *and* on
        ``DUPLICATE`` -- both are durably accounted for, and re-acking a duplicate is
        what stops a redelivery loop.
        """
        now = self._clock()
        row = self._db.execute(
            "SELECT job_id, result FROM jobs WHERE idempotency_key = ?",
            (job.idempotency_key,),
        ).fetchone()
        if row is not None:
            cached = JobResult.model_validate_json(row["result"]) if row["result"] else None
            return InsertResult(Outcome.DUPLICATE, cached)

        self._db.execute(
            "INSERT INTO jobs (job_id, idempotency_key, payload, state, created_at, updated_at) "
            "VALUES (?, ?, ?, 'queued', ?, ?)",
            (job.job_id, job.idempotency_key, job.model_dump_json(), now, now),
        )
        return InsertResult(Outcome.NEW)

    # -- print-loop queries ------------------------------------------------- #

    def queued(self) -> list[str]:
        """Job ids awaiting print, oldest first. Also the boot-recovery list."""
        rows = self._db.execute(
            "SELECT job_id FROM jobs WHERE state IN ('queued', 'printing') ORDER BY created_at"
        ).fetchall()
        return [r["job_id"] for r in rows]

    def load(self, job_id: str) -> PrintJob:
        row = self._db.execute("SELECT payload FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return PrintJob.model_validate_json(row["payload"])

    def mark(self, job_id: str, state: str) -> None:
        self._db.execute(
            "UPDATE jobs SET state = ?, updated_at = ? WHERE job_id = ?",
            (state, self._clock(), job_id),
        )

    def finish(self, job_id: str, result: JobResult) -> None:
        """Store the terminal result and its state. Caches the result for replay."""
        self._db.execute(
            "UPDATE jobs SET state = ?, result = ?, updated_at = ? WHERE job_id = ?",
            (result.state, result.model_dump_json(), self._clock(), job_id),
        )

    def requeue(self, job_id: str) -> None:
        """Return a job to the queue -- the printer napped, this is not a failure."""
        self.mark(job_id, "queued")

    def result_for(self, job_id: str) -> JobResult | None:
        row = self._db.execute("SELECT result FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        if row is None or row["result"] is None:
            return None
        return JobResult.model_validate_json(row["result"])

    # -- label-level dedupe ------------------------------------------------- #

    def is_recent_dedupe(self, key: str) -> bool:
        return (
            self._db.execute("SELECT 1 FROM dedupe WHERE dedupe_key = ?", (key,)).fetchone() is not None
        )

    def record_dedupe(self, key: str, job_id: str) -> None:
        self._db.execute(
            "INSERT INTO dedupe (dedupe_key, job_id, printed_at) VALUES (?, ?, ?) "
            "ON CONFLICT(dedupe_key) DO UPDATE SET job_id = excluded.job_id, "
            "printed_at = excluded.printed_at",
            (key, job_id, self._clock()),
        )

    def purge_dedupe(self, ttl_s: float) -> int:
        """Opportunistic sweep -- called on insert, so no timer thread is needed."""
        cur = self._db.execute("DELETE FROM dedupe WHERE printed_at < ?", (self._clock() - ttl_s,))
        return cur.rowcount

    # -- per-label print progress ------------------------------------------ #
    #
    # Committed after each label prints, so a crash mid-job reprints at most the one
    # label that was in flight: recovery skips the indices already recorded here.

    def label_printed(self, job_id: str, index: int) -> None:
        self._db.execute(
            "INSERT OR IGNORE INTO printed_labels (job_id, label_index, printed_at) VALUES (?, ?, ?)",
            (job_id, index, self._clock()),
        )

    def printed_labels(self, job_id: str) -> set[int]:
        rows = self._db.execute(
            "SELECT label_index FROM printed_labels WHERE job_id = ?", (job_id,)
        ).fetchall()
        return {r["label_index"] for r in rows}
