"""Pin the optional live refresh of ``sdk.js`` (``resolve_sdk_path``).

The vendored build stays authoritative and hash-pinned.  The refresh exists for
the one case the pin cannot cover: OpenAI rotates ``/sentinel/<version>/sdk.js``
and the configured version names a build we do not ship.  Everything here is
offline -- the download is exercised through a fake session so no test can reach
sentinel.openai.com.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from sms_tool import config as config_module
from sms_tool.sentinel import bundle

REAL_SDK = b"/* SentinelSDK synthetic test fixture */\nwindow.SentinelSDK = {};\n"
OTHER_VERSION = "20990101913b"


class _Response:
    def __init__(self, status_code: int, content: bytes) -> None:
        self.status_code = status_code
        self.content = content


class _Session:
    """One-shot fake curl_cffi session: records every URL it was asked for."""

    def __init__(self, response: _Response | Exception) -> None:
        self.response = response
        self.urls: list[str] = []

    def get(self, url: str, **_kwargs):  # noqa: ANN001 - mirrors curl_cffi's signature
        self.urls.append(url)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    def close(self) -> None:
        return


@pytest.fixture()
def cache(tmp_path, monkeypatch):
    monkeypatch.setenv(bundle.CACHE_ENV, str(tmp_path / "sentinel-sdk"))
    return tmp_path / "sentinel-sdk"


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    for name in (bundle.AUTO_UPDATE_ENV, bundle.REFRESH_ENV, bundle.CACHE_ENV):
        monkeypatch.delenv(name, raising=False)


def _config(**email) -> dict:
    return {"email_registration": dict(email)}


def _with_config(config: dict):
    return patch.object(config_module, "current_config_data", return_value=config)


def test_pinned_version_returns_the_vendored_file(cache) -> None:
    with _with_config(_config(sentinel_version=bundle.DEFAULT_SENTINEL_VERSION)):
        assert bundle.resolve_sdk_path(verify_vendored=False) == bundle.NATIVE_LIB_PATH


def test_drift_without_auto_update_still_runs_the_vendored_build(cache, caplog) -> None:
    """A claim/bytes mismatch is loud but not fatal."""
    with _with_config(_config(sentinel_version=OTHER_VERSION)):
        with caplog.at_level("WARNING"):
            path = bundle.resolve_sdk_path(verify_vendored=False)
    assert path == bundle.NATIVE_LIB_PATH
    assert any("!= pinned build" in record.message for record in caplog.records)


def test_auto_update_downloads_verifies_and_caches(cache) -> None:
    session = _Session(_Response(200, REAL_SDK))
    with _with_config(_config(sentinel_sdk_auto_update=True, sentinel_version=OTHER_VERSION)):
        path = bundle.resolve_sdk_path(session=session, verify_vendored=False)
        again = bundle.resolve_sdk_path(session=session, verify_vendored=False)
    assert session.urls == [bundle.SDK_URL_TEMPLATE.format(version=OTHER_VERSION)], "second call must hit the cache"
    assert path == again
    assert path.parent == cache / OTHER_VERSION
    manifest = json.loads((path.parent / "current.json").read_text(encoding="utf-8"))
    assert manifest["sha256"] == hashlib.sha256(REAL_SDK.replace(b"\r\n", b"\n")).hexdigest()
    assert path.read_bytes() == REAL_SDK


def test_env_flag_overrides_config(cache, monkeypatch) -> None:
    monkeypatch.setenv(bundle.AUTO_UPDATE_ENV, "off")
    with _with_config(_config(sentinel_sdk_auto_update=True)):
        assert bundle.sdk_auto_update() is False
    monkeypatch.setenv(bundle.AUTO_UPDATE_ENV, "1")
    with _with_config(_config(sentinel_sdk_auto_update=False)):
        assert bundle.sdk_auto_update() is True


def test_refresh_seconds_floors_and_defaults(cache, monkeypatch) -> None:
    with _with_config(_config()):
        assert bundle.sdk_refresh_seconds() == bundle.DEFAULT_REFRESH_SECONDS
    with _with_config(_config(sentinel_sdk_refresh_seconds=5)):
        assert bundle.sdk_refresh_seconds() == 60, "a sub-minute refresh would hammer the CDN"
    monkeypatch.setenv(bundle.REFRESH_ENV, "not-a-number")
    with _with_config(_config(sentinel_sdk_refresh_seconds=7200)):
        assert bundle.sdk_refresh_seconds() == bundle.DEFAULT_REFRESH_SECONDS


def test_html_error_page_is_rejected(cache) -> None:
    session = _Session(_Response(200, b"<!doctype html><html>captive portal</html>"))
    with _with_config(_config(sentinel_sdk_auto_update=True, sentinel_version=OTHER_VERSION)):
        with pytest.raises(bundle.SentinelBundleError, match="sentinel_sdk_unavailable"):
            bundle.resolve_sdk_path(session=session, verify_vendored=False)


def test_non_200_and_missing_marker_use_the_pinned_reason_codes(cache) -> None:
    with pytest.raises(bundle.SentinelBundleError, match="sentinel_sdk_fetch_rejected:404"):
        bundle.fetch_sdk(OTHER_VERSION, session=_Session(_Response(404, b"")))
    with pytest.raises(bundle.SentinelBundleError, match="sentinel_sdk_fetch_invalid"):
        bundle.fetch_sdk(OTHER_VERSION, session=_Session(_Response(200, b"var x = 1;")))


def test_invalid_version_never_reaches_the_network() -> None:
    session = _Session(_Response(200, REAL_SDK))
    with pytest.raises(bundle.SentinelBundleError, match="sentinel_sdk_invalid_version"):
        bundle.fetch_sdk("../../etc", session=session)
    assert session.urls == []


def test_a_failed_refresh_falls_back_to_the_stale_cache_entry(cache) -> None:
    good = _Session(_Response(200, REAL_SDK))
    with _with_config(_config(sentinel_sdk_auto_update=True, sentinel_version=OTHER_VERSION)):
        cached = bundle.resolve_sdk_path(session=good, verify_vendored=False)
        # Age the manifest past the refresh window, then fail the fetch.
        manifest_path = cached.parent / "current.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["fetched_at"] = 0
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        broken = _Session(ConnectionError("proxy down"))
        assert bundle.resolve_sdk_path(session=broken, verify_vendored=False) == cached


def test_a_failed_refresh_on_the_pinned_version_uses_the_vendored_file(cache) -> None:
    broken = _Session(ConnectionError("proxy down"))
    with _with_config(_config(sentinel_sdk_auto_update=True, sentinel_version=bundle.DEFAULT_SENTINEL_VERSION)):
        assert bundle.resolve_sdk_path(session=broken, verify_vendored=False) == bundle.NATIVE_LIB_PATH


def test_a_tampered_cache_entry_is_not_served(cache) -> None:
    """A cache file whose bytes no longer hash to the manifest is a miss.

    Served bytes and claimed bytes must be one thing, so the tampered entry is
    discarded; the failed re-fetch then surfaces the stable reason code instead
    of silently minting from an unverified script.
    """
    session = _Session(_Response(200, REAL_SDK))
    with _with_config(_config(sentinel_sdk_auto_update=True, sentinel_version=OTHER_VERSION)):
        path = bundle.resolve_sdk_path(session=session, verify_vendored=False)
    path.write_bytes(REAL_SDK + b"\n// tampered\n")
    with _with_config(_config(sentinel_sdk_auto_update=True, sentinel_version=OTHER_VERSION)):
        with pytest.raises(bundle.SentinelBundleError, match="sentinel_sdk_unavailable"):
            bundle.resolve_sdk_path(session=_Session(_Response(500, b"")), verify_vendored=False)


def test_cache_root_defaults_under_the_runtime_directory(monkeypatch) -> None:
    monkeypatch.delenv(bundle.CACHE_ENV, raising=False)
    monkeypatch.setattr(
        config_module, "current_config_data", lambda: {"runtime": {"directory": "runtime"}}, raising=False
    )
    assert Path(os.fspath(bundle.cache_root())).name == "sentinel-sdk"
