"""Decode the printer's ``1A <tag> <payload>`` status frames.

The printer answers every query *and* pushes unsolicited state changes -- a media
error arrives with no preceding request. Framing is identical on SPP and BLE; only
the delivery differs (a byte stream vs discrete ``ff03`` notifications), which is why
this module knows nothing about transports.

**This is deliberately not a port of the vendor parser.**
``QuinPrinter.InstructionProcessor.process()`` is a byte-scanner that switches on the
tag and lets each branch decide how far to advance. Two branches get it wrong:

* ``case 22`` (``0x16``) consumes the tag and no payload, but ``VERIFY_PAPER`` really
  answers ``1a16 00 40 00 00``. The leftovers fall into the ``0x99`` handler, which
  reads ``0x40`` as a length, tries a 64-byte copy out of a 6-byte buffer, throws
  ``ArrayIndexOutOfBoundsException`` -- and the caller catches it and drops the rest
  of the buffer.
* ``case 9`` (``0x09``) advances the cursor only *if a callback is registered*, so
  with no listener the payload byte gets reparsed as a tag.

Unknown tags are not skipped there either; they fall into the same length-prefixed
reader. So we are table-driven on payload length, and an unknown tag costs its prefix
and tag plus a warning instead of silently eating the rest of the buffer.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

#: Every status frame starts with this. The vendor parser also tolerates a ``0x1B``
#: prefix in the same position.
STATUS_PREFIX = 0x1A
ALT_PREFIX = 0x1B

#: Per-write acknowledgement on BLE. Transport-level flow control, not a status frame
#: -- the transport consumes it for pacing and never passes it up here.
ACK = b"\x01\x01"

#: Sentinel for a payload whose length is carried in its first byte.
LENGTH_PREFIXED = -1


@dataclass(frozen=True, slots=True)
class TagSpec:
    name: str
    length: int
    decode: Callable[[bytes], object] | None = None
    note: str = ""


def _pct(b: bytes) -> int:
    return b[0]


def _decivolts(b: bytes) -> float:
    """Battery terminal voltage, big-endian in 10mV units.

    Worth having alongside ``battery``: that one pins at 100% on charge, while this
    tracked 4.16 -> 4.17V over a few seconds on the bench and read 4.09V on a
    discharged unit's self-test page.
    """
    return int.from_bytes(b, "big") / 100


def _version(b: bytes) -> str:
    return ".".join(str(x) for x in b)


def _serial(b: bytes) -> str:
    # The vendor replaces any non-alphanumeric byte with '8' before decoding, which
    # turns a corrupt serial into a plausible one. We decode leniently but do not
    # launder it -- a caller comparing against a model table should see the damage.
    return b.decode("ascii", errors="replace").rstrip("\x00")


#: Payload lengths taken from the vendor parser's 29 branches. ``✓`` in the note
#: marks tags we have observed on hardware.
TAGS: dict[int, TagSpec] = {
    0x03: TagSpec("hot_state", 1, lambda b: b[0], "✓ 0xA8 = OK"),
    0x04: TagSpec("battery", 1, _pct, "✓ percent"),
    0x05: TagSpec("cover_state", 1, lambda b: b[0], "✓"),
    0x06: TagSpec("paper_state", 1, lambda b: PaperState(b[0]), "✓ bit0 = paper OK"),
    0x07: TagSpec("firmware", 3, _version, "✓ maj.min.patch"),
    0x08: TagSpec("serial", 15, _serial, "✓ first 4 chars identify the model"),
    0x09: TagSpec("auto_power", 1, lambda b: b[0] * 5, "✓ value is units of 5 minutes"),
    0x0A: TagSpec("crc32", 4, None, "width varies with buffer size in the vendor app"),
    0x0B: TagSpec("print_cancelled", 1),
    0x0C: TagSpec("label_type", 1, lambda b: b[0], "✓"),
    0x0E: TagSpec("p1000_state", 1),
    0x0F: TagSpec("print_complete", 1, lambda b: b[0], "✓ arrives ~2.4s after the raster"),
    0x11: TagSpec("hardware_version", 3, _version, "✓ HARDWARE_VERSION reply; 01 00 03 -> 1.0.3"),
    0x15: TagSpec("consumable_remaining", 3, None, "ribbon / RFID / carbon belt"),
    0x16: TagSpec(
        "reset_paper_ok", 4, None, "✓ VERIFY_PAPER ack; the vendor app discards these 4 bytes"
    ),
    0x17: TagSpec("bt_chip_type", 1, lambda b: b[0], "✓"),
    0x20: TagSpec("unknown_20", 1),
    0x2D: TagSpec("sensor_info", 13, None, "✓ SENSOR_INFO reply; field layout not decoded"),
    0x2F: TagSpec("voltage_v", 2, _decivolts, "✓ VOLTAGE reply; big-endian, 10mV units"),
    0x31: TagSpec("rfid_number", 3),
    0x35: TagSpec("charging", 1, lambda b: b[0] == 2),
    0x3B: TagSpec(
        "capabilities", 4, lambda b: Capabilities(b), "feature bits; the most valuable unobserved tag"
    ),
    0x3C: TagSpec("unknown_3c", 0),
    0x3E: TagSpec("print_busy", 1, lambda b: b[0] != 0),
    0x3F: TagSpec("material_error", 1, lambda b: b[0]),
    0x40: TagSpec("rfid_media", 15, None, "material no, colours, lamination, paper type, L x W"),
    0x4B: TagSpec("date_format", 2),
    0x4E: TagSpec("firmware_write_result", 7),
    0x5E: TagSpec("power_key_type", 1, lambda b: b[0]),
    0x99: TagSpec("consumable_uid", LENGTH_PREFIXED, lambda b: b.hex().upper()),
    0xA1: TagSpec("low_battery_a1", 0),
    0xA2: TagSpec("low_battery_a2", 0),
    0xA3: TagSpec("low_battery", 1, _pct),
}

#: The vendor's ``case 22`` reads zero payload bytes here, which is what desyncs its
#: parser -- the real frame carries four. Kept as a named constant so the discrepancy
#: is greppable rather than folklore.
VENDOR_RESET_PAPER_LENGTH = 0
OBSERVED_RESET_PAPER_LENGTH = 4


@dataclass(frozen=True, slots=True)
class PaperState:
    """The ``0x06`` bitfield. Only bit 0 is decoded; the rest are unmapped."""

    raw: int

    @property
    def ok(self) -> bool:
        """False when the media is missing or mis-fed.

        Observed live: ``0x89`` -> ``0x88`` when the stripe was pulled and back to
        ``0x89`` when it was replaced, unprompted both times.
        """
        return bool(self.raw & 0x01)

    def __str__(self) -> str:
        return f"paper={'ok' if self.ok else 'ERROR'}(0x{self.raw:02x})"


@dataclass(frozen=True, slots=True)
class Capabilities:
    """The ``0x3B`` feature bits, from ``InsProcessor.D0/D1/D2`` in the vendor app.

    Never observed on our unit -- the vendor app queries it on some models and not
    on the D30 path -- so the decode is from the decompile only.
    """

    raw: bytes

    @property
    def d0(self) -> int:
        return self.raw[0] if self.raw else 0

    @property
    def supports_concentration(self) -> bool:
        return bool(self.d0 & 0x01)

    @property
    def supports_velocity(self) -> bool:
        return bool(self.d0 & 0x02)

    @property
    def supports_grayscale(self) -> bool:
        return bool(self.d0 & 0x04)

    @property
    def supports_compression(self) -> bool:
        return bool(self.d0 & 0x08)

    @property
    def supports_minilzo(self) -> bool:
        return bool(self.d0 & 0x10)

    @property
    def supports_double_dpi(self) -> bool:
        return bool(self.d0 & 0x40)


@dataclass(frozen=True, slots=True)
class StatusFrame:
    """One decoded ``1A <tag> <payload>`` frame."""

    tag: int
    name: str
    raw: bytes
    value: object | None = None

    def __str__(self) -> str:
        if self.value is not None:
            return f"{self.name}={self.value}"
        return f"{self.name}[{self.raw.hex()}]" if self.raw else self.name


class StatusParser:
    """Incremental, length-aware decoder.

    Feed it whatever arrives -- a whole BLE notification, or an arbitrary slice of
    the SPP byte stream -- and it yields complete frames, holding any partial tail
    until the rest turns up.
    """

    def __init__(self, *, strict: bool = False) -> None:
        #: Raise on an unknown tag rather than skipping it. Useful in tests.
        self.strict = strict
        self._buf = bytearray()
        #: Tags seen that we have no spec for, for probe runs against new firmware.
        self.unknown_tags: dict[int, int] = {}

    def feed(self, data: bytes) -> list[StatusFrame]:
        """Add bytes, return every frame that is now complete."""
        self._buf += data
        frames: list[StatusFrame] = []
        while True:
            frame, consumed = self._try_one()
            if consumed == 0:
                break
            del self._buf[:consumed]
            if frame is not None:
                frames.append(frame)
        return frames

    @property
    def pending(self) -> bytes:
        """Bytes held back awaiting the rest of a frame."""
        return bytes(self._buf)

    def _try_one(self) -> tuple[StatusFrame | None, int]:
        buf = self._buf
        if not buf:
            return None, 0

        # Resynchronise: drop anything before a plausible frame start. On SPP the
        # stream can begin mid-frame after a reconnect.
        if buf[0] not in (STATUS_PREFIX, ALT_PREFIX):
            nxt = _find_prefix(buf, 1)
            if nxt is None:
                log.debug("discarding %dB with no frame prefix", len(buf))
                return None, len(buf)
            log.debug("resyncing, discarding %dB", nxt)
            return None, nxt

        if len(buf) < 2:
            return None, 0  # need the tag

        tag = buf[1]
        spec = TAGS.get(tag)
        if spec is None:
            self.unknown_tags[tag] = self.unknown_tags.get(tag, 0) + 1
            if self.strict:
                raise ValueError(f"unknown status tag 0x{tag:02x}")
            # Drop the prefix *and* the tag. Dropping only the prefix would leave the
            # unknown tag at the head of the buffer, where it is not a prefix, so the
            # next pass would fall into the resync scan -- and that scan stops at the
            # first 0x1A/0x1B it finds, which inside an unknown payload could be a
            # data byte rather than a frame start. Consuming both bytes keeps the
            # damage to the frame we already could not read. Still bounded: never a
            # length-prefixed gamble, and never the rest of the buffer.
            log.warning("unknown status tag 0x%02x, skipping prefix and tag", tag)
            return None, 2

        if spec.length == LENGTH_PREFIXED:
            if len(buf) < 3:
                return None, 0
            n = buf[2]
            total = 3 + n
            if len(buf) < total:
                return None, 0
            payload = bytes(buf[3:total])
        else:
            total = 2 + spec.length
            if len(buf) < total:
                return None, 0
            payload = bytes(buf[2:total])

        value = None
        if spec.decode is not None:
            try:
                value = spec.decode(payload)
            except Exception:  # a malformed payload must not kill the stream
                log.warning("could not decode %s payload %s", spec.name, payload.hex())
        return StatusFrame(tag=tag, name=spec.name, raw=payload, value=value), total


def _find_prefix(buf: bytearray, start: int) -> int | None:
    for i in range(start, len(buf)):
        if buf[i] in (STATUS_PREFIX, ALT_PREFIX):
            return i
    return None


@dataclass
class DeviceState:
    """Everything the printer has told us on this connection.

    Both a live view (``paper_ok`` while printing) and an audit trail (``frames``).
    """

    acks: int = 0
    frames: list[StatusFrame] = field(default_factory=list)
    values: dict[str, object] = field(default_factory=dict)
    prints_completed: int = 0

    def ingest(self, frame: StatusFrame) -> None:
        self.frames.append(frame)
        if frame.value is not None:
            self.values[frame.name] = frame.value
        elif frame.raw:
            self.values[frame.name] = frame.raw
        if frame.name == "print_complete":
            self.prints_completed += 1

    @property
    def serial(self) -> str | None:
        v = self.values.get("serial")
        return v if isinstance(v, str) else None

    @property
    def model_prefix(self) -> str | None:
        """First four serial characters -- how the vendor resolves the model."""
        s = self.serial
        return s[:4] if s and len(s) >= 4 else None

    @property
    def firmware(self) -> str | None:
        v = self.values.get("firmware")
        return v if isinstance(v, str) else None

    @property
    def battery_pct(self) -> int | None:
        v = self.values.get("battery")
        return v if isinstance(v, int) else None

    @property
    def paper(self) -> PaperState | None:
        v = self.values.get("paper_state")
        return v if isinstance(v, PaperState) else None

    @property
    def paper_ok(self) -> bool | None:
        """``None`` when the printer has not reported, which is not the same as OK."""
        p = self.paper
        return None if p is None else p.ok

    @property
    def voltage_v(self) -> float | None:
        """Battery terminal voltage. More useful than ``battery_pct`` while charging."""
        v = self.values.get("voltage_v")
        return v if isinstance(v, float) else None

    @property
    def material_error(self) -> int | None:
        """The ``0x3F`` consumable/material code. Non-zero is a fault."""
        v = self.values.get("material_error")
        return v if isinstance(v, int) else None

    @property
    def print_cancelled(self) -> bool:
        """Whether the printer reported ``0x0B``.

        Observed when a raster is wider than the head: the D30 refuses the job rather
        than printing a truncated label, so this is the difference between "we sent
        bytes" and "it declined them".
        """
        return "print_cancelled" in self.values

    def fault(self) -> str | None:
        """What the printer is complaining about, or ``None`` if nothing.

        Only reports what it actually told us. A printer that has said nothing yields
        ``None`` here, which is not a clean bill of health -- silence and health are
        different states, and ``paper_ok`` stays ``None`` to keep them apart.

        Known asymmetry between the three: ``paper_state`` is re-queried on every
        connect (it is in the vendor session set), but ``material_error`` and
        ``print_cancelled`` only ever arrive unsolicited. Nothing can poll them --
        ``ALL_ERROR`` (``1f1128``) is the opcode that would, and it was verified inert
        on fw 2.1.2. So a material fault raised on one connection is not re-asserted on
        the next unless the printer volunteers it again, and the status falls back to
        whatever ``paper_state`` says. Worth knowing before trusting a clean fault() as
        proof the consumable is fine.
        """
        problems = []
        paper = self.paper
        if paper is not None and not paper.ok:
            problems.append(f"media not ready ({paper})")
        material = self.material_error
        if material:
            problems.append(f"material error 0x{material:02x}")
        if self.print_cancelled:
            problems.append("printer cancelled the print")
        return "; ".join(problems) or None

    def summary(self) -> str:
        parts = [f"{self.acks} acks"]
        if self.serial:
            parts.append(f"serial={self.serial}")
        if self.firmware:
            parts.append(f"fw={self.firmware}")
        if self.battery_pct is not None:
            parts.append(f"battery={self.battery_pct}%")
        if self.voltage_v is not None:
            parts.append(f"{self.voltage_v:.2f}V")
        if self.paper is not None:
            parts.append(str(self.paper))
        if self.prints_completed:
            parts.append(f"printed={self.prints_completed}")
        extra = [
            f"{k}={v}"
            for k, v in sorted(self.values.items())
            if k not in {"serial", "firmware", "battery", "paper_state", "voltage_v"}
        ]
        if extra:
            parts.append("[" + ", ".join(extra) + "]")
        return ", ".join(parts)
