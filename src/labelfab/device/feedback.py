"""Accumulated printer feedback for one connection.

Both transports deliver the same ``1A <tag> <payload>`` status frames; only the
delivery differs. On BLE they arrive as discrete ``ff03`` notifications interleaved
with per-write ``0101`` ACKs; on SPP they arrive as an unframed byte stream. This
class sits above that split -- it takes whatever the transport hands it, runs it
through :class:`~labelfab.device.responses.StatusParser`, and exposes decoded state.

``0101`` is transport-level flow control and ``1a…`` is protocol-level status. Keeping
that line clear is what lets the driver above treat SPP and BLE identically: the BLE
transport consumes ACKs to pace itself, and everything else looks the same.

Historically this decoded exactly one field (the serial) and kept the rest as raw hex.
The vendor parser turned out to handle 29 tags; 11 are confirmed on hardware and all
of them are now named in ``responses.TAGS``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from labelfab.device.responses import (
    ACK,
    STATUS_PREFIX,
    DeviceState,
    PaperState,
    StatusFrame,
    StatusParser,
)

#: Status field id whose value is the ASCII serial number. Kept for callers that
#: predate the named-tag table.
FIELD_SERIAL = 0x08

__all__ = ["ACK", "FIELD_SERIAL", "STATUS_PREFIX", "DeviceFeedback", "PaperState", "StatusFrame"]


@dataclass
class DeviceFeedback:
    """Notifications accumulated over one print/connection.

    Feed it either whole notifications (BLE) or arbitrary stream slices (SPP);
    :class:`StatusParser` holds partial frames until they complete, so callers never
    have to reassemble.
    """

    state: DeviceState = field(default_factory=DeviceState)
    parser: StatusParser = field(default_factory=StatusParser)

    # -- ingestion ---------------------------------------------------------- #

    def ingest(self, data: bytes) -> list[StatusFrame]:
        """Absorb bytes from the transport, returning any newly completed frames.

        A bare ``0101`` is counted as an ACK and not parsed further. Anything else
        goes to the length-aware parser.
        """
        data = bytes(data)
        if data == ACK:
            self.state.acks += 1
            return []
        frames = self.parser.feed(data)
        for f in frames:
            self.state.ingest(f)
        return frames

    # -- decoded view ------------------------------------------------------- #

    @property
    def acks(self) -> int:
        return self.state.acks

    @property
    def serial(self) -> str | None:
        return self.state.serial

    @property
    def model_prefix(self) -> str | None:
        """First four serial characters -- how the vendor resolves the model."""
        return self.state.model_prefix

    @property
    def firmware(self) -> str | None:
        return self.state.firmware

    @property
    def battery_pct(self) -> int | None:
        return self.state.battery_pct

    @property
    def paper_ok(self) -> bool | None:
        """``None`` means the printer has not reported -- not the same as OK.

        This flips unprompted: pulling the stripe produced ``1a0688`` and replacing
        it produced ``1a0689``, with no query in between.
        """
        return self.state.paper_ok

    @property
    def prints_completed(self) -> int:
        """Count of ``0x0F`` frames -- the printer's own "I finished" signal.

        Before this was decoded, a completed print could only be inferred by waiting
        out the physical print duration.
        """
        return self.state.prints_completed

    @property
    def frames(self) -> list[StatusFrame]:
        return self.state.frames

    @property
    def fields(self) -> dict[int, bytes]:
        """Raw tag -> payload, for callers written against the pre-table API."""
        return {f.tag: f.raw for f in self.state.frames}

    @property
    def unknown_tags(self) -> dict[int, int]:
        """Tags with no spec, and how often each was seen.

        Non-empty here means either a firmware revision we have not characterised or
        a parser bug -- worth surfacing rather than swallowing.
        """
        return self.parser.unknown_tags

    def summary(self) -> str:
        return self.state.summary()
