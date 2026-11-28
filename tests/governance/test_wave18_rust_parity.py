"""Dual-mode Python vs Rust parity tests for wave-18 `rust-anything` crates.

Covers `registration-quality-rs`, `registration-device-seed-rs`, and
`sentinel-device-check-rs`, asserting identical outputs between the compiled
PyO3 fast paths and the pure-Python fallbacks.
"""

from __future__ import annotations

import json

import pytest

from sms_tool.registration import progress, sentinel_tokens, state


def test_registration_quality_aggregate_matches_python_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    if progress._NATIVE_REGISTRATION_QUALITY is None:
        pytest.skip("registration-quality-rs native extension not built in this environment")

    sample_rows = [
        json.dumps(
            {
                "source": "test",
                "success": True,
                "events": [{"stage": "auth_session", "duration_ms": 9999}],
            }
        ),
        json.dumps(
            {
                "source": "register",
                "success": True,
                "events": [
                    {"stage": "started", "duration_ms": 9100},
                    {"stage": "auth_session", "duration_ms": 570.25},
                    {"stage": "auth_retry", "duration_ms": 250.5},
                    {"stage": "finalize", "duration_ms": 14},
                ],
            }
        ),
        json.dumps(
            {
                "source": "register",
                "success": False,
                "events": [
                    {"stage": "started", "duration_ms": 10200},
                    {"stage": "auth_session", "duration_ms": 614.25},
                    {"stage": "finalize", "duration_ms": 0},
                ],
            }
        ),
    ]
    samples = [
        "",
        "   \n\n",
        "not-valid-json",
        "\n".join(sample_rows),
        "\n".join(sample_rows * 4),
    ]
    rust_out = [progress._aggregate_progress_jsonl(s) for s in samples]
    monkeypatch.setattr(progress, "_NATIVE_AGGREGATE_PROGRESS_JSONL", None)
    py_out = [progress._aggregate_progress_jsonl(s) for s in samples]
    assert rust_out == py_out


def test_registration_device_seed_matches_python_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    if state._NATIVE_DEVICE_SEED is None:
        pytest.skip("registration-device-seed-rs native extension not built in this environment")

    samples = [
        "",
        "a",
        "user@example.com",
        "user+tag@sub.domain.example.org",
        "中文用户@example.test",
        "strasse@beispiel.de",
    ]
    rust_out = [state._derive_seeded_device_ids(s) for s in samples]
    monkeypatch.setattr(state, "_NATIVE_DERIVE_SEEDED_DEVICE_IDS", None)
    py_out = [state._derive_seeded_device_ids(s) for s in samples]
    assert rust_out == py_out


def test_sentinel_device_check_matches_python_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    if sentinel_tokens._NATIVE_SENTINEL_DEVICE_CHECK is None:
        pytest.skip("sentinel-device-check-rs native extension not built in this environment")

    did = "11111111-2222-3333-4444-555555555555"
    other = "99999999-8888-7777-6666-777777777777"
    samples = [
        (did, did, json.dumps({"id": did}), json.dumps({"id": did}), f"a=1; oai-did={did}; b=2"),
        (did, "", json.dumps({"id": did}), "", f"OAI-DID={did}"),
        (did, other, json.dumps({"id": did}), "", ""),
        (did, did, "{bad-json", "", ""),
        (did, did, "[1, 2]", "", ""),
        (did, did, json.dumps({"id": other}), "", ""),
        (did, did, json.dumps({"id": did}), json.dumps({"id": other}), ""),
        (did, did, json.dumps({"id": did}), "", f"foo=bar; Oai-Did={other}; x=1"),
        (did, did, json.dumps({"id": f" {did} "}), "", f"x-oai-did={other}; oai-did={did}"),
        ("", did, json.dumps({"id": other}), json.dumps({"id": ""}), f"oai-did={other}"),
    ]
    rust_out = [sentinel_tokens._validate_sentinel_device_payload(*args) for args in samples]
    monkeypatch.setattr(sentinel_tokens, "_NATIVE_VALIDATE_SENTINEL_DEVICE_PAYLOAD", None)
    py_out = [sentinel_tokens._validate_sentinel_device_payload(*args) for args in samples]
    assert rust_out == py_out
