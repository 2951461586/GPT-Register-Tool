"""Parity between Python's requirements proof and the compiled Rust Sentinel VM.

Verifies field-for-field that ``sms_tool.sentinel.client._requirements_token``
and the Rust FFI VM (`sentinel_vm.dll`) emit matching 25-tuple configs, clocks,
and family-gated probes (Chromium ``performance.memory`` vs Gecko
``InstallTrigger``) with zero Node.js dependency.
"""

from __future__ import annotations

import base64
import json
import re
import time
from typing import Any

from sms_tool.sentinel import surface
from sms_tool.sentinel.client import _requirements_token
from sms_tool.sentinel.native_ffi import probe_env
from sms_tool.sentinel.runner import run_sentinel_sdk
from sms_tool.transport import auth_headers
from sms_tool.transport.endpoints import AUTH_EMAIL_VERIFICATION

DEVICE_ID = "33333333-3333-4333-8333-333333333333"

POW_CHALLENGE: dict[str, Any] = {
    "token": "parity-challenge",
    "proofofwork": {"required": True, "seed": "1699999999.4242", "difficulty": "f"},
    "turnstile": {"required": False},
    "so": {"required": False},
}

_REQUIREMENTS_PREFIX = "gAAAAAC"
_ANSWER_PREFIX = "gAAAAAB"
_ATTEMPT_SLOTS = frozenset({3, 9})
_PROBE_SLOTS = frozenset({10, 11, 12})
_CLOCK_SLOTS = frozenset({13, 17})
_EXACT_SLOTS = tuple(index for index in range(25) if index not in _ATTEMPT_SLOTS | _PROBE_SLOTS | _CLOCK_SLOTS | {1})
_DATE_RE = re.compile(r"^(?P<head>.* \d{4} )(?P<clock>\d\d:\d\d:\d\d)(?P<tail> GMT.*)$")


def _profile(impersonate: str, country: str = "US") -> dict[str, Any]:
    auth_headers.set_auth_fingerprint(impersonate)
    auth_headers.set_fingerprint_geo(country)
    auth_headers.set_fingerprint_device(f"parity-{impersonate}-{country}")
    return dict(auth_headers.sentinel_fingerprint())


def _decode(token: str, prefix: str) -> list[Any]:
    body = token[len(prefix) : -len("~S")]
    return json.loads(base64.b64decode(body + "=" * (-len(body) % 4)))


def _vm_config(profile: dict[str, Any]) -> list[Any]:
    token = json.loads(
        run_sentinel_sdk(
            dict(POW_CHALLENGE),
            flow="authorize_continue",
            device_id=DEVICE_ID,
            profile=profile,
            cookie=f"oai-did={DEVICE_ID}",
            page_url=AUTH_EMAIL_VERIFICATION,
        )
    )
    return _decode(str(token["p"]), _ANSWER_PREFIX)


def _python_config(profile: dict[str, Any], device_id: str = DEVICE_ID) -> list[Any]:
    return _decode(_requirements_token(device_id, profile), _REQUIREMENTS_PREFIX)


def test_requirements_token_is_a_field_for_field_copy_of_the_rust_vm_config():
    """Every deterministic slot of Python's ``p`` equals the Rust VM's own."""
    for impersonate in ("chrome146", "firefox144", "safari18_0_ios"):
        profile = _profile(impersonate)
        vm = _vm_config(profile)
        py = _python_config(profile)
        assert len(vm) == 25 and len(py) == 25, (impersonate, len(vm), len(py))
        for index in _EXACT_SLOTS:
            assert py[index] == vm[index], f"{impersonate}: p[{index}] differs (python={py[index]!r} vm={vm[index]!r})"
        py_date, vm_date = _DATE_RE.match(str(py[1])), _DATE_RE.match(str(vm[1]))
        assert py_date and vm_date, (impersonate, py[1], vm[1])
        assert py_date.group("head") == vm_date.group("head")
        assert py_date.group("tail") == vm_date.group("tail"), (py_date.group("tail"), vm_date.group("tail"))


def test_page_clock_identity_holds_in_both_proofs():
    """``Date.now() ≈ timeOrigin + performance.now()`` holds across Python and Rust VM."""
    for impersonate in ("chrome146", "firefox144"):
        profile = _profile(impersonate)
        now_ms = time.time() * 1000
        for label, config in (("python", _python_config(profile)), ("rust_vm", _vm_config(profile))):
            origin = float(config[17])
            elapsed = float(config[13])
            assert abs(now_ms - (origin + elapsed)) <= 5_000, (
                f"{impersonate}/{label}: now-(origin+now)={now_ms - (origin + elapsed):.0f}ms"
            )
            assert abs(origin - float(profile["time_origin"])) <= 5_000, f"{impersonate}/{label}: origin drifted"


