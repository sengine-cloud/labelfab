"""The RFCOMM address encoding, plus one opt-in test against a real printer.

The encoder is pure, so the part that is easy to get silently wrong -- the byte order
of the address and the trailing pad -- is pinned here with byte-exact vectors. Getting
the length wrong does not fail loudly at runtime: the kernel answers ``EINVAL``, which
reads like a bad argument from the caller rather than a marshalling bug.
"""

from __future__ import annotations

import os
import socket
import time

import pytest

from labelfab.device import _rfcomm


def test_encodes_the_kernel_struct() -> None:
    # family 31 (host order) | address reversed | channel | pad
    assert _rfcomm.sockaddr_rc("AA:FD:FD:6B:9F:5F", 1).hex() == "1f005f9f6bfdfdaa0100"


def test_address_is_reversed_not_copied() -> None:
    """A palindromic address would let a missing reversal pass unnoticed."""
    encoded = _rfcomm.sockaddr_rc("01:02:03:04:05:06", 0)
    assert encoded[2:8] == bytes([6, 5, 4, 3, 2, 1])


def test_is_exactly_sizeof_sockaddr_rc() -> None:
    # Short by even two bytes and the kernel rejects the connect with EINVAL.
    assert len(_rfcomm.sockaddr_rc("AA:FD:FD:6B:9F:5F", 1)) == _rfcomm.SOCKADDR_RC_SIZE == 10


def test_channel_lands_after_the_address() -> None:
    assert _rfcomm.sockaddr_rc("AA:FD:FD:6B:9F:5F", 30)[8] == 30


@pytest.mark.parametrize(
    "mac", ["", "AA:FD:FD:6B:9F", "AA:FD:FD:6B:9F:5F:11", "zz:fd:fd:6b:9f:5f", "not a mac"]
)
def test_rejects_malformed_addresses(mac: str) -> None:
    with pytest.raises(ValueError):
        _rfcomm.sockaddr_rc(mac, 1)


@pytest.mark.parametrize("channel", [-1, 256])
def test_rejects_out_of_range_channels(channel: int) -> None:
    with pytest.raises(ValueError):
        _rfcomm.sockaddr_rc("AA:FD:FD:6B:9F:5F", channel)


def test_accepts_lowercase_and_bare_addresses() -> None:
    canonical = _rfcomm.sockaddr_rc("AA:FD:FD:6B:9F:5F", 1)
    assert _rfcomm.sockaddr_rc("aa:fd:fd:6b:9f:5f", 1) == canonical
    assert _rfcomm.sockaddr_rc("AAFDFD6B9F5F", 1) == canonical


@pytest.mark.skipif(not _rfcomm.has_native_support(), reason="interpreter lacks AF_BLUETOOTH")
def test_matches_what_cpython_would_have_produced() -> None:
    """Ground truth: bind through the shim, and via the socket module, and compare.

    Only runnable on an interpreter built with the BlueZ headers -- which is the one
    interpreter that does not need the shim, and therefore the only one that can say
    whether the shim is right.
    """
    try:
        shimmed = _rfcomm.socket_rfcomm()
    except OSError as exc:  # pragma: no cover - depends on the host kernel
        pytest.skip(f"no kernel bluetooth here: {os.strerror(exc.errno or 0)}")
    with (
        shimmed,
        socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM) as native,
    ):
        import ctypes
        import ctypes.util

        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        addr = ctypes.create_string_buffer(
            _rfcomm.sockaddr_rc("00:00:00:00:00:00", 0), _rfcomm.SOCKADDR_RC_SIZE
        )
        assert libc.bind(shimmed.fileno(), addr, _rfcomm.SOCKADDR_RC_SIZE) == 0, os.strerror(
            ctypes.get_errno()
        )
        native.bind(("00:00:00:00:00:00", 0))

        def raw_name(sock: socket.socket) -> bytes:
            buf = ctypes.create_string_buffer(16)
            length = ctypes.c_int(16)
            assert libc.getsockname(sock.fileno(), buf, ctypes.byref(length)) == 0
            return buf.raw[: length.value]

        assert raw_name(shimmed) == raw_name(native)


@pytest.mark.hardware
def test_connects_to_a_real_printer() -> None:
    """End-to-end over the shim: connect, ask, decode.

    Opt in with ``LABELFAB_TEST_MAC=AA:BB:...``; the printer has to be awake, since a
    D30 that has auto-powered-off answers ``EHOSTDOWN``.
    """
    from labelfab.device import AFBluetoothTransport
    from labelfab.device.errors import D30ConnectError
    from labelfab.device.protocol import CHIP_TYPE, FIRMWARE_VERSION

    mac = os.environ.get("LABELFAB_TEST_MAC")
    if not mac:
        pytest.skip("set LABELFAB_TEST_MAC to run against a printer")

    transport = AFBluetoothTransport(mac, channel=1)
    try:
        transport.open()
    except D30ConnectError as exc:  # pragma: no cover - hardware state
        # An auto-powered-off D30 is indistinguishable from an unpaired one here, and
        # neither is a defect in the shim -- skip rather than fail the suite.
        pytest.skip(f"no reachable printer at {mac}: {exc}")
    try:
        transport.write(CHIP_TYPE() + FIRMWARE_VERSION())
        transport.flush()
        # The reader thread ingests asynchronously, so poll rather than sleep a fixed
        # amount: the replies land in a few ms on a warm link, seconds on a cold one.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and len(transport.feedback.frames) < 2:
            time.sleep(0.05)
        frames = transport.feedback.frames
        firmware = transport.feedback.firmware
    finally:
        transport.close()

    assert frames, "printer returned no status frames"
    assert {f.name for f in frames} & {"bt_chip_type", "firmware"}, frames
    assert firmware, "no firmware version decoded"
