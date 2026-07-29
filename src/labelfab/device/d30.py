"""The Phomemo D30 itself.

Two things worth knowing before changing anything here.

**There is no read channel.** Nothing is ever received from the printer. A
successful ``print_raster`` means the bytes were accepted by the socket and we
waited out the physical print duration. Out-of-tape, jams and low battery are
undetectable.

**Long strips need pacing.** The printer streams rather than buffers. A 20-label
strip is ~96KB, far more than it can hold, so writes are throttled to roughly the
speed the head can consume them. Discrete single labels never hit this, which is
precisely why it will not show up until someone prints a real batch.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from labelfab.contract import PRINT_SPEED_MM_S, PX_PER_MM
from labelfab.device.errors import D30GeometryError, D30NotReady
from labelfab.device.escpos import INIT_PACKETS, MAX_FRAME_LINES, print_header
from labelfab.device.transport import Transport
from labelfab.render.raster import DeviceRaster

MODEL = "phomemo-d30"

#: Lines the head prints per second, from the 60mm/s spec.
LINES_PER_SECOND = PRINT_SPEED_MM_S * PX_PER_MM


@dataclass(frozen=True, slots=True)
class D30Config:
    """Timing and geometry knobs, all of them hardware-verified on day one."""

    #: Bytes per write to the socket. Small enough to pace, large enough to be cheap.
    chunk_bytes: int = 4096
    #: Multiplier on the theoretical print time per chunk. Above 1.0 leaves the head
    #: room to stay ahead of the socket; tune it down until a long strip garbles.
    pace_factor: float = 1.2
    #: Pause between the seven init packets. The printer is fussy about framing.
    inter_packet_delay_s: float = 0.02
    #: Extra settle time after the last byte of a frame, on top of the computed
    #: print duration. Guards the next job from starting while tape is still moving.
    post_print_margin_s: float = 0.3
    #: Send a short blank feed on the first print after a wake. Some units print the
    #: first label faint otherwise; confirmed or ruled out during bring-up.
    wake_dummy_feed: bool = False


@dataclass
class PhomemoD30:
    """Drives one printer over one transport.

    Reconnection is *not* handled here. The caller decides whether a
    ``D30ConnectError`` means retry, stall or fail, because that decision needs
    the job's deadline and the batch mode, neither of which belong in a device
    driver.
    """

    transport: Transport
    config: D30Config = field(default_factory=D30Config)
    #: Injected so tests can run with no wall clock.
    sleep: Callable[[float], None] = time.sleep

    _initialised: bool = field(default=False, init=False, repr=False)

    @property
    def model(self) -> str:
        return MODEL

    @property
    def is_connected(self) -> bool:
        return self.transport.is_open and self._initialised

    @property
    def feedback(self):
        """Device feedback if the transport has a read channel (BLE), else ``None``.

        SPP is write-only, so this is ``None`` there; over BLE it exposes the notify
        channel's accumulated ACKs and status fields (serial, telemetry)."""
        return getattr(self.transport, "feedback", None)

    def connect(self) -> None:
        """Open the transport and run the captured session-setup sequence."""
        self.transport.open()
        for packet in INIT_PACKETS:
            self.transport.write(packet)
            self.transport.flush()
            if self.config.inter_packet_delay_s:
                self.sleep(self.config.inter_packet_delay_s)
        self._initialised = True

    def close(self) -> None:
        """Idempotent, and never raises."""
        self._initialised = False
        try:
            self.transport.close()
        except Exception:
            pass

    def __enter__(self) -> PhomemoD30:
        self.connect()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def print_duration_s(self, raster: DeviceRaster) -> float:
        return raster.height_px / LINES_PER_SECOND

    def print_raster(self, raster: DeviceRaster, *, wait: bool = True) -> None:
        """Send one ``GS v 0`` frame.

        A whole strip goes in a single frame. That is the entire economy of strip
        mode: one leader and one trailer feed for the batch rather than per label.
        """
        if not self.is_connected:
            raise D30NotReady("connect() before printing")
        if raster.height_px > MAX_FRAME_LINES:
            raise D30GeometryError(
                f"{raster.height_px} lines exceeds one frame ({MAX_FRAME_LINES}); "
                f"split the strip into shorter runs"
            )

        header = print_header(raster.width_bytes, raster.height_px)
        self.transport.write(header)
        self.transport.flush()

        if self.config.wake_dummy_feed:
            self.sleep(0.1)

        for offset in range(0, len(raster.data), self.config.chunk_bytes):
            chunk = raster.data[offset : offset + self.config.chunk_bytes]
            self.transport.write(chunk)
            self.transport.flush()
            self._pace(len(chunk), raster.width_bytes)

        if wait:
            # No acknowledgement exists, so the only way to know the print finished
            # is to wait as long as it physically takes.
            self.sleep(self.print_duration_s(raster) + self.config.post_print_margin_s)

    def _pace(self, chunk_len: int, width_bytes: int) -> None:
        """Throttle to roughly the head's consumption rate."""
        if self.config.pace_factor <= 0:
            return
        lines = chunk_len / max(1, width_bytes)
        delay = lines / LINES_PER_SECOND / self.config.pace_factor
        if delay > 0:
            self.sleep(delay)

    def self_test(self, width_px: int, height_px: int) -> DeviceRaster:
        """Build an alignment pattern: a 1px border plus rules every 8px.

        One print of this answers four bring-up questions at once -- effective head
        width, tape offset, rotation and mirroring -- because a complete border
        means the full width printed and the rules make orientation obvious.
        """
        from PIL import Image, ImageDraw

        if width_px % 8:
            raise D30GeometryError(f"width {width_px}px is not a whole number of bytes")

        img = Image.new("1", (width_px, height_px), color=0)
        d = ImageDraw.Draw(img)
        d.rectangle([0, 0, width_px - 1, height_px - 1], outline=1)
        for x in range(0, width_px, 8):
            d.line([(x, 0), (x, 5)], fill=1)
        for y in range(0, height_px, 8):
            d.line([(0, y), (5, y)], fill=1)
        # Asymmetric corner marker: makes 180-degree rotation unambiguous.
        d.rectangle([2, 2, 10, 6], fill=1)
        return DeviceRaster(width_px=width_px, height_px=height_px, data=img.tobytes())
