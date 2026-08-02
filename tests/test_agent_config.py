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
