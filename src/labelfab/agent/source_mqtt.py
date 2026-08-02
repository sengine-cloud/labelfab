"""MQTT job intake and result publishing.

The one invariant here is **commit before ack**: a delivery is parsed, validated and
durably spooled, and only then acknowledged to the broker. A crash in that window
leaves the message un-acked and the broker redelivers it. Two deliveries that must
still ack are the ones that would otherwise poison-loop:

* an unparseable payload -- a schema error is never transient, so publish ``rejected``
  and ack anyway;
* a duplicate ``idempotency_key`` -- ack and republish the cached result with
  ``duplicate: true``, which is what makes broker redelivery safe end to end.

Heavy work (rendering, printing) never runs in the network thread: a spooled job id
is handed to the print loop through ``enqueue`` and returns immediately.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from labelfab.agent.config import Config
from labelfab.agent.spool import Outcome, Spool
from labelfab.contract import JobResult, PrinterStatus, PrintJob

log = logging.getLogger("labelfab.mqtt")

#: Sentinel the print loop recognises as "flush the pending strip now".
FLUSH_COMMAND = "\x00flush"


class MqttSource:
    """Subscribes ``jobs``/``cmd``, publishes ``results``/``progress``/``status``."""

    def __init__(self, config: Config, spool: Spool, enqueue: Callable[[str], None]) -> None:
        import paho.mqtt.client as mqtt

        self.config = config
        self.spool = spool
        self.enqueue = enqueue
        self._mqtt = mqtt

        client_id = config.mqtt.client_id or f"labelfab-{config.agent.printer_id}"
        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id,
            clean_session=False,  # durable session: redelivery of anything missed offline
            transport="websockets" if config.mqtt.transport == "websockets" else "tcp",
            manual_ack=True,  # so we ack only after the spool commit
        )
        if config.mqtt.transport == "websockets":
            self.client.ws_set_options(path=config.mqtt.ws_path)
        if config.mqtt.tls:
            self.client.tls_set()
        if config.mqtt.username:
            self.client.username_pw_set(config.mqtt.username, config.mqtt.password)

        # What the retained topic currently says. Seeded from the spool so a fresh
        # process starts out holding the previous one's device truth, then updated by
        # every retained publish -- which is what lets a reconnect restore the topic
        # instead of flattening it.
        snapshot = spool.device_snapshot()
        self._last_status = snapshot.to_status(
            config.agent.printer_id,
            state=snapshot.settled_state(),
            tape_width_mm=config.tape.width_mm,
        )

        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message

    # -- lifecycle ---------------------------------------------------------- #

    def start(self) -> None:
        # Armed here rather than in __init__ so it reflects whatever the startup probe
        # and crash recovery learned, which happen after this object is built.
        self._arm_will()
        self.client.connect(self.config.mqtt.host, self.config.mqtt.port, self.config.mqtt.keepalive_s)
        self.client.loop_start()

    def _arm_will(self) -> None:
        """Point the will at the current last-known truth.

        The will lives in the CONNECT packet, so the broker holds a fixed payload for
        the whole session and no amount of calling this changes what fires if *this*
        connection drops. paho rebuilds CONNECT from these fields on every reconnect
        though, so re-arming still moves the staleness window from "whenever the
        process started" -- potentially weeks -- down to "the last reconnect", which
        here is a few hours at most. Called from ``_on_connect``, i.e. from paho's own
        network thread, which is also the thread that builds the packet, so there is
        no window where the payload and the topic disagree.
        """
        self.client.will_set(
            self.config.topic("status"), self._disconnected().model_dump_json(), qos=1, retain=True
        )

    def stop(self) -> None:
        try:
            self.publish_status(self._disconnected())
        finally:
            self.client.loop_stop()
            self.client.disconnect()

    def _disconnected(self) -> PrinterStatus:
        """Last-known truth with the link marked down.

        Backs both the will and the shutdown notice: if the agent drops, a producer
        must see that the printer is unreachable before it ever clicks print. Losing
        the link is news about reachability, though, not grounds for forgetting the
        serial, the firmware and the media state -- ``device_seen_at`` rides along to
        say how old they are.
        """
        return self._last_status.model_copy(update={"state": "disconnected", "pending_labels": 0})

    # -- Publisher protocol ------------------------------------------------- #

    def publish_result(self, result: JobResult) -> None:
        self.client.publish(self.config.topic("results"), result.model_dump_json(), qos=1)

    def publish_status(self, status: PrinterStatus, *, retain: bool = True) -> None:
        if retain:
            # Recorded before the publish and regardless of whether it succeeds: the
            # startup probe and crash recovery both publish before the client is
            # connected, so those go nowhere and the connect below is what lands them.
            # A non-retained publish is not what the topic holds, so it is not recorded.
            self._last_status = status
        self.client.publish(self.config.topic("status"), status.model_dump_json(), qos=1, retain=retain)

    def publish_progress(self, job_id: str, printed: int, total: int) -> None:
        payload = f'{{"job_id":"{job_id}","printed":{printed},"total":{total}}}'
        self.client.publish(self.config.topic("progress"), payload, qos=0)

    # -- callbacks ---------------------------------------------------------- #

    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        if reason_code != 0:
            log.error("mqtt connect failed: %s", reason_code)
            return
        client.subscribe(self.config.topic("jobs"), qos=self.config.mqtt.qos)
        client.subscribe(self.config.topic("cmd"), qos=1)
        # Republish what the topic already said, rather than a freshly synthesised
        # blank. This fires on every reconnect, not just the first connect, and paho
        # reconnects silently -- the Istio route for the broker caps at a 24h timeout
        # and flaps besides, so it runs several times a day. Publishing a hardcoded
        # bare status here was not merely uninformative, it was lossy: it overwrote a
        # retained message that was correct a moment earlier, and the printer is asleep
        # and cannot be asked again.
        self.publish_status(self._last_status)
        self._arm_will()  # for the connect after this one; see _arm_will

    def _on_message(self, client, userdata, msg) -> None:
        if msg.topic.endswith("/cmd"):
            self._handle_cmd(msg)
            client.ack(msg.mid, msg.qos)
            return

        try:
            job = PrintJob.model_validate_json(msg.payload)
        except Exception as exc:  # a schema error is never transient
            log.warning("rejecting malformed job: %s", exc)
            self.publish_result(JobResult(job_id="unknown", state="rejected", error=str(exc)[:400]))
            client.ack(msg.mid, msg.qos)
            return

        try:
            self.spool.purge_dedupe(self.config.spool.dedupe_ttl_s)
            result = self.spool.try_insert(job)
        except Exception:  # do NOT ack: leave it for redelivery
            log.exception("spool insert failed for %s", job.job_id)
            return

        if result.outcome is Outcome.DUPLICATE:
            cached = result.cached or JobResult(job_id=job.job_id, state="completed")
            self.publish_result(cached.model_copy(update={"duplicate": True}))
            client.ack(msg.mid, msg.qos)
            return

        client.ack(msg.mid, msg.qos)  # committed above; safe to acknowledge now
        self.enqueue(job.job_id)

    def _handle_cmd(self, msg) -> None:
        body = msg.payload.decode("utf-8", "replace").strip().lower()
        if "flush" in body:
            self.enqueue(FLUSH_COMMAND)
