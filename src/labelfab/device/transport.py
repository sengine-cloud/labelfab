"""Byte pipes to the printer.

Three implementations behind one Protocol:

* ``AFBluetoothTransport`` -- the default. Connects an RFCOMM socket directly, so
  there is no ``rfcomm bind``, no root, no ``/dev/rfcommN`` node to race against
  on reconnect.
* ``SerialTransport`` -- the reference implementation's path, over a bound
  ``/dev/rfcommN``. Kept for A/B comparison during hardware bring-up.
* ``FakeTransport`` -- captures bytes and flush boundaries so the whole stack can
  be tested with no printer.

All of them expose a ``feedback`` attribute carrying the printer's status frames, so
the driver above cannot tell SPP from BLE. The delivery differs -- BLE gets discrete
``ff03`` notifications plus per-write ``0101`` ACKs, SPP gets an unframed byte stream
with no ACKs -- but :class:`~labelfab.device.responses.StatusParser` normalises both.

The D30 *does* answer over SPP; the earlier belief that it was write-only came from
never having read the socket.
"""

from __future__ import annotations

import errno
import socket
import threading
from typing import Protocol, runtime_checkable

from labelfab.device.errors import D30ConnectError, D30WriteTimeout
from labelfab.device.feedback import DeviceFeedback

#: OS errors that mean "the printer went away", as opposed to a programming fault.
_CONNECTION_ERRNOS = frozenset(
    {
        errno.ETIMEDOUT,
        errno.EHOSTDOWN,
        errno.EHOSTUNREACH,
        errno.ECONNREFUSED,
        errno.ECONNRESET,
        errno.ENOTCONN,
        errno.EPIPE,
        errno.EBADF,
    }
)


#: "afbluetooth" on Linux where the socket family is present, "ble" elsewhere (e.g.
#: python-build-standalone environments that lack libbluetooth-dev).
DEFAULT_TRANSPORT = "afbluetooth" if hasattr(socket, "AF_BLUETOOTH") else "ble"


@runtime_checkable
class Transport(Protocol):
    """A bidirectional byte pipe.

    ``feedback`` accumulates whatever the printer sends back. It is always present so
    callers never branch on transport type; on a link that happens to deliver nothing
    it simply stays empty.
    """

    feedback: DeviceFeedback

    def open(self) -> None: ...

    def write(self, data: bytes) -> None: ...

    def flush(self) -> None: ...

    def close(self) -> None: ...

    @property
    def is_open(self) -> bool: ...


def _wrap_oserror(exc: OSError, what: str) -> Exception:
    if isinstance(exc, TimeoutError | socket.timeout):
        return D30WriteTimeout(f"{what} timed out: {exc}")
    if isinstance(exc, BrokenPipeError | ConnectionResetError):
        return D30ConnectError(f"{what} failed, printer disconnected: {exc}")
    if exc.errno in _CONNECTION_ERRNOS:
        return D30ConnectError(f"{what} failed: {exc}")
    return exc


class AFBluetoothTransport:
    """RFCOMM over ``socket.AF_BLUETOOTH``.

    Note for anyone hardening the systemd unit: ``RestrictAddressFamilies=`` must
    include ``AF_BLUETOOTH`` or this fails at ``socket()`` with ``EAFNOSUPPORT``
    and no indication that systemd is the cause.
    """

    def __init__(
        self,
        mac: str,
        channel: int = 1,
        connect_timeout_s: float = 10.0,
        write_timeout_s: float = 5.0,
        *,
        read: bool = True,
    ) -> None:
        self.mac = mac
        self.channel = channel
        self.connect_timeout_s = connect_timeout_s
        self.write_timeout_s = write_timeout_s
        #: Run a reader thread. The printer answers queries and pushes unsolicited
        #: media-error notifications over SPP, so this is on by default.
        self.read = read
        self.feedback = DeviceFeedback()
        self._sock: socket.socket | None = None
        self._reader: threading.Thread | None = None
        self._stop = threading.Event()

    @property
    def is_open(self) -> bool:
        return self._sock is not None

    def open(self) -> None:
        if self._sock is not None:
            return
        if not hasattr(socket, "AF_BLUETOOTH"):  # pragma: no cover - non-Linux
            raise D30ConnectError(
                "this platform has no AF_BLUETOOTH; use transport 'serial' with rfcomm bind"
            )
        try:
            sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
        except OSError as exc:
            raise D30ConnectError(
                f"could not create an RFCOMM socket: {exc}. If running under systemd, "
                f"check that RestrictAddressFamilies includes AF_BLUETOOTH."
            ) from exc
        try:
            sock.settimeout(self.connect_timeout_s)
            sock.connect((self.mac, self.channel))
            sock.settimeout(self.write_timeout_s)
        except OSError as exc:
            sock.close()
            raise D30ConnectError(
                f"could not connect to {self.mac} on RFCOMM channel {self.channel}: {exc}. "
                f"Is the printer awake and paired?"
            ) from exc
        self._sock = sock
        self.feedback = DeviceFeedback()  # fresh per connection
        if self.read:
            self._stop.clear()
            self._reader = threading.Thread(
                target=self._read_loop, name=f"d30-spp-reader-{self.mac}", daemon=True
            )
            self._reader.start()

    def _read_loop(self) -> None:
        """Drain the socket into ``feedback`` until close.

        Status frames arrive unframed and can straddle reads, which is why the parser
        is incremental rather than per-packet. Errors end the loop quietly -- a reader
        thread must never be the thing that takes down a print.
        """
        sock = self._sock
        if sock is None:
            return
        while not self._stop.is_set():
            try:
                sock.settimeout(0.5)
                data = sock.recv(512)
            except (TimeoutError, socket.timeout):
                continue
            except OSError:
                return  # socket closed under us, or the printer went away
            if not data:
                return  # clean EOF
            try:
                self.feedback.ingest(data)
            except Exception:  # a decode fault must not kill the reader
                continue

    def write(self, data: bytes) -> None:
        if self._sock is None:
            raise D30ConnectError("transport is not open")
        try:
            self._sock.sendall(data)
        except OSError as exc:
            raise _wrap_oserror(exc, f"writing {len(data)}B") from exc

    def flush(self) -> None:
        """No-op: ``sendall`` has already handed the bytes to the kernel."""

    def close(self) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass  # closing must never raise; the socket is going away regardless
            self._sock = None
        if self._reader is not None:
            self._reader.join(timeout=1.0)
            self._reader = None


