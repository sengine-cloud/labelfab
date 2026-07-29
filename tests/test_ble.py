"""BleTransport unit checks that need no radio.

The real link is exercised on hardware (a D30 with no SPP record); here we only cover
the state machine and the Transport contract, which run without bleak connected.
"""

from __future__ import annotations

import pytest

from labelfab.device import DEFAULT_WRITE_UUID, BleTransport, Transport
from labelfab.device.errors import D30ConnectError


def test_is_a_transport_and_starts_closed():
    t = BleTransport("AA:BB:CC:DD:EE:FF")
    assert isinstance(t, Transport)  # satisfies the runtime-checkable protocol
    assert t.is_open is False
    assert t.write_uuid == DEFAULT_WRITE_UUID


def test_write_before_open_raises():
    t = BleTransport("AA:BB:CC:DD:EE:FF")
    with pytest.raises(D30ConnectError):
        t.write(b"x")


def test_flush_and_close_are_safe_when_never_opened():
    t = BleTransport("AA:BB:CC:DD:EE:FF")
    t.flush()  # no-op
    t.close()  # must not raise even though nothing was opened
    assert t.is_open is False