def _assert_probe(nav_probe: str, doc_probe: str, win_probe: str) -> None:
    nav_keys = set(surface.NAVIGATOR_PROBES) | set(surface.NAVIGATOR_PROFILE_PROBES)
    key, separator, rendered = nav_probe.partition(surface.SEPARATOR)
    assert key in nav_keys, nav_probe
    if key in surface.NAVIGATOR_PROFILE_PROBES:
        assert separator and rendered, f"{key} must render a value from its profile field"
    elif surface.NAVIGATOR_PROBES[key] is None:
        assert nav_probe == key
    else:
        assert rendered == surface.NAVIGATOR_PROBES[key], (nav_probe, rendered)
    assert doc_probe in surface.DOCUMENT_PROBES, doc_probe
    assert win_probe in surface.WINDOW_PROBES, win_probe


def test_probes_are_drawn_per_account_not_per_worker_thread():
    profile = _profile("chrome146")
    seen: dict[str, set[str]] = {"nav": set(), "doc": set(), "win": set()}
    for index in range(40):
        config = _python_config(profile, device_id=f"probe-device-{index}")
        nav_probe, doc_probe, win_probe = str(config[10]), str(config[11]), str(config[12])
        _assert_probe(nav_probe, doc_probe, win_probe)
        seen["nav"].add(nav_probe)
        seen["doc"].add(doc_probe)
        seen["win"].add(win_probe)
    assert len(seen["nav"]) > 1 and len(seen["doc"]) > 1 and len(seen["win"]) > 1
    first = _python_config(profile, device_id="stable-device")
    second = _python_config(profile, device_id="stable-device")
    assert [first[index] for index in (10, 11, 12)] == [second[index] for index in (10, 11, 12)]


def test_rust_vm_env_exposes_core_navigator_document_window_surface():
    dumped = probe_env({"browser_family": "chrome"})
    for key in ("userAgent", "language", "languages", "platform", "vendor", "hardwareConcurrency", "maxTouchPoints"):
        assert key in dumped["navKeys"], key
    for key in ("cookie", "URL", "documentURI", "readyState", "scripts", "body", "head"):
        assert key in dumped["docKeys"], key
    for key in ("navigator", "document", "screen", "performance", "location", "crypto", "fetch"):
        assert key in dumped["winKeys"], key


def test_heap_limit_and_install_trigger_are_family_gated():
    expectations = {
        "chrome146": (True, 0),
        "firefox144": (False, 1),
        "safari18_0": (False, 0),
        "safari18_0_ios": (False, 0),
    }
    for impersonate, (has_heap, install_trigger) in expectations.items():
        config = _python_config(_profile(impersonate))
        if has_heap:
            assert isinstance(config[2], int), (impersonate, config[2])
        else:
            assert config[2] is None, f"{impersonate} cannot have performance.memory (p[2]={config[2]!r})"
        assert config[24] == install_trigger, (impersonate, config[24])


def test_the_vm_reports_the_same_install_trigger_flag():
    firefox = _vm_config(_profile("firefox144"))
    chrome = _vm_config(_profile("chrome146"))
    assert firefox[24] == 1 and chrome[24] == 0, (firefox[24], chrome[24])
    assert firefox[2] is None and isinstance(chrome[2], int), (firefox[2], chrome[2])


def test_accept_encoding_matches_the_engine_that_can_decode_it():
    zstd = "gzip, deflate, br, zstd"
    plain = "gzip, deflate, br"
    for impersonate, expected in (
        ("chrome146", zstd),
        ("chrome124", zstd),
        ("firefox144", zstd),
        ("firefox133", zstd),
        ("safari270", zstd),
        ("safari260", plain),
        ("safari18_0", plain),
        ("safari17_0", plain),
        ("safari184_ios", plain),
    ):
        assert auth_headers.accept_encoding_for(impersonate) == expected, impersonate


def test_the_outgoing_headers_carry_the_family_specific_encoding():
    auth_headers.set_auth_fingerprint("safari18_0")
    auth_headers.set_fingerprint_geo("US")
    safari = auth_headers.openai_auth_headers("did", family="auth")
    auth_headers.set_auth_fingerprint("chrome146")
    chrome = auth_headers.openai_auth_headers("did", family="auth")
    assert safari["Accept-Encoding"] == "gzip, deflate, br"
    assert chrome["Accept-Encoding"] == "gzip, deflate, br, zstd"


def test_rust_vm_formats_date_in_profile_timezone_without_host_cjk_leak():
    for country, expected_fragment in (("US", "GMT-"), ("DE", "GMT+0"), ("IN", "GMT+0530")):
        profile = _profile("chrome146", country=country)
        vm = _vm_config(profile)
        date_str = str(vm[1])
        assert expected_fragment in date_str, (country, date_str)
        assert not re.search(r"[\u4e00-\u9fff]", date_str), (country, date_str)
