"""What the retained status topic holds, across connects and reconnects.

Driven through a fake paho client rather than a broker: everything worth asserting
here is about *which payload* is published on which callback, and none of it needs a
network. The one thing a broker would add — that paho reconnects on its own, silently,
several times a day — is the premise, not something under test.
"""

from __future__ import annotations

import json

import pytest
from conftest import make_config

from labelfab.agent import DeviceSnapshot, Spool
from labelfab.agent.source_mqtt import FLUSH_COMMAND, MqttSource
from labelfab.contract import PrinterStatus

SERIAL = "Q223P4C31420105"


class FakeClient:
    """Stands in for ``paho.mqtt.client.Client``, recording publishes and subscriptions."""

    def __init__(self, *_args, **_kwargs) -> None:
        self.published: list[tuple[str, str, bool]] = []
        self.subscribed: list[str] = []
        self.will: tuple[str, str] | None = None
        self.on_connect = None
        self.on_message = None
        self.acked: list[int] = []

    # -- setup the source performs ------------------------------------------ #
    def ws_set_options(self, **_kw) -> None: ...
    def tls_set(self, *_a, **_kw) -> None: ...
    def username_pw_set(self, *_a, **_kw) -> None: ...
    def connect(self, *_a, **_kw) -> None: ...
    def loop_start(self) -> None: ...
    def loop_stop(self) -> None: ...
    def disconnect(self) -> None: ...

    def will_set(self, topic, payload, qos=0, retain=False) -> None:
        self.will = (topic, payload)

    def subscribe(self, topic, qos=0) -> None:
        self.subscribed.append(topic)

    def publish(self, topic, payload, qos=0, retain=False) -> None:
        self.published.append((topic, payload, retain))

    def ack(self, mid, qos) -> None:
        self.acked.append(mid)

    # -- what tests read ---------------------------------------------------- #
    @property
    def statuses(self) -> list[dict]:
        return [json.loads(p) for t, p, _ in self.published if t.endswith("/status")]


@pytest.fixture
def source(tmp_path, monkeypatch):
    """Build an MqttSource over a fake client, on a spool the test can pre-load."""
    import paho.mqtt.client as mqtt

    monkeypatch.setattr(mqtt, "Client", FakeClient)

    def build(snapshot: DeviceSnapshot | None = None) -> MqttSource:
        spool = Spool(tmp_path / "spool.db")
        if snapshot is not None:
            spool.save_device_snapshot(snapshot)
        config = make_config()
        config.mqtt.host = "broker.invalid"
        return MqttSource(config, spool, lambda _job_id: None)

    return build


def _connect(src: MqttSource) -> None:
    """Fire the callback paho fires on connect — and on every silent reconnect."""
    src._on_connect(src.client, None, {}, 0)


KNOWN = DeviceSnapshot(
    serial=SERIAL, firmware="2.1.2", battery_pct=100, voltage_v=4.18, media_ok=True, seen_at=1000.0
)


def test_a_reconnect_republishes_device_truth_instead_of_erasing_it(source):
    """The reported defect. A reconnect used to publish a hardcoded bare status,
    retained, over a message that was correct a moment earlier — and the printer is
    asleep, so nothing could put it back until the next print."""
    src = source()
    src.publish_status(
        PrinterStatus(printer_id="d30-workshop", state="idle", serial=SERIAL, media_ok=True)
    )

    _connect(src)  # paho reconnects on its own, several times a day

    restored = src.client.statuses[-1]
    assert restored["serial"] == SERIAL
    assert restored["media_ok"] is True


def test_a_fresh_process_republishes_what_the_last_one_learned(source):
    """The original issue: a restarted agent had nothing to say about the printer until
    something happened to print."""
    src = source(KNOWN)
    _connect(src)

    published = src.client.statuses[-1]
    assert published["serial"] == SERIAL
    assert published["firmware"] == "2.1.2"
    assert published["media_ok"] is True
    assert published["device_seen_at"] is not None
    assert published["state"] == "idle"


def test_a_remembered_fault_is_not_downgraded_to_healthy_on_restart(source):
    src = source(KNOWN.model_copy(update={"media_ok": False, "fault": "media not ready"}))
    _connect(src)

    published = src.client.statuses[-1]
    assert published["state"] == "error"
    assert published["media_ok"] is False
    assert published["error"] == "media not ready"


def test_an_agent_that_has_never_printed_claims_nothing(source):
    src = source()
    _connect(src)

    published = src.client.statuses[-1]
    assert published["state"] == "idle"
    assert published["serial"] is None
    assert published["media_ok"] is None  # not "fine", just unsaid
    assert published["device_seen_at"] is None


def test_the_status_is_retained_and_the_topics_are_subscribed(source):
    src = source(KNOWN)
    _connect(src)

    assert all(retain for topic, _, retain in src.client.published if topic.endswith("/status"))
    assert src.client.subscribed == [
        "se/v1/print/d30-workshop/jobs",
        "se/v1/print/d30-workshop/cmd",
    ]


