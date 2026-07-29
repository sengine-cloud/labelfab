"""The Phomemo D30 itself.

Three things worth knowing before changing anything here.

**There is a read channel, on both transports.** The printer answers every query and
also pushes unsolicited state changes -- pulling the tape produces a media-error frame
with no request behind it. ``print_raster`` can therefore wait for the printer's own
``print_complete`` (``0x0F``) rather than sleeping out the physical duration and hoping.
The timer remains as a fallback for a link that reports nothing.

**Long strips need pacing, but only on BLE.** Over SPP the RFCOMM layer fragments and
credits automatically; the 2,888-byte print write we captured reached the air as
662-byte fragments the app never saw. Over BLE there is no such help, so the transport
gates each write on the printer's per-packet ``0101`` ACK.

**The preamble is tolerant.** The Android and iOS apps send different ones -- Android
``LEFT_MARGIN``, iOS ``PRINT_MULTI`` + ``EXIT_COMPRESS_MODE`` -- and both print. We send
the union, a superset of two known-good sequences.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from labelfab.contract import PRINT_SPEED_MM_S, PX_PER_MM
from labelfab.device.errors import D30GeometryError, D30NotReady
from labelfab.device.protocol import (
    DENSITIES,
    DENSITY_MEDIUM,
    MAX_FRAME_LINES,
    print_preamble,
    session_setup,
)
from labelfab.device.transport import Transport
from labelfab.render.raster import DeviceRaster

MODEL = "phomemo-d30"

#: Lines the head prints per second, from the 60mm/s spec.
LINES_PER_SECOND = PRINT_SPEED_MM_S * PX_PER_MM


@dataclass(frozen=True, slots=True)
class D30Config:
    """Timing and geometry knobs."""

    #: Bytes per write to the socket. Small enough to pace, large enough to be cheap.
    chunk_bytes: int = 4096
    #: Multiplier on the theoretical print time per chunk. Only meaningful when the
    #: transport has no flow control of its own -- RFCOMM credits and the BLE ``0101``
    #: ACK both make it redundant. Set to 0 to disable.
    pace_factor: float = 1.2
    #: Pause between session-setup packets.
    inter_packet_delay_s: float = 0.02
    #: Extra settle time after a print, on top of the computed duration.
    post_print_margin_s: float = 0.3
    #: Send a short blank feed on the first print after a wake. Some units print the
    #: first label faint otherwise; confirmed or ruled out during bring-up.
    wake_dummy_feed: bool = False
    #: Burn darkness: 1 light, 2 medium, 4 heavy. Verified by printing one label at
    #: each against byte-identical rasters.
    density: int = DENSITY_MEDIUM
    #: Head width in bytes, for ``LEFT_MARGIN`` letterboxing. ``None`` means unmeasured
    #: and sends margin 0, which is what we did before and is byte-identical for a
    #: full-width label. Set it to 12 (96 dots) once the width sweep confirms the head;
    #: until then, whether 15mm tape prints 96 or 120 dots is an open question and
    #: letterboxing on the assumption would be wrong.
    head_width_bytes: int | None = None
    #: Coalesce the session queries into one write, as the Android app does.
    batch_session_queries: bool = False
    #: Wait for the printer's ``print_complete`` frame instead of the duration timer.
    #: Falls back to the timer if nothing arrives within the computed time plus margin.
    await_print_complete: bool = True

    def __post_init__(self) -> None:
        if self.density not in DENSITIES:
            raise ValueError(f"density {self.density} is not one of {DENSITIES}")


@dataclass
class PhomemoD30:
    """Drives one printer over one transport.

    Reconnection is *not* handled here. The caller decides whether a
    ``D30ConnectError`` means retry, stall or fail, because that decision needs the
    job's deadline and the batch mode, neither of which belong in a device driver.
    """

    transport: Transport
    config: D30Config = field(default_factory=D30Config)
    #: Injected so tests can run with no wall clock.
    sleep: Callable[[float], None] = time.sleep
    #: Injected alongside ``sleep`` so completion waits are testable.
    clock: Callable[[], float] = time.monotonic

    _initialised: bool = field(default=False, init=False, repr=False)

    #: How often to check for the completion frame while waiting.
    _COMPLETION_POLL_S = 0.05

    @property
    def model(self) -> str:
        return MODEL

    @property
    def is_connected(self) -> bool:
        return self.transport.is_open and self._initialised

    @property
    def feedback(self):
        """Status accumulated on this connection. Never ``None``."""
        return self.transport.feedback

    @property
    def paper_ok(self) -> bool | None:
        """``None`` when the printer has not reported -- not the same as OK."""
        return self.transport.feedback.paper_ok

    def connect(self) -> None:
        """Open the transport and run the session-setup sequence."""
        self.transport.open()
        for packet in session_setup(
            density=self.config.density, batched=self.config.batch_session_queries
        ):
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

    def print_duration_s(self, raster: DeviceRaster, copies: int = 1) -> float:
        return raster.height_px * copies / LINES_PER_SECOND

    def print_raster(
        self,
        raster: DeviceRaster,
        *,
        wait: bool = True,
        copies: int = 1,
        density: int | None = None,
    ) -> None:
        """Send one ``GS v 0`` frame.

        A whole strip goes in a single frame. That is the entire economy of strip
        mode: one leader and one trailer feed for the batch rather than per label.

        ``copies`` uses the printer's own ``PRINT_MULTI``, so N labels cost one
        transmission of the raster rather than N.
        """
        if not self.is_connected:
            raise D30NotReady("connect() before printing")
        if raster.height_px > MAX_FRAME_LINES:
            raise D30GeometryError(
                f"{raster.height_px} lines exceeds one frame ({MAX_FRAME_LINES}); "
                f"split the strip into shorter runs"
            )
        completed_before = self.transport.feedback.prints_completed

        self.transport.write(
            print_preamble(
                raster.width_bytes,
                raster.height_px,
                density=density if density is not None else self.config.density,
                copies=copies,
                head_width_bytes=self.config.head_width_bytes,
            )
        )
        self.transport.flush()

        if self.config.wake_dummy_feed:
            self.sleep(0.1)

        for offset in range(0, len(raster.data), self.config.chunk_bytes):
            chunk = raster.data[offset : offset + self.config.chunk_bytes]
            self.transport.write(chunk)
            self.transport.flush()
            self._pace(len(chunk), raster.width_bytes)

        if wait:
            self._await_completion(raster, copies, completed_before)

    def _await_completion(self, raster: DeviceRaster, copies: int, before: int) -> None:
        """Wait for the printer to say it finished, or time out and assume it did.

        The ``0x0F`` frame arrives about 2.4s after the last raster byte for a single
        label. Waiting for it rather than for a computed duration is the difference
        between "we sent bytes" and "the printer acknowledged the job".
        """
        budget = self.print_duration_s(raster, copies) + self.config.post_print_margin_s
        fb = self.transport.feedback
        # If the link has told us nothing at all so far -- no frames, no ACKs -- there
        # is no reason to expect it to announce completion either. Sleep the duration
        # in one go rather than polling something that will never change.
        silent = not fb.frames and not fb.acks
        if not self.config.await_print_complete or silent:
            self.sleep(budget)
            return
        # Elapsed time is accumulated from the sleeps themselves rather than read off a
        # clock, so an injected no-op sleep cannot spin here.
        waited = 0.0
        while waited < budget:
            if fb.prints_completed > before:
                return
            step = min(self._COMPLETION_POLL_S, budget - waited)
            self.sleep(step)
            waited += step
        # No confirmation inside the budget. The print probably succeeded, so fall
        # back to the old duration-based assumption rather than failing it.

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
        width, tape offset, rotation and mirroring -- because a complete border means
        the full width printed and the rules make orientation obvious.

        The printer also has a built-in ``PRINT_TEST_PAGE`` (``1F 11 27``), which is
        in the vendor tables but sent by no vendor app, so it is untested.
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

    def head_width_probe(self, width_px: int = 120, step_px: int = 8) -> DeviceRaster:
        """A staircase that measures the head instead of inferring it.

        Whether the head is 96 or 120 dots has been an open question, and a width
        sweep cannot settle it: the failure mode is *silent*, so "it printed" and
        "it printed the part that fits" look identical.

        This pattern reads itself. One solid block per byte-column, each one step
        taller than the last, plus a full-width bar along the bottom:

        * **Count the steps.** Each is one byte of head. 12 steps = 96 dots,
          15 steps = 120 dots. No calipers, no arithmetic.
        * **Look at the bottom bar.** Straight and short means the printer honoured
          our line width and the head simply cannot reach further -- clean truncation.
          **Diagonal** means it consumed a different number of bytes per line than we
          sent, so each row started mid-way through the previous one. That skew is
          the unmistakable signature of a head narrower than the frame, and it is
          precisely what a sweep hides.
        * **Neither** -- all steps present, bar straight and full width -- means the
          head really is ``width_px`` dots.

        Step *height* encodes column index, so the pattern also says **which** end was
        truncated: if the shortest surviving step is taller than one unit, the low end
        was cut rather than the high one. That distinguishes a narrow head from a tape
        offset, which counting alone cannot.

        Send this at the *widest* hypothesis (120). A narrower head reveals itself;
        a wider one cannot be discovered by asking for less.

        Cheaper first: ``LABEL_WIDTH`` (``1F 11 18``) is a read-only query that may
        just answer. It is in the vendor tables and no vendor app sends it, so it is
        untested -- but it costs one round-trip and no tape.
        """
        from PIL import Image, ImageDraw

        if width_px % 8:
            raise D30GeometryError(f"width {width_px}px is not a whole number of bytes")

        columns = width_px // 8
        bar_px = 4
        height_px = columns * step_px + bar_px + step_px
        img = Image.new("1", (width_px, height_px), color=0)
        d = ImageDraw.Draw(img)
        for i in range(columns):
            x0 = i * 8
            d.rectangle([x0, 0, x0 + 6, step_px * (i + 1)], fill=1)
        # Full-width bar: straight = truncation, diagonal = byte-per-line mismatch.
        d.rectangle([0, height_px - bar_px, width_px - 1, height_px - 1], fill=1)
        return DeviceRaster(width_px=width_px, height_px=height_px, data=img.tobytes())
