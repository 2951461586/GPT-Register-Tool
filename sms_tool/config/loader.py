"""Shard file layout and I/O for the application configuration.

``proxy.json`` / ``runtime.json`` / ``payment.json`` are the authoritative
configuration; a legacy single ``config.json`` is migrated into them on first
load.  Everything that touches those files lives here, so "which shard owns this
key" has exactly one answer.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..kernel.paths import config_dir as _resolve_config_dir
from ..kernel.paths import migrate_legacy_root_files_if_default
from ._state import config_dir as _config_dir
from ._state import package_dir as _package_dir
from .schema import ConfigError

# ---- Config sharding (proxy.json / runtime.json / payment.json) ----
# The historical single config.json is split into three focused shard files.
# Both the desktop shell and the Python backend merge these shards at load time,
# and a legacy config.json is migrated into shards on first load.
SHARD_FILES: dict[str, str] = {
    "proxy": "proxy.json",
    "runtime": "runtime.json",
    "payment": "payment.json",
}
# Top-level config key -> owning shard name. Every key present in config.json
# must be listed here so writes can be routed to the correct shard.
SHARD_OWNERSHIP: dict[str, str] = {
    # runtime.json
    "runtime": "runtime",
    "timeouts": "runtime",
    "storage": "runtime",
    "output": "runtime",
    "account_health": "runtime",
    "registration": "runtime",
    "chatgpt": "runtime",
    "email_registration": "runtime",
    "codex_oauth": "runtime",
    # proxy.json
    "proxy": "proxy",
    "mailbox_proxy": "proxy",
    "mailbox_proxy_pool": "proxy",
    "phone_reuse": "proxy",
    "paypal_browser": "proxy",
    # payment.json
    "paypal": "payment",
    "paypal_nocard": "payment",
    "upi": "payment",
    "omakse": "payment",
    "protocol_payments": "payment",
    "kakao": "payment",
    "momo": "payment",
    "cpa_mode": "payment",
    "sub2api": "payment",
}

#: The portable template ``--init-config`` bootstraps the shards from.
EXAMPLE_CONFIG_NAME = "config.example.json"


def _deep_merge(target: dict, source: Mapping) -> None:
    for key, value in source.items():
        if key in target and isinstance(target[key], dict) and isinstance(value, dict):
            _deep_merge(target[key], value)
        else:
            target[key] = value


def _split_into_shards(data: Mapping[str, Any]) -> dict[str, dict]:
    shards: dict[str, dict] = {name: {} for name in SHARD_FILES}
    for key, value in data.items():
        owner = SHARD_OWNERSHIP.get(key, "runtime")
        shards[owner][key] = value
    return shards


def _dump_json(data: Mapping[str, Any]) -> str:
    """Canonical on-disk form of a shard.

    This is the *only* place that decides how a shard is serialized, because
    :func:`_atomic_write_json` compares its output against what is already on
    disk to decide whether to write at all. A second serializer -- differing in
    anything, even just ``ensure_ascii`` -- would make every save look like a
    change, and the "write only what changed" path would silently degrade back
    into the unconditional rewrite it replaced.
    """
    return json.dumps(dict(data), indent=2, ensure_ascii=False) + "\n"


def _read_text_or_none(path: Path) -> str | None:
    """Existing content of ``path``, or ``None`` when it is missing/unreadable.

    Read with ``utf-8-sig`` -- the same codec :func:`load_merged_config` uses --
    so a shard that picked up a BOM is compared on equal footing instead of
    looking changed on every single save.
    """
    try:
        return path.read_text(encoding="utf-8-sig")
    except OSError:
        return None


def _atomic_write_json(path: Path, data: Mapping[str, Any]) -> bool:
    """Write JSON atomically: a temp file in the *same* directory + ``os.replace``.

    The temp file lives beside the target so ``os.replace`` is a same-volume
    rename (atomic on Windows, no truncated-file window). A ``.bak`` copy of
    the previous content is kept first, because the three shard files *are*
    the whole application configuration — a crash mid-write used to leave a
    truncated JSON and take the app down with it.

    Returns ``True`` when the file was written. A payload identical to what is
    already on disk is a no-op, for two reasons that only show up in a sharded
    layout: the mtime stays put (so "which shard changed" is again a usable
    audit signal), and the previous ``.bak`` survives instead of being
    overwritten by a save that changed nothing.
    """
    if _read_text_or_none(path) == _dump_json(data):
        return False
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    backup = path.with_name(path.name + ".bak")
    if path.exists():
        try:
            shutil.copy2(path, backup)
        except OSError:
            # Best-effort only; a missing/locked backup must not fail the write.
            pass
    prefix = "." + path.stem + "."
    fd, tmp_name = tempfile.mkstemp(dir=directory, prefix=prefix, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(_dump_json(data))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return True


def _write_shards(shards: Mapping[str, Mapping], config_dir: Path) -> list[str]:
    """Persist the shards, skipping any whose content is unchanged.

    Returns the file names actually written. Callers and regression tests need
    that list: "all three files exist" stays true whether or not anything was
    written, so it cannot distinguish a working trim from a broken one.
    """
    written: list[str] = []
    for name, filename in SHARD_FILES.items():
        if _atomic_write_json(config_dir / filename, dict(shards[name])):
            written.append(filename)
    return written


def load_merged_config() -> dict[str, Any]:
    """Merge the proxy/runtime/payment shards into a single config dict.

    Honors a legacy single config.json by migrating it into shards on first
    load. Returns {} when no configuration exists.
    """
    if _config_dir() == _resolve_config_dir():
        migrate_legacy_root_files_if_default()
    shard_paths = [(name, _config_dir() / filename) for name, filename in SHARD_FILES.items()]
    if any(path.exists() for _, path in shard_paths):
        merged: dict[str, Any] = {}
        for _, path in shard_paths:
            if not path.exists():
                continue
            # Config load is allowed to raise: callers turn a malformed shard
            # into a ConfigError. Wrapping it here would hide that contract.
            # pi-lens-ignore: unchecked-throwing-call-python
            with open(path, encoding="utf-8-sig") as handle:
                # pi-lens-ignore: unchecked-throwing-call-python
                data = json.load(handle)
            if isinstance(data, dict):
                _deep_merge(merged, data)
        return merged
    legacy = _config_dir() / "config.json"
    if legacy.exists():
        # pi-lens-ignore: unchecked-throwing-call-python
        with open(legacy, encoding="utf-8-sig") as handle:
            # pi-lens-ignore: unchecked-throwing-call-python
            data = json.load(handle)
        if isinstance(data, dict):
            _write_shards(_split_into_shards(data), _config_dir())
            return dict(data)
    return {}


def default_config_dir() -> Path:
    """Directory that holds the active configuration.

    Configuration is a *set of shards* (proxy/runtime/payment) sitting in the
    project root, not a single file, so "where does config live" is a directory
    question. This exists because the previous idiom -- ``default_config_path()
    .parent`` -- silently degraded the moment the legacy project-root
    ``config.json`` was archived: the path then fell through to the *bundled
    package* fallback and callers reported ``sms_tool/`` as the config source.
    """
    return _config_dir()


def default_config_path() -> Path:
    """Legacy single-file path; prefer :func:`default_config_dir` in new code.

    Resolves independently of the process current directory, preferring the
    project-root ``config.json`` and falling back to the bundled package copy.
    With the sharded layout the project-root file normally does not exist, so
    this returns a path that is *not* a real config source -- any ``.parent``
    derived from it points at the wrong directory.
    """
    package_dir = _package_dir()
    project_file = _config_dir() / "config.json"
    package_file = package_dir / "config.json"
    return project_file if project_file.is_file() else package_file


def example_config_candidates(config_dir: Path | None = None) -> list[Path]:
    """Where ``config.example.json`` is looked for, most specific first.

    The config directory is outside the checkout (``kernel.paths.config_dir``),
    so the example is not guaranteed to sit beside the shards: a repo checkout
    has it at the root, an installed build may ship it next to the package, and
    an operator may copy it into the config directory itself.
    """
    package_dir = _package_dir()
    return [
        (Path(config_dir) if config_dir else _config_dir()) / EXAMPLE_CONFIG_NAME,
        package_dir.parent / EXAMPLE_CONFIG_NAME,
        package_dir / EXAMPLE_CONFIG_NAME,
    ]


def initialize_config_shards(
    config_dir: str | Path | None = None, source: str | Path | None = None
) -> list[tuple[str, str]]:
    """Create the config shards from ``config.example.json``, never overwriting.

    Returns ``[(filename, "written" | "skipped"), ...]`` in shard order. The
    distinction matters: "all three files exist" is true either way, so a
    caller cannot tell a first-run bootstrap from a no-op without it.

    Existing shards are left untouched. A partially configured machine is the
    normal case (an operator edited ``proxy.json`` but never had ``payment.json``),
    and merging the example over live credentials would be the worst possible
    outcome of a convenience command.
    """
    target_dir = Path(config_dir) if config_dir else _config_dir()
    example = (
        Path(source)
        if source
        else next((candidate for candidate in example_config_candidates(target_dir) if candidate.is_file()), None)
    )
    if example is None:
        searched = ", ".join(str(candidate) for candidate in example_config_candidates(target_dir))
        raise ConfigError(f"{EXAMPLE_CONFIG_NAME} not found (looked in: {searched})")

    # Loading the example is allowed to raise: a malformed template is a
    # packaging defect the operator must see, not a silently empty config.
    # pi-lens-ignore: unchecked-throwing-call-python
    raw = json.loads(example.read_text(encoding="utf-8-sig"))
    if not isinstance(raw, dict):
        raise ConfigError(f"{example} root must be a JSON object")
    shards = _split_into_shards(raw)

    results: list[tuple[str, str]] = []
    for name, filename in SHARD_FILES.items():
        path = target_dir / filename
        if path.exists():
            results.append((filename, "skipped"))
            continue
        _atomic_write_json(path, dict(shards[name]))
        results.append((filename, "written"))
    return results
