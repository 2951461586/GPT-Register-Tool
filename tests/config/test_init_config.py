"""``--init-config`` bootstraps the config shards and never overwrites them.

Why this exists
---------------
The canonical config is the three shards under the per-user config directory,
and until now a fresh machine had no command that created them: the operator
copied ``config.example.json`` by hand or relied on the legacy single-file
migration. Worse, ``initialize_runtime_config`` runs before argument parsing, so
an empty config crashed the CLI with ``chatgpt must be an object`` before
``--doctor`` (the command that is supposed to explain the problem) could run.

Two contracts are pinned here: the shards are created from the example, and a
machine that already has one is left alone -- merging the example over live
credentials would be the worst possible outcome of a convenience command.
"""

from __future__ import annotations

import json
import sys
from unittest.mock import patch

import pytest

from sms_tool import cli, config, doctor

EXAMPLE = {
    "proxy": {"default": "http://127.0.0.1:7897"},
    "timeouts": {"request": 30},
    "paypal": {"mode": "hosted"},
}


def write_example(tmp_path):
    example = tmp_path / "config.example.json"
    example.write_text(json.dumps(EXAMPLE), encoding="utf-8")
    return example


def test_writes_all_three_shards(tmp_path):
    example = write_example(tmp_path)
    target = tmp_path / "cfg"

    results = config.initialize_config_shards(target, example)

    assert [status for _, status in results] == ["written"] * len(config.SHARD_FILES)
    for filename in config.SHARD_FILES.values():
        assert (target / filename).is_file()
    assert json.loads((target / "proxy.json").read_text(encoding="utf-8")) == {"proxy": EXAMPLE["proxy"]}


def test_second_run_skips_everything(tmp_path):
    example = write_example(tmp_path)
    target = tmp_path / "cfg"

    config.initialize_config_shards(target, example)
    results = config.initialize_config_shards(target, example)

    assert [status for _, status in results] == ["skipped"] * len(config.SHARD_FILES)


def test_an_existing_shard_is_never_overwritten(tmp_path):
    example = write_example(tmp_path)
    target = tmp_path / "cfg"
    target.mkdir()
    live = target / "proxy.json"
    live.write_text(json.dumps({"proxy": {"default": "http://live:1"}}), encoding="utf-8")

    results = config.initialize_config_shards(target, example)

    assert dict(results)["proxy.json"] == "skipped"
    assert json.loads(live.read_text(encoding="utf-8"))["proxy"]["default"] == "http://live:1"


def test_a_missing_example_names_every_searched_path(tmp_path):
    target = tmp_path / "cfg"

    with (
        patch.object(config, "_CONFIG_DIR", target),
        patch.object(config, "example_config_candidates", return_value=[tmp_path / "nowhere.json"]),
    ):
        with pytest.raises(config.ConfigError) as error:
            config.initialize_config_shards(target)

    assert "config.example.json" in str(error.value)
    assert "nowhere.json" in str(error.value)


def test_the_example_is_found_in_the_config_dir_first(tmp_path):
    target = tmp_path / "cfg"
    target.mkdir()
    (target / "config.example.json").write_text(json.dumps(EXAMPLE), encoding="utf-8")

    results = config.initialize_config_shards(target)

    assert [status for _, status in results] == ["written"] * len(config.SHARD_FILES)


def test_the_cli_handles_init_config_before_loading_config(monkeypatch, capsys):
    """The whole point: a machine with no config must still be able to run it."""

    def explode():
        raise AssertionError("initialize_runtime_config must not run for --init-config")

    monkeypatch.setattr(sys, "argv", ["sms_tool", "--init-config"])
    monkeypatch.setattr(cli, "initialize_runtime_config", explode)
    monkeypatch.setattr(cli, "_init_config_command", lambda: 0)

    with pytest.raises(SystemExit) as exit_info:
        cli.main()

    assert exit_info.value.code == 0


def test_init_config_reports_a_missing_example(tmp_path, monkeypatch, capsys):
    def missing():
        raise config.ConfigError("config.example.json not found")

    # Patch where it is looked up: `cli` binds the name at import time.
    monkeypatch.setattr(cli, "initialize_config_shards", missing)

    assert cli._init_config_command() == 2
    assert "config.example.json not found" in capsys.readouterr().err


def test_doctor_warns_about_missing_shards(tmp_path):
    checks = {check["name"]: check for check in doctor._probe_config({}, str(tmp_path))}

    assert checks["config_shards"]["status"] == "warn"
    assert "--init-config" in checks["config_shards"]["hint"]


def test_doctor_is_green_once_the_shards_exist(tmp_path):
    example = write_example(tmp_path)
    target = tmp_path / "cfg"
    config.initialize_config_shards(target, example)

    checks = {check["name"]: check for check in doctor._probe_config({}, str(target))}

    assert checks["config_shards"]["status"] == "ok"