class SerialTransport:
    """A bound ``/dev/rfcommN`` node, as used by the reference implementation."""

    def __init__(self, port: str = "/dev/rfcomm0", write_timeout_s: float = 5.0) -> None:
        self.port = port
        self.write_timeout_s = write_timeout_s
        #: Present for interface parity. Nothing drains the port here -- pyserial is
        #: only used for A/B comparison during bring-up, so reads stay out of it.
        self.feedback = DeviceFeedback()
        self._port = None

    @property
    def is_open(self) -> bool:
        return self._port is not None

    def open(self) -> None:
        if self._port is not None:
            return
        try:
            import serial
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise D30ConnectError(
                "the serial transport needs pyserial: pip install 'labelfab[serial]'"
            ) from exc
        try:
            self._port = serial.Serial(
                self.port, timeout=self.write_timeout_s, write_timeout=self.write_timeout_s
            )
        except Exception as exc:
            raise D30ConnectError(f"could not open {self.port}: {exc}") from exc

    def write(self, data: bytes) -> None:
        if self._port is None:
            raise D30ConnectError("transport is not open")
        try:
            self._port.write(data)
        except OSError as exc:
            raise _wrap_oserror(exc, f"writing {len(data)}B") from exc
        except Exception as exc:  # pyserial raises its own SerialTimeoutException
            if "timeout" in type(exc).__name__.lower():
                raise D30WriteTimeout(f"writing {len(data)}B timed out: {exc}") from exc
            raise

    def flush(self) -> None:
        if self._port is not None:
            self._port.flush()

    def close(self) -> None:
        if self._port is not None:
            try:
                self._port.close()
            except Exception:
                pass
            self._port = None


class FakeTransport:
    """Captures everything written, plus where each flush landed.

    Recording flush *offsets* rather than only the bytes is what lets tests assert
    the framing -- seven separate init writes, then a header, then the body -- which
    is exactly the thing these printers are fussy about.
    """

    def __init__(self, fail_after_bytes: int | None = None) -> None:
        self.buf = bytearray()
        self.flushes: list[int] = []
        self.writes: list[int] = []
        self.opened = 0
        self.closed = 0
        self._open = False
        #: Simulate a mid-transfer disconnect, for testing partial-strip handling.
        self.fail_after_bytes = fail_after_bytes
        self.feedback = DeviceFeedback()

    def inject(self, data: bytes) -> None:
        """Simulate the printer sending something back.

        Lets tests drive the status path -- print-complete, an unsolicited media
        error -- without a printer or a transport-specific fake.
        """
        self.feedback.ingest(data)

    @property
    def is_open(self) -> bool:
        return self._open

    def open(self) -> None:
        self._open = True
        self.opened += 1

    def write(self, data: bytes) -> None:
        if not self._open:
            raise D30ConnectError("transport is not open")
        if self.fail_after_bytes is not None and len(self.buf) + len(data) > self.fail_after_bytes:
            room = max(0, self.fail_after_bytes - len(self.buf))
            self.buf += data[:room]
            raise D30ConnectError("simulated disconnect mid-write")
        self.buf += data
        self.writes.append(len(data))

    def flush(self) -> None:
        self.flushes.append(len(self.buf))

    def close(self) -> None:
        if self._open:
            self.closed += 1
        self._open = False
