"""Agent configuration.

One TOML file at ``/etc/labelfab/agent.toml`` (``config|noreplace`` in the package,
so an upgrade never clobbers it), with every key overridable by an environment
variable. That last part matters for the container producers and for secrets: the
MQTT password arrives as ``LABELFAB_MQTT__PASSWORD`` from a systemd credential,
never written into the TOML.

Nesting uses a double underscore, so ``[strip] max_wait_s`` is
``LABELFAB_STRIP__MAX_WAIT_S``.

The hardware-truth constants -- ``device.raster_width_px``, ``tape.offset_px``,
``tape.rotation``, ``device.pace_factor`` -- carry the plan's safe defaults and get
their real values from day-one ``labelfab probe`` runs. They are config, not code,
precisely so that a first misprint is an edit here rather than a rebuild.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

from labelfab.device.transport import DEFAULT_TRANSPORT

#: Where the packaged config lives; overridable for tests and the dir-only mode.
DEFAULT_CONFIG_PATH = Path("/etc/labelfab/agent.toml")


class AgentSection(BaseModel):
    #: The printer this agent drives. Forms the topic subtree and the status id.
    printer_id: str = Field(default="d30-workshop", pattern=r"^[a-z0-9][a-z0-9-]*$")
    #: Root of the MQTT topic tree: ``<prefix>/<printer_id>/{jobs,results,...}``.
    topic_prefix: str = "se/v1/print"
    #: Optional local spool directory watched for ``*.json`` jobs, so a bench
    #: without the broker (or the internet) can still print by ``cp job.json``.
    spool_dir: Path | None = None
    #: How the agent renders when a job omits detail; see RenderConfig fields.
    log_level: str = "INFO"


class MqttSection(BaseModel):
    #: Empty host disables the MQTT source entirely (dir-only operation).
    host: str = ""
    port: int = 1883
    #: WebSocket is how the LAN agent reaches the in-cluster broker: the Cloudflare
    #: tunnel only routes to ``istio-ingressgateway:443``, so raw 1883 is unroutable
    #: from off-cluster. In-cluster producers use ``tcp`` to the service directly.
    transport: str = Field(default="tcp", pattern=r"^(tcp|websockets)$")
    #: WebSocket path on the ingress; ignored for the ``tcp`` transport.
    ws_path: str = "/mqtt"
    tls: bool = False
    username: str = ""
    password: str = ""
    keepalive_s: int = 30
    #: ``clean_session=false`` + a stable id is what gives at-least-once redelivery
    #: of anything published while the agent was offline.
    client_id: str = ""
    qos: int = Field(default=1, ge=0, le=2)


class DeviceSection(BaseModel):
    #: afbluetooth = Classic SPP (RFCOMM); ble = BLE/GATT for units with no SPP record.
    transport: str = Field(default=DEFAULT_TRANSPORT, pattern=r"^(afbluetooth|ble|serial|fake)$")
    mac: str = ""
    channel: int = 1
    serial_port: str = "/dev/rfcomm0"
    #: BLE write characteristic and adapter (transport = ble). ff02 is the D30's.
    ble_write_uuid: str = "0000ff02-0000-1000-8000-00805f9b34fb"
    ble_adapter: str = ""
    #: Effective print-head width. 96 (12mm) is the only value ever verified; 120
    #: (15mm) is a day-one hypothesis. Letterbox onto wider tape with tape.offset_px.
    raster_width_px: int = 96
    #: Throttle multiplier for long strips; tuned down until a strip garbles, +50%.
    pace_factor: float = 1.2
    #: A blank feed on the first print after a wake, if bring-up finds faint labels.
    wake_dummy_feed: bool = False
    #: Drop the socket between batches: the D30 auto-sleeps, so a held-open socket
    #: just relocates the failure. Reconnect-per-batch is cheaper to reason about.
    idle_disconnect: bool = True


class TapeSection(BaseModel):
    """The media physically loaded in the printer.

    This is authoritative: the agent knows what tape is in the machine, so it applies
    these to every job's geometry rather than trusting a producer that cannot know.
    Swap the tape, edit this section, done.
    """

    width_mm: float = Field(default=15.0, ge=6, le=15)
    #: ``gap`` = die-cut labels (the firmware aligns to the die gap, which forces
    #: discrete mode -- one frame per label, never a multi-label strip across gaps).
    kind: Literal["continuous", "gap"] = "continuous"
    #: Fixed label length. For die-cut media pin it to the label size (e.g. 30 for
    #: 15x30); ``"auto"`` sizes each label to its content and only suits continuous tape.
    length_mm: float | Literal["auto"] = "auto"
    #: Left offset when a narrow head prints onto wider, edge-guided tape. Answered
    #: by the alignment self-test on day one.
    offset_px: int = 0
    rotation: int = Field(default=270, ge=0, le=270)
    mirror: bool = False


class StripSection(BaseModel):
    #: Flush the pending strip this long after the most recent job with no new one.
    max_wait_s: float = 30.0
    #: Flush once the accumulated strip reaches this length.
    max_length_mm: float = 300.0
    #: Flush once this many labels have accumulated.
    max_labels: int = Field(default=24, ge=1, le=200)
    separator_mm: float = 2.0


class RenderSection(BaseModel):
    qr_base_url: str = ""
    threshold: int = Field(default=128, ge=0, le=255)


class SpoolSection(BaseModel):
    #: SQLite spool. ``synchronous=FULL`` because the point is surviving a bench
    #: power-cut; the write rate is about one job per second, so the cost is moot.
    path: Path = Path("/var/lib/labelfab/spool.db")
    #: A processed idempotency key is remembered this long to replay cached results.
    dedupe_ttl_s: int = 7 * 24 * 3600


class Config(BaseSettings):
    """Top-level agent configuration, assembled from TOML then environment."""

    model_config = SettingsConfigDict(
        env_prefix="LABELFAB_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    agent: AgentSection = AgentSection()
    mqtt: MqttSection = MqttSection()
    device: DeviceSection = DeviceSection()
    tape: TapeSection = TapeSection()
    strip: StripSection = StripSection()
    render: RenderSection = RenderSection()
    spool: SpoolSection = SpoolSection()

    #: Set at load time so settings sources can find the file; not a real field.
    _toml_path: Path = DEFAULT_CONFIG_PATH

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Precedence, highest first: explicit init args, then environment, then the
        # TOML file. Environment beats the file so a secret or a per-host override
        # never has to be written to disk.
        toml_path = getattr(settings_cls, "_toml_path_override", DEFAULT_CONFIG_PATH)
        sources: list[PydanticBaseSettingsSource] = [init_settings, env_settings]
        if Path(toml_path).is_file():
            sources.append(TomlConfigSettingsSource(settings_cls, toml_file=toml_path))
        return tuple(sources)

    def topic(self, leaf: str) -> str:
        """``jobs`` -> ``se/v1/print/d30-workshop/jobs``."""
        return f"{self.agent.topic_prefix}/{self.agent.printer_id}/{leaf}"


def _credential(name: str) -> str | None:
    """Read a systemd-provided credential, if the unit passed one.

    ``LoadCredentialEncrypted=`` drops the decrypted secret into a tmpfs directory
    named by ``$CREDENTIALS_DIRECTORY``. Reading it here is what lets the MQTT
    password be a TPM-sealed credential rather than a value written into the config.
    """
    import os

    base = os.environ.get("CREDENTIALS_DIRECTORY")
    if not base:
        return None
    path = Path(base) / name
    if path.is_file():
        return path.read_text().strip()
    return None


def load(path: Path | str | None = None) -> Config:
    """Load config, optionally from a specific TOML file."""
    if path is not None:
        Config._toml_path_override = Path(path)  # type: ignore[attr-defined]
    try:
        config = Config()
    finally:
        if path is not None:
            del Config._toml_path_override  # type: ignore[attr-defined]

    # A systemd credential wins over an (absent) config password: it is the sealed,
    # not-on-disk path the packaged unit is built around.
    if not config.mqtt.password:
        cred = _credential("mqtt_password")
        if cred:
            config.mqtt.password = cred
    return config
