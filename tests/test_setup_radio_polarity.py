"""Run the interactive shell installer with isolated files and local presets."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml


@pytest.mark.skipif(os.name == "nt" or not shutil.which("jq"), reason="requires POSIX shell and jq")
@pytest.mark.parametrize("existing_polarity", [False, None])
def test_shell_setup_applies_and_resets_polarity(tmp_path, existing_polarity):
    root = Path(__file__).parents[1]
    script = tmp_path / "setup-radio-config.sh"
    # Isolate the installer's temporary menu files as well as its config.
    script.write_text(
        (root / script.name).read_text().replace("/tmp/radio_", str(tmp_path / "radio_"))
    )
    for name in ("radio-settings.json", "radio-presets.json"):
        shutil.copyfile(root / name, tmp_path / name)
    config = yaml.safe_load((root / "config.yaml.example").read_text())
    config["repeater"]["identity_key"] = "preserve-existing-identity"
    for key in ("rxen_active_high", "txen_active_high"):
        if existing_polarity is None:
            config["sx1262"].pop(key, None)
        else:
            config["sx1262"][key] = existing_polarity
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config, sort_keys=False))
    commands = tmp_path / "bin"
    commands.mkdir()
    for name in ("curl", "systemctl"):
        stub = commands / name
        stub.write_text("#!/bin/sh\nexit 0\n")
        stub.chmod(0o755)
    env = {**os.environ, "PATH": str(commands) + os.pathsep + os.environ["PATH"]}
    hardware = json.loads((root / "radio-settings.json").read_text())["hardware"]

    for key, pin, active_high in (("bq-station-g3", 23, False), ("waveshare", 12, True)):
        selection = list(hardware).index(key) + 1
        result = subprocess.run(
            ["bash", str(script), str(tmp_path)],
            input=f"\n1\n{selection}\n1\n",
            text=True,
            capture_output=True,
            env=env,
            timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        written = yaml.safe_load(config_path.read_text())
        assert written["sx1262"]["rxen_pin"] == pin
        assert written["sx1262"]["rxen_active_high"] is active_high
        assert written["sx1262"]["txen_active_high"] is True
        assert written["repeater"]["identity_key"] == "preserve-existing-identity"
