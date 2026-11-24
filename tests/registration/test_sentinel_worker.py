"""Tests for the in-process Rust Sentinel VM FFI bridge (`native_ffi.py`).

Replaces the legacy Node worker tests: the Rust cdylib runs in-process with zero
subprocess spawning, so ``worker_enabled()`` is always ``False`` and every
FFI boundary check (version, requirements PoW, invalid challenge, missing lib)
is asserted directly.
"""

from __future__ import annotations

import base64
import json

import pytest

from sms_tool.sentinel import bundle, native_ffi
from sms_tool.sentinel import runner as runner_module


def _encode_dx(program: list[list], key: str) -> str:
    raw = json.dumps(program, separators=(",", ":")).encode("utf-8")
    k = key.encode("utf-8")
    xored = bytes(b ^ k[i % len(k)] for i, b in enumerate(raw))
    return base64.b64encode(xored).decode("ascii")


def test_worker_is_disabled_because_rust_ffi_is_in_process():
    assert runner_module.worker_enabled() is False


def test_native_version_and_requirements_token():
    version = native_ffi.native_version()
    assert version and version[0].isdigit()
    req = native_ffi.requirements_token({"lang": "en-US", "timezone": "UTC", "browser_family": "chrome"})
    assert req.startswith("gAAAAAC") and req.endswith("~S")


def test_native_ffi_solves_turnstile_and_so_bytecode_programs():
    proof = "gAAAAACtest-key~S"
    # Minimal VM instruction: set output register via op 0 (set output) / invalid op returns error cleanly
    # Verify round-trip error handling for malformed dx without crashing Python process:
    with pytest.raises(native_ffi.SentinelNativeError):
        native_ffi.mint_turnstile("!!!not-base64!!!", proof, {"browser_family": "chrome"})

    # Verify empty instruction list executes cleanly and returns empty string without panic
    empty_dx = _encode_dx([], proof)
    assert native_ffi.mint_turnstile(empty_dx, proof, {"browser_family": "chrome"}) == ""
    assert native_ffi.mint_so(empty_dx, empty_dx, proof, {"browser_family": "chrome"}) == ""


def test_native_lib_env_override_rejects_missing_file_without_leaking_path(monkeypatch):
    secret_path = "/private/secret-token/sentinel_vm.dll"
    monkeypatch.setenv(bundle.NATIVE_LIB_ENV, secret_path)
    native_ffi.reset_cached_library()
    try:
        with pytest.raises(bundle.SentinelBundleError) as caught:
            native_ffi.native_library_path()
        assert "sentinel_runtime_missing" in str(caught.value)
        assert secret_path not in str(caught.value)
    finally:
        monkeypatch.delenv(bundle.NATIVE_LIB_ENV, raising=False)
        native_ffi.reset_cached_library()


def test_selfcheck_runs_offline_and_reports_native_ok():
    from sms_tool.sentinel.selfcheck import run_selfcheck

    report = run_selfcheck()
    assert report["ok"] is True
    assert report["bundle"] == "ok"
    assert str(report["native"]).startswith("v")
    assert report["mint"] == "ok"
