"""Parsing the D30's status frames — fixtures are real bytes captured off the wire.

The same ``1A <tag> <payload>`` framing appears on both transports; only delivery
differs, so these tests drive it through the transport-agnostic parser.
"""

from __future__ import annotations

import pytest

from labelfab.device import BleTransport, DeviceFeedback, PhomemoD30, StatusParser
from labelfab.device.transport import FakeTransport

SERIAL = "Q223P4C31420105"


def test_counts_acks_and_decodes_serial():
    fb = DeviceFeedback()
    frames = [
        bytes.fromhex("0101"),
        bytes.fromhex("0101"),
        bytes([0x1A, 0x08]) + SERIAL.encode(),
        bytes.fromhex("1a0458"),  # battery, 88%
        bytes.fromhex("0101"),
    ]
    for f in frames:
        fb.ingest(f)
    assert fb.acks == 3
    assert fb.serial == SERIAL
    assert fb.battery_pct == 88
    assert f"serial={SERIAL}" in fb.summary()


def test_ignores_empty_and_unknown_frames():
    fb = DeviceFeedback()
    fb.ingest(b"")
    fb.ingest(bytes.fromhex("02b600"))  # not an ACK, not a status frame
    assert fb.acks == 0
    assert fb.serial is None


def test_feedback_is_exposed_on_every_transport():
    """Both transports have a read channel; the driver must not branch on which.

    This previously asserted SPP reported nothing. That was never a property of the
    printer — it answers over SPP too — only of us never having read the socket.
    """
    ble = PhomemoD30(BleTransport("AA:BB:CC:DD:EE:FF"))
    assert isinstance(ble.feedback, DeviceFeedback)
    spp = PhomemoD30(FakeTransport())
    assert isinstance(spp.feedback, DeviceFeedback)


# --------------------------------------------------------------------------- #
# Captured live from the printer.
# --------------------------------------------------------------------------- #


def test_decodes_the_captured_session_replies():
    fb = DeviceFeedback()
    for hexstr in ("1a07020102", "1a0458", "1a0689", "1a0c0a", "1a1703", "1a0598", "1a03a8"):
        fb.ingest(bytes.fromhex(hexstr))
    assert fb.firmware == "2.1.2"
    assert fb.battery_pct == 88
    assert fb.paper_ok is True


def test_print_complete_is_a_real_signal_not_a_timer():
    fb = DeviceFeedback()
    assert fb.prints_completed == 0
    fb.ingest(bytes.fromhex("1a0f0c"))
    assert fb.prints_completed == 1


def test_media_error_arrives_unsolicited_and_recovers():
    """Captured live: pulling the stripe and replacing it, with no query in between."""
    fb = DeviceFeedback()
    fb.ingest(bytes.fromhex("1a0689"))
    assert fb.paper_ok is True
    fb.ingest(bytes.fromhex("1a0688"))  # stripe pulled
    assert fb.paper_ok is False
    fb.ingest(bytes.fromhex("1a0689"))  # replaced
    assert fb.paper_ok is True


def test_unreported_paper_is_none_not_ok():
    """``None`` and ``False`` must not collapse: unknown is not the same as fine."""
    assert DeviceFeedback().paper_ok is None


def test_serial_prefix_identifies_the_model():
    fb = DeviceFeedback()
    fb.ingest(bytes([0x1A, 0x08]) + SERIAL.encode())
    assert fb.model_prefix == "Q223"  # -> D30 in the vendor's DefaultPrinter.json


# --------------------------------------------------------------------------- #
# Framing. The vendor parser gets these wrong; ours must not.
# --------------------------------------------------------------------------- #


def test_verify_paper_payload_is_consumed_not_reparsed():
    """``1a16`` carries 4 bytes the vendor app discards — and then misparses.

    Its ``case 22`` consumes the tag only, so ``00 40 00 00`` falls through to the
    length-prefixed UID reader, which takes ``0x40`` as a 64-byte length and throws.
    A length-aware parser consumes all four and moves on cleanly.
    """
    p = StatusParser()
    frames = p.feed(bytes.fromhex("1a1600400000") + bytes.fromhex("1a0f0c"))
    assert [f.name for f in frames] == ["reset_paper_ok", "print_complete"]
    assert frames[0].raw == bytes.fromhex("00400000")
    assert p.pending == b""


def test_frames_split_across_reads_are_reassembled():
    """SPP delivers a stream, so a frame can straddle two reads."""
    p = StatusParser()
    assert p.feed(bytes.fromhex("1a07")) == []
    assert p.feed(bytes.fromhex("0201")) == []
    frames = p.feed(bytes.fromhex("02"))
    assert len(frames) == 1
    assert frames[0].value == "2.1.2"


def test_several_frames_in_one_packet():
    p = StatusParser()
    frames = p.feed(bytes.fromhex("1a04581a06891a0f0c"))
    assert [f.name for f in frames] == ["battery", "paper_state", "print_complete"]


def test_unknown_tag_costs_its_own_frame_not_the_buffer():
    """The vendor's fallback eats the rest of the buffer; ours drops prefix + tag.

    Dropping only the prefix would leave the unknown tag at the head of the buffer,
    where it is not a prefix, so the next pass would fall into the resync scan -- and
    that scan stops at the first 0x1A/0x1B, which inside an unknown payload could be
    a data byte rather than a frame start.
    """
    p = StatusParser()
    frames = p.feed(bytes.fromhex("1aee") + bytes.fromhex("1a0458"))
    assert [f.name for f in frames] == ["battery"]
    assert p.unknown_tags == {0xEE: 1}


def test_an_unknown_tag_corrupts_at_most_its_neighbourhood():
    """Bounded damage, not perfect recovery -- and the distinction is honest.

    Without a payload length for the unknown tag we cannot know where its frame ends,
    so a following frame may be misread. What is guaranteed: the parser always makes
    progress, never raises, never consumes the rest of the buffer, and records the
    tag so a probe run can tell us what we are missing. Contrast the vendor, whose
    unknown-tag path reads the next byte as a length and discards everything after.
    """
    p = StatusParser()
    frames = p.feed(bytes.fromhex("1aee1a04") + bytes.fromhex("1a0f0c"))
    assert p.unknown_tags == {0xEE: 1}
    assert len(frames) <= 2  # progress was made, nothing hung
    assert p.pending == b""  # and the buffer was not left holding the rest


def test_the_stream_recovers_on_the_next_clean_frame():
    p = StatusParser()
    p.feed(bytes.fromhex("1aee"))
    frames = p.feed(bytes.fromhex("1a0f0c"))
    assert [f.name for f in frames] == ["print_complete"]


def test_strict_mode_raises_on_an_unknown_tag():
    with pytest.raises(ValueError, match="unknown status tag"):
        StatusParser(strict=True).feed(bytes.fromhex("1aee00"))


def test_leading_garbage_is_resynced():
    p = StatusParser()
    frames = p.feed(bytes.fromhex("ffff") + bytes.fromhex("1a0458"))
    assert [f.name for f in frames] == ["battery"]
