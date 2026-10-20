"""Cache semantics for the unread-config-key scan.

``--doctor`` runs the scan on the desktop first launch, and it parses every
production source file (~750 files). ``cached_unread_config_keys`` memoizes the
answer on a ``(path, mtime_ns, size)`` fingerprint so only a real edit pays the
scan again. These tests pin the three properties that matter:

* a cache hit does not recompute (proved by making the scan raise);
* an edit invalidates the cache without a TTL;
* a corrupt cache degrades to a scan instead of failing ``--doctor``.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from sms_tool import config_usage


def _tree(tmp_path):
    (tmp_path / "sms_tool").mkdir()
    (tmp_path / "services").mkdir()
    (tmp_path / "sms_tool" / "mod.py").write_text('KEY = "alpha"\n', encoding="utf-8")
    (tmp_path / "config.json").write_text(json.dumps({"alpha": 1, "beta": 2}), encoding="utf-8")
    (tmp_path / "config.example.json").write_text(json.dumps({"alpha": 1}), encoding="utf-8")
    return tmp_path


def _keys(root, cache):
    return {key.path for key in config_usage.cached_unread_config_keys(root, cache)}


def test_cache_hit_does_not_rescan(tmp_path):
    root = _tree(tmp_path)
    cache = tmp_path / "cache.json"

    assert _keys(root, cache) == {"beta"}
    assert cache.is_file()

    with patch.object(
        config_usage,
        "unread_config_keys",
        side_effect=AssertionError("a fingerprint hit must not rescan"),
    ):
        assert _keys(root, cache) == {"beta"}


def test_source_edit_invalidates_cache(tmp_path):
    root = _tree(tmp_path)
    cache = tmp_path / "cache.json"
    assert _keys(root, cache) == {"beta"}

    # Wiring the key up must be visible on the very next doctor run, not after
    # some TTL. The per-process literal cache is a separate layer and has to be
    # cleared explicitly in a test.
    (root / "sms_tool" / "mod.py").write_text('KEY = "alpha"\nOTHER = "beta"\n', encoding="utf-8")
    config_usage.source_string_literals.cache_clear()

    assert _keys(root, cache) == set()


def test_corrupt_cache_falls_back_to_a_scan(tmp_path):
    root = _tree(tmp_path)
    cache = tmp_path / "cache.json"
    cache.write_text("{not json", encoding="utf-8")

    assert _keys(root, cache) == {"beta"}


def test_cache_schema_drift_falls_back_to_a_scan(tmp_path):
    root = _tree(tmp_path)
    cache = tmp_path / "cache.json"
    cache.write_text(
        json.dumps({"schema": "some-old-schema", "fingerprint": "x", "keys": []}),
        encoding="utf-8",
    )

    assert _keys(root, cache) == {"beta"}
