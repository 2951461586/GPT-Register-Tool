"""Regression guards for the sharded config writer.

The three shard files (proxy/runtime/payment) ARE the whole application
configuration. A non-atomic ``write_text`` left a truncated JSON on crash and
took the app down; the writer must now be atomic and keep a ``.bak``. It must
also skip shards whose content did not change -- otherwise the shard mtime stops
being usable as "which part of the config was edited", which is exactly the
signal that made the provider-key cross-write hard to attribute.
"""
import os
from pathlib import Path
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
            # The trailing newline is part of the cross-language contract: the C#
            # writer appends the same one, and both sides compare payloads byte
            # for byte to decide whether a shard changed.
            assert (tmp_path / filename).read_text(encoding="utf-8").endswith("\n")


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

        # The second write MUST carry different content. With an identical payload
        # the change-detection short-circuits before os.replace is reached, the
        # patched failure below never fires, and this test would pass while
        # proving nothing about the failure path it is named for.
        shards["proxy"] = {"proxy": {"http": "B"}}

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


def test_write_shards_skips_shards_whose_content_did_not_change(tmp_path):
    """Saving one section must not rewrite the other two.

    The old writer rewrote all three unconditionally, so every save reset all
    three timestamps -- and overwrote the other shards' ``.bak`` with content
    that had not changed, discarding the last real backup.
    """
    with patch.object(config, "_CONFIG_DIR", tmp_path):
        shards = {
            "proxy": {"proxy": {"http": "A"}},
            "runtime": {"timeouts": {"request": 30}},
            "payment": {},
        }
        # Nothing exists yet, so the first save writes all three.
        assert sorted(config._write_shards(shards, tmp_path)) == [
            "payment.json", "proxy.json", "runtime.json",
        ]
        first_proxy = (tmp_path / "proxy.json").read_text(encoding="utf-8")
        first_runtime = (tmp_path / "runtime.json").read_text(encoding="utf-8")

        # An identical save writes nothing at all, and leaves no fresh backup.
        assert config._write_shards(shards, tmp_path) == []
        assert not (tmp_path / "runtime.json.bak").exists()
        assert not (tmp_path / "payment.json.bak").exists()

        # Changing one shard writes exactly that shard, and only it gets a .bak.
        shards["proxy"] = {"proxy": {"http": "B"}}
        assert config._write_shards(shards, tmp_path) == ["proxy.json"]
        assert (tmp_path / "proxy.json.bak").read_text(encoding="utf-8") == first_proxy
        assert (tmp_path / "runtime.json").read_text(encoding="utf-8") == first_runtime
        assert not (tmp_path / "runtime.json.bak").exists()


def test_write_shards_returns_only_what_it_wrote(tmp_path):
    """The return value is the only signal separating a working trim from a broken
    one: "all three files exist" is true either way."""
    with patch.object(config, "_CONFIG_DIR", tmp_path):
        shards = {"proxy": {}, "runtime": {}, "payment": {}}
        assert sorted(config._write_shards(shards, tmp_path)) == [
            "payment.json", "proxy.json", "runtime.json",
        ]
        assert config._write_shards(shards, tmp_path) == []


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


def test_default_config_dir_is_the_project_root():
    package_dir = Path(config.__file__).resolve().parent
    root = config.default_config_dir()
    assert root == config._CONFIG_DIR
    assert root == package_dir.parent
    assert root != package_dir


def test_default_config_path_parent_degenerates_once_the_legacy_file_is_gone(tmp_path, monkeypatch):
    """The trap ``default_config_dir`` exists to replace.

    ``default_config_path()`` falls through to the *bundled package* copy when the
    project-root config.json is absent, so ``.parent`` on its result silently
    reports ``sms_tool/`` as the config source -- which is what doctor did once the
    legacy file was archived.
    """
    package = tmp_path / "sms_tool"
    package.mkdir()
    monkeypatch.setattr(config, "__file__", str(package / "config.py"))

    resolved_package = Path(config.__file__).resolve().parent
    assert config.default_config_path() == resolved_package / "config.json"
    assert not config.default_config_path().is_file()
    assert config.default_config_path().parent == resolved_package
    assert config.default_config_path().parent != config.default_config_dir()
