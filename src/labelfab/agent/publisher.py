"""Where results and status go.

A narrow sink so the worker never touches a broker directly: the MQTT source
implements this, and tests pass a list-backed fake. Nothing consumes ``results`` in
v1 -- ``mosquitto_sub -t 'se/v1/print/#' -v`` is the whole observability story -- but
publishing anyway costs nothing and makes that story exist.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from labelfab.contract import JobResult, PrinterStatus


@runtime_checkable
class Publisher(Protocol):
    def publish_result(self, result: JobResult) -> None: ...

    def publish_status(self, status: PrinterStatus, *, retain: bool = True) -> None: ...

    def publish_progress(self, job_id: str, printed: int, total: int) -> None: ...


class NullPublisher:
    """Discards everything. Used by the dir-only mode, which has no broker."""

    def publish_result(self, result: JobResult) -> None:
        pass

    def publish_status(self, status: PrinterStatus, *, retain: bool = True) -> None:
        pass

    def publish_progress(self, job_id: str, printed: int, total: int) -> None:
        pass


class RecordingPublisher:
    """Keeps everything in lists so tests can assert what was published."""

    def __init__(self) -> None:
        self.results: list[JobResult] = []
        self.statuses: list[PrinterStatus] = []
        self.progress: list[tuple[str, int, int]] = []

    def publish_result(self, result: JobResult) -> None:
        self.results.append(result)

    def publish_status(self, status: PrinterStatus, *, retain: bool = True) -> None:
        self.statuses.append(status)

    def publish_progress(self, job_id: str, printed: int, total: int) -> None:
        self.progress.append((job_id, printed, total))
