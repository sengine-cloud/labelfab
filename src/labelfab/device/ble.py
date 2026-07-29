"""BLE (GATT) transport, for D30 units that speak Bluetooth Low Energy, not Classic SPP.

Some D30 firmware exposes no Serial Port Profile record -- ``sdptool browse`` comes
back empty and pairing instead resolves a GATT profile: a vendor primary service
``0xff00`` with characteristics ``ff01``/``ff02``/``ff03``. Those units cannot be driven
over an RFCOMM socket; the print bytes go to a GATT characteristic (``ff02``).

The payload is identical to the SPP path -- the same ESC/POS init packets, header and
raster -- so this is a drop-in ``Transport`` that differs only in how the bytes leave
the machine. ``bleak`` is async while ``PhomemoD30`` drives the transport synchronously
(with an injected clock), so a dedicated asyncio loop runs in a background thread and
each sync call blocks on it.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading

from labelfab.device.errors import D30ConnectError, D30WriteTimeout
from labelfab.device.feedback import ACK, DeviceFeedback

#: Vendor GATT write characteristic. Established by the D30 BLE references
#: (crabdancing/phomemo-d30, polskafan/phomemo_d30); ``ff01``/``ff03`` are the
#: alternates to try if a firmware revision doesn't accept writes here.
DEFAULT_WRITE_UUID = "0000ff02-0000-1000-8000-00805f9b34fb"
#: Notify characteristic that streams per-packet ACKs and status frames.
DEFAULT_NOTIFY_UUID = "0000ff03-0000-1000-8000-00805f9b34fb"


class BleTransport:
    """Writes ESC/POS bytes to a GATT characteristic over BLE.

    Runs its own asyncio event loop on a background thread; every public method is
    synchronous and blocks on that loop, so the driver stays clock-injected and
    testable and never has to know the link is async.
    """

    def __init__(
        self,
        mac: str,
        write_uuid: str = DEFAULT_WRITE_UUID,
        *,
        notify_uuid: str = DEFAULT_NOTIFY_UUID,
        adapter: str | None = None,
        connect_timeout_s: float = 20.0,
        write_timeout_s: float = 10.0,
        # write-without-response matches the references and is what the D30's write
        # characteristic supports.
        write_response: bool = False,
        # Gate each packet on the printer's per-packet ACK notification, so writes
        # never outrun what it can consume (the reliable path once we have the notify
        # channel). Falls through after ack_timeout_s so a missed ACK can't hang.
        flow_control: bool = True,
        ack_timeout_s: float = 0.25,
    ) -> None:
        self.mac = mac
        self.write_uuid = write_uuid
        self.notify_uuid = notify_uuid
        self.adapter = adapter
        self.connect_timeout_s = connect_timeout_s
        self.write_timeout_s = write_timeout_s
        self.write_response = write_response
        self.flow_control = flow_control
        self.ack_timeout_s = ack_timeout_s
        #: Status streamed on the notify characteristic; readable after a print.
        self.feedback = DeviceFeedback()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._client = None
        self._ack: asyncio.Event | None = None

    @property
    def is_open(self) -> bool:
        return self._client is not None and bool(self._client.is_connected)

    # -- sync/async bridge -------------------------------------------------- #

    def _start_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name="labelfab-ble", daemon=True)
        self._thread.start()

    def _teardown_loop(self) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
            if self._thread is not None:
                self._thread.join(timeout=3)
            self._loop.close()
        self._loop = None
        self._thread = None

    def _run(self, coro, timeout: float):
        assert self._loop is not None
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout)
        except concurrent.futures.TimeoutError as exc:
            future.cancel()
            raise D30WriteTimeout(f"BLE operation timed out after {timeout}s") from exc

    # -- Transport ---------------------------------------------------------- #

    def open(self) -> None:
        if self._client is not None:
            return
        try:
            import bleak  # noqa: F401
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise D30ConnectError("the BLE transport needs bleak: pip install 'labelfab[ble]'") from exc

        self.feedback = DeviceFeedback()  # fresh per connection
        self._start_loop()
        try:
            self._run(self._connect(), self.connect_timeout_s + 5)
        except D30WriteTimeout:
            self._teardown_loop()
            raise D30ConnectError(
                f"timed out connecting to {self.mac} over BLE; is the printer awake?"
            ) from None
        except Exception as exc:
            self._teardown_loop()
            self._client = None
            raise D30ConnectError(
                f"could not connect to {self.mac} over BLE: {exc}. Is it awake and paired?"
            ) from exc

    async def _connect(self) -> None:
        from bleak import BleakClient

        kwargs: dict = {"timeout": self.connect_timeout_s}
        if self.adapter:
            kwargs["adapter"] = self.adapter
        client = BleakClient(self.mac, **kwargs)
        await client.connect()
        self._client = client
        self._ack = asyncio.Event()
        try:
            await client.start_notify(self.notify_uuid, self._on_notify)
        except Exception:
            # A firmware without the notify characteristic still prints -- just blind.
            self._ack = None

    def _on_notify(self, _handle, data) -> None:
        self.feedback.ingest(data)
        if self._ack is not None and bytes(data) == ACK:
            self._ack.set()

    def write(self, data: bytes) -> None:
        if not self.is_open:
            raise D30ConnectError("transport is not open")
        try:
            self._run(self._write(bytes(data)), self.write_timeout_s + 5)
        except (D30WriteTimeout, D30ConnectError):
            raise
        except Exception as exc:
            raise D30ConnectError(f"BLE write failed, printer disconnected: {exc}") from exc

    async def _write(self, data: bytes) -> None:
        # Sub-chunk to the negotiated ATT MTU (minus the 3-byte header). Large frames
        # far exceed one MTU, so they must be split even though the caller already
        # chunked for pacing.
        mtu = getattr(self._client, "mtu_size", 23) or 23
        chunk = max(20, mtu - 3)
        gated = self.flow_control and self._ack is not None
        for offset in range(0, len(data), chunk):
            if gated:
                self._ack.clear()  # type: ignore[union-attr]
            await self._client.write_gatt_char(
                self.write_uuid, data[offset : offset + chunk], response=self.write_response
            )
            if gated:
                try:
                    # Wait for the printer's per-packet ACK, so we never outrun it.
                    await asyncio.wait_for(self._ack.wait(), self.ack_timeout_s)  # type: ignore[union-attr]
                except asyncio.TimeoutError:
                    pass  # a missed ACK must not hang the print; pace on

    def flush(self) -> None:
        """No-op: each write is dispatched to the device synchronously above."""

    def close(self) -> None:
        try:
            if self._client is not None and self._loop is not None:
                try:
                    self._run(self._client.disconnect(), 5)
                except Exception:
                    pass  # closing must never raise; the link is going away regardless
        finally:
            self._teardown_loop()
            self._client = None
