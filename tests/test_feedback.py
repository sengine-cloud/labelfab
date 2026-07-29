"""Parsing the D30's BLE notify frames — fixtures are real bytes captured off ff03."""

from __future__ import annotations

from labelfab.device import BleTransport, DeviceFeedback, PhomemoD30
from labelfab.device.transport import FakeTransport

SERIAL = "Q223P4C31420105"


def test_counts_acks_and_decodes_serial():
    fb = DeviceFeedback()
    frames = [
        bytes.fromhex("0101"),
        bytes.fromhex("0101"),
        bytes([0x1A, 0x08]) + SERIAL.encode(),  # serial field
        bytes.fromhex("1a0bb8"),  # a telemetry field
        bytes.fromhex("0101"),
    ]
    for f in frames:
        fb.ingest(f)
    assert fb.acks == 3
    assert fb.serial == SERIAL
    assert fb.fields[0x0B] == bytes.fromhex("b8")
    assert f"serial={SERIAL}" in fb.summary()
    assert "0x0b=b8" in fb.summary()


def test_ignores_empty_and_unknown_frames():
    fb = DeviceFeedback()
    fb.ingest(b"")  # empty notification
    fb.ingest(bytes.fromhex("02b600"))  # not an ACK, not a status frame
    assert fb.acks == 0
    assert fb.serial is None


def test_feedback_is_exposed_through_the_device_over_ble_only():
    ble = PhomemoD30(BleTransport("AA:BB:CC:DD:EE:FF"))
    assert isinstance(ble.feedback, DeviceFeedback)  # BLE has a read channel
    spp = PhomemoD30(FakeTransport())
    assert spp.feedback is None  # write-only transports report nothing
