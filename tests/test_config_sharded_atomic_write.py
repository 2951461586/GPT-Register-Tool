"""Regression guards for the sharded config writer.

The three shard files (proxy/runtime/payment) ARE the whole application
configuration. A non-atomic ``write_text`` left a truncated JSON on crash and
took the app down; the writer must now be atomic and keep a ``.bak``.
"""
import os
from unittest.mock import patch

from sms_tool import config


def test_write_shards_creates_all_three_shards(tmp_path):
    with patch.object(config, "_CONFIG_DIR", tmp_path):
        shards = {
            "proxy": {"proxy": {"http": "http://127.0.0.1:7897"}},
            "runtime": {"timeouts": {"request": 30}},
            "payment": {"paypal": {"mode": "hosted"}},
        }
        config._write_shards(shards, tmp_path)
        for filename in config.SHARD_FILES.values():
            assert (tmp_path / filename).is_file()


def test_write_shards_keeps_a_backup_of_previous_content(tmp_path):
    with patch.object(config, "_CONFIG_DIR", tmp_path):
        shards = {"proxy": {"proxy": {"http": "A"}}, "runtime": {}, "payment": {}}
        config._write_shards(shards, tmp_path)
        first = (tmp_path / "proxy.json").read_text(encoding="utf-8")

        shards["proxy"] = {"proxy": {"http": "B"}}
        config._write_shards(shards, tmp_path)
        second = (tmp_path / "proxy.json").read_text(encoding="utf-8")

        assert first != second
        assert (tmp_path / "proxy.json.bak").is_file()
        assert (tmp_path / "proxy.json.bak").read_text(encoding="utf-8") == first


def test_write_shards_keeps_old_file_when_replace_fails(tmp_path):
    """A failed rename must never truncate the live shard."""
    with patch.object(config, "_CONFIG_DIR", tmp_path):
        shards = {"proxy": {"proxy": {"http": "A"}}, "runtime": {}, "payment": {}}
        config._write_shards(shards, tmp_path)
        old = (tmp_path / "proxy.json").read_text(encoding="utf-8")

        def _boom(*_args, **_kwargs):  # noqa: ANN002, ANN003
            raise OSError("simulated rename failure")

        with patch.object(os, "replace", _boom):
            try:
                config._write_shards(shards, tmp_path)
            except OSError:
                pass

        # Live file untouched, temp file cleaned up.
        assert (tmp_path / "proxy.json").read_text(encoding="utf-8") == old
        assert not list(tmp_path.glob(".proxy.*.tmp"))


def test_load_merged_config_after_atomic_write_reads_back(tmp_path):
    with patch.object(config, "_CONFIG_DIR", tmp_path):
        shards = {
            "proxy": {"proxy": {"http": "http://127.0.0.1:7897"}},
            "runtime": {"timeouts": {"request": 30}},
            "payment": {"paypal": {"mode": "hosted"}},
        }
        config._write_shards(shards, tmp_path)
        merged = config.load_merged_config()
        assert merged["proxy"]["http"] == "http://127.0.0.1:7897"
        assert merged["timeouts"]["request"] == 30
        assert merged["paypal"]["mode"] == "hosted"
