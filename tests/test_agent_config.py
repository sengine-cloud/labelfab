"""Config: TOML load, environment override, topic construction."""

from __future__ import annotations

from labelfab.agent import load
from labelfab.agent.config import Config


def test_defaults_and_topic():
    cfg = Config()
    assert cfg.agent.printer_id == "d30-workshop"
    assert cfg.topic("jobs") == "se/v1/print/d30-workshop/jobs"
    assert cfg.device.raster_width_px == 96  # the only verified head width
    assert cfg.tape.rotation == 270


def test_toml_is_loaded(tmp_path):
    toml = tmp_path / "agent.toml"
    toml.write_text(
        "[agent]\nprinter_id = 'd30-bench'\n\n[strip]\nmax_labels = 8\n\n[mqtt]\nhost = 'broker'\n"
    )
    cfg = load(toml)
    assert cfg.agent.printer_id == "d30-bench"
    assert cfg.strip.max_labels == 8
    assert cfg.topic("results") == "se/v1/print/d30-bench/results"


def test_env_overrides_toml(tmp_path, monkeypatch):
    toml = tmp_path / "agent.toml"
    toml.write_text("[mqtt]\nhost = 'from-file'\npassword = 'in-file'\n")
    monkeypatch.setenv("LABELFAB_MQTT__PASSWORD", "from-env")
    cfg = load(toml)
    assert cfg.mqtt.host == "from-file"  # untouched keys still come from the file
    assert cfg.mqtt.password == "from-env"  # secret arrives from the environment


def test_density_defaults_to_light_and_reaches_the_driver():
    """The agent had no density knob at all, so D30Config's medium default always won."""
    from labelfab.agent.__main__ import make_printer_factory

    cfg = Config()
    assert cfg.device.density == 1  # light

    cfg.device.transport = "fake"
    printer = make_printer_factory(cfg)()
    assert printer.config.density == 1


def test_the_configured_tape_decides_the_tape_type_the_printer_is_told():
    """The printer stores this and acts on it: told die-cut it feeds on after every print
    hunting for a gap, which on a continuous roll runs the tape away. The agent used to
    send the die-cut value on every connect whatever ``[tape] kind`` said."""
    from labelfab.agent.__main__ import make_printer_factory
    from labelfab.device.protocol import PAPER_CONTINUOUS, PAPER_GAP

    cfg = Config()
    cfg.device.transport = "fake"
    assert cfg.tape.kind == "continuous"  # the shipped default
    assert make_printer_factory(cfg)().config.paper_type == PAPER_CONTINUOUS

    cfg.tape.kind = "gap"
    assert make_printer_factory(cfg)().config.paper_type == PAPER_GAP


def test_the_trailing_feed_is_configurable_and_bounded(tmp_path):
    import pytest
    from pydantic import ValidationError

    from labelfab.agent.__main__ import make_printer_factory

    assert Config().device.feed_lines == 23  # the vendor's own value

    toml = tmp_path / "agent.toml"
    toml.write_text("[device]\ntransport = 'fake'\nfeed_lines = 64\n")
    cfg = load(toml)
    assert make_printer_factory(cfg)().config.feed_lines == 64

    toml.write_text("[device]\nfeed_lines = 300\n")
    with pytest.raises(ValidationError):
        load(toml)


def test_preset_aliases_load_from_toml(tmp_path):
    toml = tmp_path / "agent.toml"
    toml.write_text(
        "[render]\npreset_aliases = { stock_item = 'stock_item_vertical' }\nvertical_flip = true\n"
    )
    cfg = load(toml)
    assert cfg.render.preset_aliases == {"stock_item": "stock_item_vertical"}
    assert cfg.render.vertical_flip is True
    assert Config().render.preset_aliases == {}  # nothing is remapped unless asked
    assert Config().render.vertical_flip is False


def test_the_startup_probe_is_on_and_can_be_turned_off(tmp_path):
    """On by default because it cannot wake a sleeping printer, so the only cost of a
    miss is one connect timeout. The knob exists for hosts where that is in the way."""
    assert Config().device.probe_on_start is True

    toml = tmp_path / "agent.toml"
    toml.write_text("[device]\nprobe_on_start = false\n")
    assert load(toml).device.probe_on_start is False


def test_an_unknown_density_is_rejected_at_load():
    import pytest
    from pydantic import ValidationError

    from labelfab.agent.config import DeviceSection

    with pytest.raises(ValidationError, match="not one of"):
        DeviceSection(density=3)


def test_qr_quiet_zone_defaults_to_the_spec_and_reaches_the_renderer(tmp_path, harness):
    """A value that stopped at the config section would still print at 4, so check it
    arrives in the RenderConfig the worker hands to the renderer."""
    assert Config().render.qr_quiet_zone == 4

    toml = tmp_path / "agent.toml"
    toml.write_text("[render]\nqr_quiet_zone = 2\n")
    assert load(toml).render.qr_quiet_zone == 2

    h = harness()
    h.config.render.qr_quiet_zone = 2
    assert h.worker._render_cfg().qr_quiet_zone == 2


def test_a_quiet_zone_outside_the_contract_is_rejected_at_load():
    import pytest
    from pydantic import ValidationError

    from labelfab.agent.config import RenderSection

    for bad in (0, 9):
        with pytest.raises(ValidationError):
            RenderSection(qr_quiet_zone=bad)
