"""What the printer last said about itself, and when it said it.

The D30 only talks while it is being printed to. It auto-powers-off, and
``device.idle_disconnect`` defaults to true precisely because a held-open socket just
relocates the failure -- so between jobs there is nobody to ask. Every consumer of the
status topic therefore reads *remembered* device truth most of the time, and the honest
way to serve that is to remember it deliberately and say how old it is, rather than
either forgetting it or waking the printer to refresh it.

``seen_at`` is the whole point. Without it a consumer cannot distinguish "media is
loaded" from "media was loaded at some unknown time in the past", and rendering the
second as the first is exactly the failure the tri-state ``media_ok`` was introduced to
avoid, one layer along.

This also collects the six loose ``_device_*`` fields the worker used to carry and the
two places that built a ``PrinterStatus`` out of them. That duplication is why the
worker and the MQTT source disagreed: one published everything the printer had said,
the other published a hardcoded blank.
"""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict

from labelfab.contract import PrinterStatus
from labelfab.device.d30 import MODEL
from labelfab.device.feedback import DeviceFeedback


class DeviceSnapshot(BaseModel):
    """Last-known device truth, persisted in the spool and republished on connect."""

    # ``extra="ignore"`` is load-bearing rather than a default: this is stored in the
    # spool DB, which outlives package upgrades, so a row written by a different agent
    # version has to degrade to the fields it has in common. Failing to decode it would
    # take the agent down at boot, and a status field is not worth an outage.
    model_config = ConfigDict(extra="ignore", frozen=True)

    serial: str | None = None
    firmware: str | None = None
    battery_pct: int | None = None
    voltage_v: float | None = None
    media_ok: bool | None = None
    #: What the printer was complaining about the last time it was asked. Never latched
    #: -- see ``merge``.
    fault: str | None = None
    #: Epoch seconds when the printer last reported anything, or ``None`` if it never
    #: has. Stored as a float to match the rest of the spool; converted to a UTC
    #: instant at the wire boundary.
    seen_at: float | None = None

    # -- accumulation ------------------------------------------------------- #

    def merge(self, fb: DeviceFeedback, *, now: float) -> DeviceSnapshot:
        """Fold one connection's feedback in, keeping whatever it did not report.

        A connection that answered nothing must not blank out a serial we already
        know. ``fault`` is the deliberate exception: it is recomputed from scratch
        every connection, so a cleared media error clears the status instead of
        latching an error forever.

        ``seen_at`` only moves when the printer actually said something. Advancing it
        on a silent connection would claim a freshness we do not have, and this
        timestamp is the only thing standing between a consumer and treating
        three-day-old truth as live.
        """
        reported = (fb.serial, fb.firmware, fb.battery_pct, fb.voltage_v, fb.paper_ok)
        return DeviceSnapshot(
            serial=fb.serial or self.serial,
            firmware=fb.firmware or self.firmware,
            battery_pct=self.battery_pct if fb.battery_pct is None else fb.battery_pct,
            voltage_v=self.voltage_v if fb.voltage_v is None else fb.voltage_v,
            media_ok=self.media_ok if fb.paper_ok is None else fb.paper_ok,
            fault=fb.fault(),
            seen_at=now if any(v is not None for v in reported) else self.seen_at,
        )

    # -- publishing --------------------------------------------------------- #

    def settled_state(self) -> str:
        """What to publish once nothing is in flight.

        "idle" unless the printer is complaining. Kept apart from the status builder so
        that "printing" and "disconnected" -- facts about the link, not about the media
        -- are never silently rewritten into it.
        """
        return "error" if self.fault else "idle"

    def to_status(
        self,
        printer_id: str,
        *,
        state: str,
        tape_width_mm: float | None = None,
        pending_labels: int = 0,
    ) -> PrinterStatus:
        """Build the retained status message from this snapshot."""
        return PrinterStatus(
            printer_id=printer_id,
            state=state,  # type: ignore[arg-type]
            model=MODEL,
            serial=self.serial,
            firmware=self.firmware,
            battery_pct=self.battery_pct,
            voltage_v=self.voltage_v,
            media_ok=self.media_ok,
            tape_width_mm=tape_width_mm,
            pending_labels=pending_labels,
            error=self.fault,
            device_seen_at=self.seen_at_utc(),
        )

    def seen_at_utc(self) -> datetime | None:
        """``seen_at`` as a UTC instant, whole seconds.

        Truncated because sub-second precision on "when did the printer last speak" is
        noise in a payload whose primary reader is a human running ``mosquitto_sub -v``.
        """
        if self.seen_at is None:
            return None
        return datetime.fromtimestamp(self.seen_at, tz=timezone.utc).replace(microsecond=0)
