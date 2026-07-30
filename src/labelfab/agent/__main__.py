"""The ``labelfab-agent`` daemon.

A single-threaded print loop fed by two sources. MQTT runs in paho's own network
thread and only ever *enqueues* a spooled job id; the dir source is polled on the
loop's idle tick. Keeping all rendering and printing on one loop means the coalescer
and the printer are never touched concurrently, so neither needs a lock.

The loop blocks on the job queue for exactly as long as the pending strip may wait --
``seconds_until_idle_flush`` -- so an idle flush happens on time without a busy-wait.
"""

from __future__ import annotations

import logging
import queue
import signal
import sys
from collections.abc import Callable

from labelfab.agent.config import Config, load
from labelfab.agent.publisher import NullPublisher, Publisher
from labelfab.agent.source_dir import DirSource
from labelfab.agent.spool import Spool
from labelfab.agent.worker import PrintWorker
from labelfab.device.d30 import D30Config, PhomemoD30

log = logging.getLogger("labelfab.agent")


def make_printer_factory(config: Config) -> Callable[[], PhomemoD30]:
    from labelfab.device import AFBluetoothTransport, FakeTransport, SerialTransport

    dcfg = D30Config(
        pace_factor=config.device.pace_factor,
        wake_dummy_feed=config.device.wake_dummy_feed,
        density=config.device.density,
    )

    def factory() -> PhomemoD30:
        kind = config.device.transport
        if kind == "fake":
            transport = FakeTransport()
        elif kind == "serial":
            transport = SerialTransport(port=config.device.serial_port)
        elif kind == "ble":
            from labelfab.device import BleTransport

            if not config.device.mac:
                raise SystemExit("labelfab-agent: device.mac is required for the ble transport")
            transport = BleTransport(
                mac=config.device.mac,
                write_uuid=config.device.ble_write_uuid,
                adapter=config.device.ble_adapter or None,
            )
        else:
            if not config.device.mac:
                raise SystemExit("labelfab-agent: device.mac is required for the afbluetooth transport")
            transport = AFBluetoothTransport(mac=config.device.mac, channel=config.device.channel)
        return PhomemoD30(transport, config=dcfg)

    return factory


class Agent:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.spool = Spool(config.spool.path)
        self.queue: queue.Queue[str] = queue.Queue()
        self._stop = False

        self.source = None
        publisher: Publisher = NullPublisher()
        if config.mqtt.host:
            from labelfab.agent.source_mqtt import MqttSource

            self.source = MqttSource(config, self.spool, self.queue.put)
            publisher = self.source

        self.worker = PrintWorker(config, self.spool, publisher, make_printer_factory(config))
        self.dir_source = (
            DirSource(config.agent.spool_dir, self.spool, self.queue.put)
            if config.agent.spool_dir
            else None
        )

    def stop(self, *_: object) -> None:
        self._stop = True
        self.queue.put("\x00stop")  # break the loop out of its blocking get

    def run(self) -> int:
        from labelfab.agent.source_mqtt import FLUSH_COMMAND

        signal.signal(signal.SIGINT, self.stop)
        signal.signal(signal.SIGTERM, self.stop)

        self.worker.recover()
        if self.dir_source:
            self.dir_source.poll()
        if self.source:
            self.source.start()

        log.info("labelfab-agent up for printer %s", self.config.agent.printer_id)
        while not self._stop:
            timeout = self.worker.seconds_until_idle_flush()
            try:
                item = self.queue.get(timeout=timeout if timeout is not None else 1.0)
            except queue.Empty:
                self.worker.tick()
                if self.dir_source:
                    self.dir_source.poll()
                continue
            if item in ("\x00stop", FLUSH_COMMAND):
                if item == FLUSH_COMMAND:
                    self.worker.flush()
                continue
            try:
                self.worker.submit(item)
            except Exception:
                log.exception("failed to process job %s", item)

        self.worker.flush()  # do not strand a half-filled strip on shutdown
        if self.source:
            self.source.stop()
        self.spool.close()
        return 0


def main(argv: list[str] | None = None) -> int:
    config = load()
    logging.basicConfig(
        level=getattr(logging, config.agent.log_level.upper(), logging.INFO),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    return Agent(config).run()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
