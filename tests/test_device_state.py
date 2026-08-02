"""Last-known device truth: what is remembered, and how old it is allowed to look.

The rules here all serve one property. The printer is only reachable while it is being
printed to, so almost every read of the status topic is a read of *remembered* truth.
That is fine as long as nobody can mistake it for live truth — which is what ``seen_at``
is for, and why it is not allowed to drift forward on its own.
"""

from __future__ import annotations

from datetime import timezone

from labelfab.agent import DeviceSnapshot
from labelfab.device import DeviceFeedback

SERIAL = "Q223P4C31420105"

#: Frames as the printer actually sends them; see tests/test_feedback.py.
FULL = ("1a08" + SERIAL.encode().hex(), "1a07020102", "1a0464", "1a2f01a1", "1a0689")
MEDIA_BAD = "1a0688"


def _feedback(*frames_hex: str) -> DeviceFeedback:
    fb = DeviceFeedback()
    for frame in frames_hex:
        fb.ingest(bytes.fromhex(frame))
    return fb


def test_merge_decodes_a_whole_connection():
    snap = DeviceSnapshot().merge(_feedback(*FULL), now=1000.0)
    assert snap.serial == SERIAL
    assert snap.firmware == "2.1.2"
    assert snap.battery_pct == 100
    assert snap.voltage_v == 4.17
    assert snap.media_ok is True
    assert snap.fault is None
    assert snap.seen_at == 1000.0


def test_a_quiet_connection_keeps_what_we_already_knew():
    """Otherwise one uncommunicative connect erases a serial learned an hour ago."""
    known = DeviceSnapshot().merge(_feedback(*FULL), now=1000.0)
    still = known.merge(_feedback(), now=2000.0)
    assert still.serial == SERIAL
    assert still.firmware == "2.1.2"
    assert still.battery_pct == 100
    assert still.media_ok is True


def test_a_quiet_connection_does_not_advance_seen_at():
    """The timestamp is the only thing separating remembered truth from live truth.

    Moving it because we merely *connected* would launder three-day-old media state
    into something a consumer renders as current.
    """
    known = DeviceSnapshot().merge(_feedback(*FULL), now=1000.0)
    assert known.merge(_feedback(), now=9999.0).seen_at == 1000.0


def test_a_partial_report_advances_seen_at_and_leaves_the_rest():
    later = DeviceSnapshot().merge(_feedback(*FULL), now=1000.0).merge(_feedback("1a0458"), now=2000.0)
    assert later.battery_pct == 88  # the one thing it reported
    assert later.serial == SERIAL  # everything it did not
    assert later.seen_at == 2000.0


def test_a_fault_does_not_latch():
    """A media error that has cleared must stop being reported, or the printer looks
    broken until someone restarts the agent."""
    faulted = DeviceSnapshot().merge(_feedback(MEDIA_BAD), now=1000.0)
    assert faulted.fault and faulted.settled_state() == "error"

    cleared = faulted.merge(_feedback(*FULL), now=2000.0)
    assert cleared.fault is None
    assert cleared.settled_state() == "idle"
    assert cleared.media_ok is True


def test_status_carries_the_snapshot_and_its_age():
    snap = DeviceSnapshot().merge(_feedback(*FULL), now=1000.0)
    status = snap.to_status("d30-workshop", state="idle", tape_width_mm=15.0, pending_labels=3)

    assert status.serial == SERIAL
    assert status.media_ok is True
    assert status.pending_labels == 3
    assert status.device_seen_at is not None
    assert status.device_seen_at.tzinfo is not None  # a bare instant is unusable
    assert status.device_seen_at.timestamp() == 1000.0


def test_never_observed_publishes_null_not_a_guess():
    status = DeviceSnapshot().to_status("d30-workshop", state="idle")
    assert status.device_seen_at is None
    assert status.media_ok is None  # silence is still not health


def test_seen_at_is_whole_seconds_utc():
    """Sub-second precision on "when did the printer last speak" is noise in a payload
    whose main reader is a human running mosquitto_sub."""
    seen = DeviceSnapshot(seen_at=1000.75).seen_at_utc()
    assert seen is not None
    assert seen.microsecond == 0
    assert seen.tzinfo is timezone.utc


def test_a_snapshot_survives_a_json_round_trip():
    snap = DeviceSnapshot().merge(_feedback(*FULL), now=1000.0)
    assert DeviceSnapshot.model_validate_json(snap.model_dump_json()) == snap


def test_a_snapshot_from_another_version_degrades_rather_than_raising():
    """This row outlives package upgrades. Refusing to decode one written by a different
    agent version would turn a cosmetic status field into a boot failure."""
    snap = DeviceSnapshot.model_validate_json(f'{{"serial": "{SERIAL}", "future_field": 42}}')
    assert snap.serial == SERIAL
    assert snap.seen_at is None
