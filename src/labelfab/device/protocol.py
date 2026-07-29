"""The D30 command set, independent of how the bytes travel.

The printer is dual-mode -- Classic SPP (RFCOMM) and BLE GATT -- and the command
encoding is byte-identical on both. This module is that encoding; the transports
below it differ only in framing and flow control.

Three prefixes cover everything:

* ``1F 11`` -- vendor control plane, get/set device state
* ``1B 4E`` -- vendor config plane, persistent defaults
* ESC/POS  -- ``1B 40`` init, ``1D 76 30 00`` raster, ``1B 64`` feed

Every command carries a :class:`Support` level so "we have seen this work" is data
rather than folklore. ``VERIFIED`` means observed on the wire against a real D30;
``DECOMPILED`` means it exists in the vendor's tables and nothing more. Do not put a
``DECOMPILED`` command on a default code path.

Source of truth: ``~/Documents/quyin-printer-protocol.md`` (opcode reference) and
``HARDWARE-NOTES.md`` (what the hardware actually did).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

CTRL = b"\x1f\x11"  #: vendor control-plane prefix
CFG = b"\x1b\x4e"  #: vendor config-plane prefix


class Support(Enum):
    """How much we actually know about a command."""

    #: Observed on the wire against a real D30 and behaved as documented.
    VERIFIED = "verified"
    #: Reachable from the vendor app's D30 code path, but never captured.
    REACHABLE = "reachable"
    #: Present in the vendor opcode tables and called from nowhere in the app.
    #: Firmware surface with no UI. Plausible, untested.
    DECOMPILED = "decompiled"


class Danger(Enum):
    """Consequence of getting a command wrong."""

    SAFE = "safe"  #: read-only or trivially reversible
    STATEFUL = "stateful"  #: persists across power cycles
    DESTRUCTIVE = "destructive"  #: can brick or permanently misidentify the unit


@dataclass(frozen=True, slots=True)
class Command:
    """One opcode, plus everything we know about whether it is safe to send."""

    name: str
    opcode: bytes
    support: Support
    danger: Danger = Danger.SAFE
    #: Number of argument bytes the command takes, or ``None`` for variable.
    args: int | None = 0
    note: str = ""

    def __call__(self, *values: int) -> bytes:
        """Build the wire bytes, validating the argument count."""
        if self.args is not None and len(values) != self.args:
            raise ValueError(f"{self.name} takes {self.args} argument(s), got {len(values)}")
        for v in values:
            if not 0 <= v <= 0xFF:
                raise ValueError(f"{self.name} argument {v} is not a byte")
        return self.opcode + bytes(values)

    @property
    def is_safe_to_probe(self) -> bool:
        """True if sending this cannot change persistent state."""
        return self.danger is Danger.SAFE


def _ctrl(sub: int) -> bytes:
    return CTRL + bytes([sub])


def _cfg(sub: int) -> bytes:
    return CFG + bytes([sub])


# --------------------------------------------------------------------------- #
# Queries -- all read-only, all safe to probe.
# --------------------------------------------------------------------------- #

V, R, D = Support.VERIFIED, Support.REACHABLE, Support.DECOMPILED

FIRMWARE_VERSION = Command("FIRMWARE_VERSION", _ctrl(0x07), V, note="-> 0x07, 3 bytes maj.min.patch")
BATTERY = Command("BATTERY", _ctrl(0x08), V, note="-> 0x04, percent")
SERIAL = Command("SERIAL", _ctrl(0x09), V, note="-> 0x08, 15 ASCII; first 4 chars identify the model")
AUTO_POWER_TIME = Command("AUTO_POWER_TIME", _ctrl(0x0E), V, note="-> 0x09, units of 5 minutes")
UNKNOWN_0A = Command(
    "UNKNOWN_0A", _ctrl(0x0A), V, note="sent once per print job by both vendor apps; meaning unknown"
)
PAPER_STATE = Command("PAPER_STATE", _ctrl(0x11), V, note="-> 0x06, bit0 = paper OK")
COVER_STATE = Command("COVER_STATE", _ctrl(0x12), V, note="-> 0x05")
HOT_STATE = Command("HOT_STATE", _ctrl(0x13), V, note="-> 0x03, 0xA8 = OK")
LABEL_TYPE = Command("LABEL_TYPE", _ctrl(0x19), V, note="-> 0x0C")
CHIP_TYPE = Command("CHIP_TYPE", _ctrl(0x38), V, note="-> 0x17")
POWER_KEY_TYPE = Command("POWER_KEY_TYPE", _ctrl(0x65), V, note="-> 0x5E")
DATE_FORMAT = Command(
    "DATE_FORMAT", _ctrl(0x4B), V, note="-> 0x4B; opcode collides with BT_LOSS_TEST_RESULT"
)
BT_LOSS_TEST = Command("BT_LOSS_TEST", _ctrl(0x4A), V, note="collides with GET_DATE_TITLE")

#: Never sent by the vendor app but the single most useful unknown: it would settle
#: whether the head is 96 or 120 dots without printing a test label.
LABEL_WIDTH = Command("LABEL_WIDTH", _ctrl(0x18), D, note="head width; would settle the 15mm question")
ALL_ERROR = Command(
    "ALL_ERROR", _ctrl(0x28), D, note="comprehensive error word; would decode the PAPER_STATE bits"
)
HARDWARE_VERSION = Command("HARDWARE_VERSION", _ctrl(0x33), D)
COMM_VERSION = Command(
    "COMM_VERSION", _ctrl(0x34), D, note="protocol version; useful for feature gating"
)
SENSOR_INFO = Command("SENSOR_INFO", _ctrl(0x1D), D)
SENSOR_HEAT = Command("SENSOR_HEAT", _ctrl(0x3A), D)
VOLTAGE = Command("VOLTAGE", _ctrl(0x1F), D)
CHARGE_MODE = Command("CHARGE_MODE", _ctrl(0x43), D)
COMPRESS_TYPE = Command("COMPRESS_TYPE", _ctrl(0x51), D, note="whether minilzo raster is supported")
COMPRESS_SIZE = Command("COMPRESS_SIZE", _ctrl(0x36), D)
PRINT_BUSY = Command("PRINT_BUSY", _ctrl(0x54), R, note="-> 0x3E")
BT_MAC = Command("BT_MAC", _ctrl(0x20), D)
RFID_REMAIN = Command("RFID_REMAIN", _ctrl(0x22), R)
RFID_LABEL_INFO = Command("RFID_LABEL_INFO", _ctrl(0x31), R, note="-> 0x31")
QUERY_CONSUMABLES_UID = Command("QUERY_CONSUMABLES_UID", _ctrl(0x99), R, note="-> 0x99, length-prefixed")

#: Read-only set that is safe to sweep against an unknown unit.
PROBE_SET: tuple[Command, ...] = (
    SERIAL,
    FIRMWARE_VERSION,
    HARDWARE_VERSION,
    COMM_VERSION,
    BATTERY,
    PAPER_STATE,
    COVER_STATE,
    HOT_STATE,
    LABEL_TYPE,
    LABEL_WIDTH,
    CHIP_TYPE,
    AUTO_POWER_TIME,
    VOLTAGE,
    SENSOR_INFO,
    SENSOR_HEAT,
    CHARGE_MODE,
    COMPRESS_TYPE,
    ALL_ERROR,
    PRINT_BUSY,
)

# --------------------------------------------------------------------------- #
# Print path -- verified end to end against the hardware.
# --------------------------------------------------------------------------- #

PRINT_DENSITY = Command("PRINT_DENSITY", _ctrl(0x02), V, args=1, note="1 light / 2 medium / 4 heavy")
LEFT_MARGIN = Command(
    "LEFT_MARGIN", _ctrl(0x24), V, args=1, note="in BYTES, not dots; = head_bytes - width_bytes"
)
PRINT_MULTI = Command(
    "PRINT_MULTI", _ctrl(0x21), V, args=1, note="copy count; one raster serves N labels"
)
EXIT_COMPRESS_MODE = Command(
    "EXIT_COMPRESS_MODE", _ctrl(0x35), V, args=1, note="send 0; the iOS app does this defensively"
)
INIT_PRINTER = Command("INIT_PRINTER", b"\x1b\x40", V, note="ESC @")
PRINT_IMAGE = Command(
    "PRINT_IMAGE", b"\x1d\x76\x30\x00", V, args=None, note="GS v 0; followed by xL xH yL yH then raster"
)

#: Density values, matching ``D30Constant.TYPE_CONCENTRATION_*`` and confirmed on the
#: wire by printing one label at each.
DENSITY_LIGHT = 1
DENSITY_MEDIUM = 2
DENSITY_HEAVY = 4
DENSITIES = (DENSITY_LIGHT, DENSITY_MEDIUM, DENSITY_HEAVY)

#: Feed after each label on the *continuous* path. The vendor sends ESC d 23, about
#: 2.88mm at 203dpi. Unverified here: both captured jobs took the die-cut path.
PRINT_AND_FEED = Command(
    "PRINT_AND_FEED", b"\x1b\x64", D, args=1, note="ESC d n; vendor uses n=23 on continuous media"
)
VENDOR_FEED_LINES = 23

FEED_PAPER = Command("FEED_PAPER", _ctrl(0x32), D, note="feed without printing")
PRINT_TEST_PAGE = Command(
    "PRINT_TEST_PAGE", _ctrl(0x27), D, note="built-in self test; no rasteriser needed"
)
PAPER_LEARN = Command("PAPER_LEARN", _ctrl(0x1E), D, note="gap detection for die-cut media")
AUTO_LOCATE = Command("AUTO_LOCATE", _ctrl(0x25), D)
VERIFY_PAPER = Command(
    "VERIFY_PAPER",
    _cfg(0x10),
    V,
    note="the app's fix-error 'reset' button; answers 0x16 plus 4 bytes the vendor discards",
)

# --------------------------------------------------------------------------- #
# Configuration -- persists across power cycles.
# --------------------------------------------------------------------------- #

#: Auto power-off, in units of 5 minutes. 0 = never. Confirmed across both vendor
#: apps: 10m -> 0x02, 30m -> 0x06, 60m -> 0x0C, 10h -> 0x78.
AUTO_SHUTDOWN_TIME = Command(
    "AUTO_SHUTDOWN_TIME", _cfg(0x07), V, Danger.STATEFUL, args=1, note="units of 5 min, 0 = never"
)
AUTO_POWER_NEVER = 0
PRINT_SPEED = Command("PRINT_SPEED", _ctrl(0x23), R, Danger.STATEFUL, args=1)
PAPER_TYPE = Command("PAPER_TYPE", CTRL, R, Danger.STATEFUL, args=1)
SET_POWER_KEY_TYPE = Command("SET_POWER_KEY_TYPE", _cfg(0x25), R, Danger.STATEFUL, args=1)
SHUTDOWN = Command("SHUTDOWN", _ctrl(0x42), D, Danger.STATEFUL, note="remote power off")
HEART_BEAT = Command(
    "HEART_BEAT",
    b"\x1a\x18\x01",
    D,
    note="in the vendor tables, sent by neither vendor app; may or may not defeat sleep",
)


def auto_shutdown_minutes(minutes: int) -> bytes:
    """``AUTO_SHUTDOWN_TIME`` from whole minutes. 0 disables auto power-off."""
    if minutes < 0:
        raise ValueError("minutes must not be negative")
    if minutes % 5:
        raise ValueError(f"{minutes}min is not a multiple of the 5-minute unit")
    units = minutes // 5
    if units > 0xFF:
        raise ValueError(f"{minutes}min exceeds the one-byte field (max {0xFF * 5}min)")
    return AUTO_SHUTDOWN_TIME(units)


# --------------------------------------------------------------------------- #
# Never send these.
# --------------------------------------------------------------------------- #

#: Rewrites the serial number. The first four characters are what identify the unit
#: as a D30; corrupting them breaks model resolution permanently and silently -- the
#: vendor parser even sanitises non-alphanumeric serial bytes to ``'8'``, so a
#: mangled serial still looks plausible.
DEVICE_ID = Command("DEVICE_ID", _cfg(0x08), R, Danger.DESTRUCTIVE, args=None)
OTA_MODE = Command("OTA_MODE", _ctrl(0x0F), D, Danger.DESTRUCTIVE)
FIRMWARE_UPGRADE_START = Command("FIRMWARE_UPGRADE_START", _ctrl(0x14), R, Danger.DESTRUCTIVE)
FIRMWARE_UPGRADE_CONFIRM = Command("FIRMWARE_UPGRADE_CONFIRM", _ctrl(0x15), R, Danger.DESTRUCTIVE)
FIRMWARE_UPGRADE_CANCEL = Command("FIRMWARE_UPGRADE_CANCEL", _ctrl(0x16), R, Danger.DESTRUCTIVE)
SET_CRIMP_MODE = Command("SET_CRIMP_MODE", _ctrl(0x88), D, Danger.DESTRUCTIVE)
MATERIAL_CONFIG = Command("MATERIAL_CONFIG", _ctrl(0x39), D, Danger.DESTRUCTIVE)

FORBIDDEN: frozenset[str] = frozenset(
    {
        DEVICE_ID.name,
        OTA_MODE.name,
        FIRMWARE_UPGRADE_START.name,
        FIRMWARE_UPGRADE_CONFIRM.name,
        FIRMWARE_UPGRADE_CANCEL.name,
        SET_CRIMP_MODE.name,
        MATERIAL_CONFIG.name,
    }
)

# --------------------------------------------------------------------------- #
# Session setup.
# --------------------------------------------------------------------------- #

#: Queries both vendor apps issue on connect. The Android app batches several into
#: one write (``1f11381f11121f11131f1107``) and the printer answers them in order, so
#: the long-held belief that the printer is "picky about framing" and needs one write
#: per packet is not supported by the captures.
SESSION_QUERIES: tuple[Command, ...] = (
    CHIP_TYPE,
    COVER_STATE,
    HOT_STATE,
    SERIAL,
    PAPER_STATE,
    LABEL_TYPE,
    FIRMWARE_VERSION,
    BATTERY,
    AUTO_POWER_TIME,
)


def session_setup(*, density: int = DENSITY_MEDIUM, batched: bool = False) -> tuple[bytes, ...]:
    """Connect-time sequence: identify the printer, then set density.

    ``batched=True`` coalesces the queries into a single write, which is what the
    Android app does. The default keeps one write per query -- marginally slower,
    and the conservative choice until batching is confirmed on our own unit.
    """
    if density not in DENSITIES:
        raise ValueError(f"density {density} is not one of {DENSITIES}")
    queries = [c() for c in SESSION_QUERIES]
    packets = [b"".join(queries)] if batched else queries
    return (*packets, UNKNOWN_0A() + PRINT_DENSITY(density))


# --------------------------------------------------------------------------- #
# Raster framing.
# --------------------------------------------------------------------------- #

#: ``yL/yH`` is a 16-bit little-endian line count, so one frame tops out here. At
#: 203dpi that is 8.2m -- longer than a 20ft roll, which is what makes whole-batch
#: strip printing possible in a single frame.
MAX_FRAME_LINES = 0xFFFF
MAX_FRAME_BYTES_PER_LINE = 0xFFFF

#: Head width in bytes. ``D30Printer.MAX_PRINT_WIDTH`` is a hardcoded static in the
#: vendor app, never reassigned, and 12 was confirmed on the wire (96 dots).
HEAD_WIDTH_BYTES = 12


def raster_header(width_bytes: int, height_px: int) -> bytes:
    """``GS v 0`` plus the little-endian ``xL xH yL yH`` dimensions."""
    if not 0 < width_bytes <= MAX_FRAME_BYTES_PER_LINE:
        raise ValueError(f"width_bytes {width_bytes} out of range")
    if not 0 < height_px <= MAX_FRAME_LINES:
        raise ValueError(
            f"{height_px} lines exceeds the 16-bit yL/yH field (max {MAX_FRAME_LINES}, "
            f"about {MAX_FRAME_LINES / 7.992:.0f}mm); split the strip"
        )
    return PRINT_IMAGE.opcode + width_bytes.to_bytes(2, "little") + height_px.to_bytes(2, "little")


def left_margin_bytes(width_bytes: int, head_width_bytes: int = HEAD_WIDTH_BYTES) -> int:
    """Letterbox offset for a raster narrower than the head.

    The vendor computes ``12 - width/8`` and sends it as ``LEFT_MARGIN``. Earlier
    notes treated the captured ``1f112400`` as opaque framing that must never change;
    it is this command with the argument that happens to be 0 for a full-width label.
    Hardcoding the 0 puts every narrower label hard against one edge.

    We have definitively verified that the physical D30 print head is exactly
    96 dots (12 bytes) wide, centered in the tape path. Even with 15mm tape
    loaded, attempting to print wider than 12 bytes causes the printer to reject
    the job with a `print_cancelled` error. Therefore, `HEAD_WIDTH_BYTES` (12)
    is the absolute hardware maximum and the correct default.
    """
    return max(0, head_width_bytes - width_bytes)


def print_preamble(
    width_bytes: int,
    height_px: int,
    *,
    density: int | None = None,
    copies: int = 1,
    head_width_bytes: int = HEAD_WIDTH_BYTES,
) -> bytes:
    """Everything that precedes the raster body, in the vendor's order.

    Both vendor apps agree on density, ``UNKNOWN_0A`` and ``ESC @``; they differ on
    the rest (Android sends ``LEFT_MARGIN``, iOS sends ``PRINT_MULTI`` and
    ``EXIT_COMPRESS_MODE``). Both print correctly, so the preamble is tolerant. We
    send the union, which is a superset of two known-good sequences.
    """
    if copies < 1:
        raise ValueError("copies must be at least 1")
    if copies > 0xFF:
        raise ValueError(f"copies {copies} exceeds the one-byte field")
    out = bytearray()
    if density is not None:
        if density not in DENSITIES:
            raise ValueError(f"density {density} is not one of {DENSITIES}")
        out += UNKNOWN_0A() + PRINT_DENSITY(density)
    out += LEFT_MARGIN(left_margin_bytes(width_bytes, head_width_bytes))
    if copies > 1:
        out += PRINT_MULTI(copies)
    out += INIT_PRINTER()
    out += EXIT_COMPRESS_MODE(0)
    out += raster_header(width_bytes, height_px)
    return bytes(out)
