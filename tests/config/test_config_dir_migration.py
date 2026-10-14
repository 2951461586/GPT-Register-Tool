"""Credential files live outside the git checkout (``sms_tool.paths.config_dir``).

The migration moves the legacy repository-root files exactly once, never
overwrites an existing file in the new location, and never runs for an explicit
``GPT_REGISTER_CONFIG_DIR`` (tests and portable installs choose their own place).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sms_tool import paths


def _seed_root(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "runtime.json").write_text('{"registration": {}}\n', encoding="utf-8")
    (root / "proxy.json").write_text("{}\n", encoding="utf-8")
    (root / "runtime.json.bak").write_text("{}\n", encoding="utf-8")
    (root / "mailbox_tokens.txt").write_text("a@example.test----tok\n", encoding="utf-8")
    (root / "README.md").write_text("not a credential\n", encoding="utf-8")


def test_config_dir_prefers_the_environment_override(tmp_path, monkeypatch):
    monkeypatch.setenv(paths.CONFIG_DIR_ENV, str(tmp_path / "custom"))
    assert paths.config_dir() == tmp_path / "custom"


def test_default_config_dir_is_per_user_and_outside_the_checkout(monkeypatch):
    monkeypatch.delenv(paths.CONFIG_DIR_ENV, raising=False)
    resolved = paths.config_dir()
    assert resolved == paths.default_user_config_dir()
    assert paths.PROJECT_ROOT not in resolved.parents


def test_migration_moves_credential_files_and_keeps_everything_else(tmp_path):
    root, dest = tmp_path / "repo", tmp_path / "user" / "config"
    _seed_root(root)

    moved = paths.migrate_legacy_root_files(dest, source=root)

    assert sorted(moved) == ["mailbox_tokens.txt", "proxy.json", "runtime.json", "runtime.json.bak"]
    assert (dest / "runtime.json").read_text(encoding="utf-8") == '{"registration": {}}\n'
    assert (dest / "mailbox_tokens.txt").read_text(encoding="utf-8") == "a@example.test----tok\n"
    assert not (root / "runtime.json").exists()
    assert not (root / "mailbox_tokens.txt").exists()
    assert (root / "README.md").exists()
    assert not list(dest.glob("*.migrating.*")), "staging files must not be left behind"


def test_migration_never_overwrites_the_new_location_and_is_idempotent(tmp_path):
    root, dest = tmp_path / "repo", tmp_path / "user" / "config"
    _seed_root(root)
    dest.mkdir(parents=True)
    (dest / "runtime.json").write_text('{"winner": true}\n', encoding="utf-8")

    first = paths.migrate_legacy_root_files(dest, source=root)
    second = paths.migrate_legacy_root_files(dest, source=root)

    assert "runtime.json" not in first
    assert (dest / "runtime.json").read_text(encoding="utf-8") == '{"winner": true}\n'
    # The legacy copy stays untouched when it lost the conflict.
    assert (root / "runtime.json").exists()
    assert second == []


def test_migration_is_a_noop_when_source_and_destination_coincide(tmp_path):
    _seed_root(tmp_path)
    assert paths.migrate_legacy_root_files(tmp_path, source=tmp_path) == []
    assert (tmp_path / "runtime.json").exists()


def test_explicit_override_never_pulls_files_out_of_the_repository(tmp_path, monkeypatch):
    monkeypatch.setenv(paths.CONFIG_DIR_ENV, str(tmp_path / "chosen"))
    monkeypatch.setattr(paths, "PROJECT_ROOT", tmp_path / "repo")
    _seed_root(tmp_path / "repo")

    assert paths.migrate_legacy_root_files_if_default() == []
    assert (tmp_path / "repo" / "runtime.json").exists()


@pytest.mark.parametrize("value", ["", None, "mailbox_tokens.txt"])
def test_resolve_config_file_defaults_into_the_config_directory(tmp_path, monkeypatch, value):
    monkeypatch.setenv(paths.CONFIG_DIR_ENV, str(tmp_path))
    monkeypatch.chdir(tmp_path.parent)
    assert paths.resolve_config_file(value) == tmp_path / "mailbox_tokens.txt"


def test_resolve_config_file_honours_absolute_paths_verbatim(tmp_path, monkeypatch):
    monkeypatch.setenv(paths.CONFIG_DIR_ENV, str(tmp_path / "cfg"))
    absolute = tmp_path / "elsewhere" / "pool.txt"
    assert paths.resolve_config_file(str(absolute)) == absolute


def test_resolve_config_file_falls_back_to_a_legacy_working_directory_file(tmp_path, monkeypatch):
    cfg, cwd = tmp_path / "cfg", tmp_path / "cwd"
    cwd.mkdir()
    (cwd / "pool.txt").write_text("x\n", encoding="utf-8")
    monkeypatch.delenv(paths.CONFIG_DIR_ENV, raising=False)
    monkeypatch.setattr(paths, "default_user_config_dir", lambda: cfg)
    monkeypatch.setattr(paths, "PROJECT_ROOT", tmp_path / "repo")
    monkeypatch.chdir(cwd)
    assert paths.resolve_config_file("pool.txt") == cwd / "pool.txt"
    (cfg).mkdir()
    (cfg / "pool.txt").write_text("y\n", encoding="utf-8")
    assert paths.resolve_config_file("pool.txt") == cfg / "pool.txt"


def test_an_explicit_override_ignores_legacy_files(tmp_path, monkeypatch):
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    (cwd / "pool.txt").write_text("x\n", encoding="utf-8")
    monkeypatch.setenv(paths.CONFIG_DIR_ENV, str(tmp_path / "cfg"))
    monkeypatch.chdir(cwd)
    assert paths.resolve_config_file("pool.txt") == tmp_path / "cfg" / "pool.txt"
