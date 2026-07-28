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