def test_a_failed_connect_publishes_nothing(source):
    src = source(KNOWN)
    src._on_connect(src.client, None, {}, 5)  # not authorised
    assert src.client.published == []


def test_the_will_carries_last_known_truth(source):
    """An ungraceful drop should say the printer is unreachable, not forget what it is."""
    src = source(KNOWN)
    src.start()

    assert src.client.will is not None
    will = json.loads(src.client.will[1])
    assert will["state"] == "disconnected"
    assert will["serial"] == SERIAL
    assert will["device_seen_at"] is not None


def test_the_will_is_re_armed_so_it_does_not_describe_startup_forever(source):
    """The will is fixed for the life of a connection -- it rides in the CONNECT packet
    and the broker holds it. paho rebuilds that packet on every reconnect, though, so
    re-arming bounds how stale the will can be by the reconnect interval rather than by
    the process lifetime."""
    src = source()
    src.start()
    assert json.loads(src.client.will[1])["serial"] is None  # nothing known at boot

    _connect(src)
    src.publish_status(KNOWN.to_status("d30-workshop", state="idle"))
    _connect(src)  # a later reconnect: this is where the newer will gets armed

    will = json.loads(src.client.will[1])
    assert will["state"] == "disconnected"
    assert will["serial"] == SERIAL
    assert will["device_seen_at"] is not None


def test_a_stale_will_is_corrected_by_the_restart_that_follows_it(tmp_path, monkeypatch):
    """The will is frozen at connect, so a fault learned mid-session cannot reach it: if
    the agent is killed after that fault, the broker publishes the older, healthier
    reading over the newer one. Nothing can change what the broker holds for a live
    session -- what stops it mattering is that the next process seeds from the spool,
    which does have the fault, and republishes on connect. The unit is Restart=always
    with RestartSec=5, so that correction is seconds behind the will."""
    import paho.mqtt.client as mqtt

    monkeypatch.setattr(mqtt, "Client", FakeClient)
    config = make_config()
    config.mqtt.host = "broker.invalid"

    spool = Spool(tmp_path / "spool.db")
    spool.save_device_snapshot(KNOWN)
    healthy = MqttSource(config, spool, lambda _j: None)
    healthy.start()
    assert json.loads(healthy.client.will[1])["media_ok"] is True  # what a crash will say

    # Mid-session: a print finds the tape gone. The topic is correct; the will is not.
    faulted = KNOWN.model_copy(update={"media_ok": False, "fault": "media not ready", "seen_at": 5000.0})
    spool.save_device_snapshot(faulted)
    healthy.publish_status(faulted.to_status("d30-workshop", state="error"))
    spool.close()

    # SIGKILL: the broker publishes the stale will, then systemd brings the agent back.
    restarted = MqttSource(config, Spool(tmp_path / "spool.db"), lambda _j: None)
    _connect(restarted)

    corrected = restarted.client.statuses[-1]
    assert corrected["media_ok"] is False
    assert corrected["error"] == "media not ready"
    assert corrected["device_seen_at"] > json.loads(healthy.client.will[1])["device_seen_at"]


def test_shutdown_says_disconnected_without_forgetting(source):
    src = source(KNOWN)
    src.stop()

    final = src.client.statuses[-1]
    assert final["state"] == "disconnected"
    assert final["pending_labels"] == 0
    assert final["serial"] == SERIAL


def test_a_reconnect_mid_print_republishes_printing_not_idle(source):
    """`state` is a fact about the link and the batch. Synthesising "idle" here would
    tell a producer the strip it is waiting on had finished."""
    src = source(KNOWN)
    src.publish_status(KNOWN.to_status("d30-workshop", state="printing", pending_labels=12))

    _connect(src)

    republished = src.client.statuses[-1]
    assert republished["state"] == "printing"
    assert republished["pending_labels"] == 12


def test_a_non_retained_status_does_not_become_what_the_topic_holds(source):
    src = source(KNOWN)
    src.publish_status(PrinterStatus(printer_id="d30-workshop", state="printing"), retain=False)

    _connect(src)

    assert src.client.statuses[-1]["serial"] == SERIAL


def test_a_flush_command_reaches_the_print_loop(tmp_path, monkeypatch):
    """The cmd topic is handled on paho's thread, so it must only ever enqueue."""
    import paho.mqtt.client as mqtt

    monkeypatch.setattr(mqtt, "Client", FakeClient)
    enqueued: list[str] = []
    config = make_config()
    config.mqtt.host = "broker.invalid"
    src = MqttSource(config, Spool(tmp_path / "spool.db"), enqueued.append)

    class _Msg:
        topic = "se/v1/print/d30-workshop/cmd"
        payload = b"flush"
        qos = 1
        mid = 7

    src._on_message(src.client, None, _Msg())
    assert enqueued == [FLUSH_COMMAND]
    assert src.client.acked == [7]
