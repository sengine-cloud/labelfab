"""Decode the D30's BLE notify (ff03) frames into structured feedback.

Discovered on hardware: the BLE link -- unlike SPP -- has a real read channel. During
a print the printer streams two kinds of notification on ``ff03``:

* a per-packet **ACK** (``0101``) -- one for roughly every write, so it can gate the
  next write and pace a print to the printer's actual consumption rate; and
* one-field **status frames** prefixed ``0x1a``: ``1a <field-id> <value...>``, e.g.
  field ``0x08`` carries the serial number as ASCII (``Q223P4C31420105``). The other
  fields are telemetry whose meaning is not yet decoded, so they are kept raw.

This turns the SPP-era "we hope it printed" into "the printer acknowledged N packets
and reported these fields", which the agent surfaces as real status.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: Per-packet acknowledgement the printer emits while consuming a frame.
ACK = b"\x01\x01"
#: One-field status frames start with this byte.
STATUS_PREFIX = 0x1A
#: Status field id whose value is the ASCII serial number.
FIELD_SERIAL = 0x08


@dataclass
class DeviceFeedback:
    """Accumulated notifications for one print/connection."""

    acks: int = 0
    serial: str | None = None
    #: Decoded status fields: field-id -> raw value bytes (serial also parsed out).
    fields: dict[int, bytes] = field(default_factory=dict)

    def ingest(self, frame: bytes) -> None:
        frame = bytes(frame)
        if frame == ACK:
            self.acks += 1
        elif len(frame) >= 2 and frame[0] == STATUS_PREFIX:
            field_id, value = frame[1], frame[2:]
            self.fields[field_id] = value
            if field_id == FIELD_SERIAL:
                try:
                    self.serial = value.decode("ascii").rstrip("\x00") or None
                except UnicodeDecodeError:
                    pass

    def summary(self) -> str:
        parts = [f"{self.acks} acks"]
        if self.serial:
            parts.append(f"serial={self.serial}")
        extra = [
            f"{fid:#04x}={val.hex()}"
            for fid, val in sorted(self.fields.items())
            if fid != FIELD_SERIAL
        ]
        if extra:
            parts.append("fields[" + ",".join(extra) + "]")
        return ", ".join(parts)
