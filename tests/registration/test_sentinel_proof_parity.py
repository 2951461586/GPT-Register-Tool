"""Parity between the hand-built requirements proof and the real ``sdk.js``.

Why this exists
---------------
``p`` (the requirements token) is sent to ``/backend-api/sentinel/req`` for every
registration, and it is the *one* proof this repo builds in Python instead of
letting the SDK build it — the challenge is fetched *with* it, so the SDK cannot
produce it.  It therefore has to be a byte-faithful copy of ``sdk.js
getConfig()``, and nothing was checking that: on 2026-11-12 three slots were
literal constants (``userAgent−…`` / ``body`` / ``crypto``) that the SDK
randomises on **every page load**, ``p[24]`` claimed Firefox's
``InstallTrigger`` for Chrome/Safari accounts, ``p[2]`` claimed
``performance.memory`` on engines that do not implement it, and ``p[13]``/``p[17]``
broke ``Date.now() ≈ timeOrigin + performance.now()`` on 98.8 % of accounts.

The two proofs of one registration are compared here field by field against the
real runner.  Fields the SDK genuinely randomises per invocation (the three
environment probes) are checked for *shape and candidate membership* instead of
equality, and the two the PoW path overwrites are excluded by name — everything
else must be identical, which is what makes this a gate rather than a smoke test.
"""

from __future__ import annotations

import base64
import json
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from sms_tool import auth_headers
from sms_tool.endpoints import AUTH_EMAIL_VERIFICATION
from sms_tool.sentinel import surface
from sms_tool.sentinel.bundle import RUNNER_PATH
from sms_tool.sentinel.client import _requirements_token
from sms_tool.sentinel.runner import run_sentinel_sdk

pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="Node.js is required by the Sentinel runner")

DEVICE_ID = "33333333-3333-4333-8333-333333333333"

#: Synthetic challenge whose PoW is satisfied on the first attempt: ``difficulty``
#: is compared as ``hash[:1] <= "f"``, which no hex digit can fail.  This is what
#: makes the real SDK emit a ``p`` (the config array) with no network at all.
POW_CHALLENGE: dict[str, Any] = {
    "token": "parity-challenge",
    "proofofwork": {"required": True, "seed": "1699999999.4242", "difficulty": "f"},
    "turnstile": {"required": False},
    "so": {"required": False},
}

#: ``sdk.js`` copies ``getConfig()`` into whichever answer buffer it writes.
#: ``gAAAAAC`` is the requirements token, ``gAAAAAB`` the PoW/enforcement one.
_REQUIREMENTS_PREFIX = "gAAAAAC"
_ANSWER_PREFIX = "gAAAAAB"

#: Slots the SDK itself overwrites on the requirements path (``[3]`` = attempt 1,
#: ``[9]`` = elapsed ms), so the PoW answer legitimately differs there.
_ATTEMPT_SLOTS = frozenset({3, 9})

#: Slots that are random *per page load* in the SDK.
_PROBE_SLOTS = frozenset({10, 11, 12})

#: Slots that carry the page clock.
_CLOCK_SLOTS = frozenset({13, 17})

#: Slots that must be byte-identical.
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


def _surface(tmp_path: Path, family: str = "chrome") -> dict[str, Any]:
    """Dump the fabricated VM environment the SDK samples its probes from."""
    script = tmp_path / "dump_surface.js"
    script.write_text(
        "\n".join(
            [
                "const { createBrowserContext } = require(process.argv[2]);",
                "const profile = JSON.parse(process.argv[3]);",
                "const { context } = createBrowserContext({",
                "  pageUrl: 'https://auth.openai.com/email-verification',",
                "  browserFamily: profile.browserFamily,",
                "  userAgent: profile.userAgent,",
                "  language: profile.language,",
                "  languages: profile.languages,",
                "  navigatorPlatform: profile.navigatorPlatform,",
                "  navigatorVendor: profile.navigatorVendor,",
                "  maxTouchPoints: profile.maxTouchPoints,",
                "  hardwareConcurrency: profile.hardwareConcurrency,",
                "  deviceMemory: 8,",
                "  devicePixelRatio: 1,",
                "  jsHeapSizeLimit: 4294705152,",
                "  timeZone: 'America/New_York',",
                "  scriptSrc: 'https://sentinel.openai.com/sentinel/20260810913b/sdk.js',",
                "  screen: { width: 1920, height: 1080, availWidth: 1920, availHeight: 1040, colorDepth: 24 },",
                "});",
                "const nav = context.window.navigator;",
                "const proto = Object.getPrototypeOf(nav);",
                "const render = (value) => { try { return String(value); } catch (error) { return null; } };",
                "process.stdout.write(JSON.stringify({",
                "  navKeys: Object.keys(proto),",
                "  navValues: Object.fromEntries(Object.keys(proto).map((key) => [key, render(nav[key])])),",
                "  docKeys: Object.keys(context.window.document),",
                "  winKeys: Object.keys(context.window),",
                "}));",
            ]
        ),
        encoding="utf-8",
    )
    payload = {
        "browserFamily": family,
        "userAgent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
        ),
        "language": "en-US",
        "languages": ["en-US", "en"],
        "navigatorPlatform": "Win32",
        "navigatorVendor": "Google Inc.",
        "maxTouchPoints": 0,
        "hardwareConcurrency": 8,
    }
    completed = subprocess.run(
        [shutil.which("node") or "node", str(script), str(RUNNER_PATH), json.dumps(payload)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    return json.loads(completed.stdout)


def test_requirements_token_is_a_field_for_field_copy_of_the_sdk_config():
    """Every deterministic slot of the hand-built ``p`` equals the SDK's own."""
    for impersonate in ("chrome146", "firefox144", "safari18_0_ios"):
        profile = _profile(impersonate)
        vm = _vm_config(profile)
        py = _python_config(profile)
        assert len(vm) == 25 and len(py) == 25, (impersonate, len(vm), len(py))
        for index in _EXACT_SLOTS:
            assert py[index] == vm[index], f"{impersonate}: p[{index}] differs (python={py[index]!r} sdk={vm[index]!r})"
        # p[1] is a wall-clock string; the two proofs are built seconds apart, so
        # everything except the seconds field must match (date, zone offset, label).
        py_date, vm_date = _DATE_RE.match(str(py[1])), _DATE_RE.match(str(vm[1]))
        assert py_date and vm_date, (impersonate, py[1], vm[1])
        assert py_date.group("head") == vm_date.group("head")
        assert py_date.group("tail") == vm_date.group("tail"), (py_date.group("tail"), vm_date.group("tail"))


def test_page_clock_identity_holds_in_both_proofs():
    """``Date.now() ≈ timeOrigin + performance.now()`` — the check a server gets free."""
    for impersonate in ("chrome146", "firefox144"):
        profile = _profile(impersonate)
        now_ms = time.time() * 1000
        for label, config in (("python", _python_config(profile)), ("sdk", _vm_config(profile))):
            origin = float(config[17])
            elapsed = float(config[13])
            assert abs(now_ms - (origin + elapsed)) <= 5_000, (
                f"{impersonate}/{label}: now-(origin+now)={now_ms - (origin + elapsed):.0f}ms"
            )
            # Both halves must describe the profile's own page, not a fresh process.
            assert abs(origin - float(profile["time_origin"])) <= 5_000, f"{impersonate}/{label}: origin drifted"


def _assert_probe(nav_probe: str, doc_probe: str, win_probe: str) -> None:
    """One probe triplet must be shape-valid and land on a declared candidate."""
    nav_keys = set(surface.NAVIGATOR_PROBES) | set(surface.NAVIGATOR_PROFILE_PROBES)
    key, separator, rendered = nav_probe.partition(surface.SEPARATOR)
    assert key in nav_keys, nav_probe
    if key in surface.NAVIGATOR_PROFILE_PROBES:
        assert separator and rendered, f"{key} must render a value from its profile field"
    elif surface.NAVIGATOR_PROBES[key] is None:
        assert nav_probe == key, "an undefined navigator value degenerates to the bare key"
    else:
        assert rendered == surface.NAVIGATOR_PROBES[key], (nav_probe, rendered)
    assert doc_probe in surface.DOCUMENT_PROBES, doc_probe
    assert win_probe in surface.WINDOW_PROBES, win_probe


def test_probes_are_drawn_per_account_not_per_worker_thread():
    """``p[10..12]`` vary with the account even when one thread is reused.

    Regression: the first version seeded the probes from
    ``profile["session_id"]``, which is a *thread-local* uuid — one batch worker
    reused it for every account it handled, so all of them drew the same
    triplet and the constant this module removes was back in a new place.
    """
    profile = _profile("chrome146")  # one thread: identical session_id throughout
    seen: dict[str, set[str]] = {"nav": set(), "doc": set(), "win": set()}
    for index in range(40):
        config = _python_config(profile, device_id=f"probe-device-{index}")
        nav_probe, doc_probe, win_probe = str(config[10]), str(config[11]), str(config[12])
        _assert_probe(nav_probe, doc_probe, win_probe)
        seen["nav"].add(nav_probe)
        seen["doc"].add(doc_probe)
        seen["win"].add(win_probe)
    assert len(seen["nav"]) > 1 and len(seen["doc"]) > 1 and len(seen["win"]) > 1, (
        "a probe that never changes is the constant this gate exists to catch"
    )
    # Same account, same triplet: one page identity per account, across sub-flows.
    first = _python_config(profile, device_id="stable-device")
    second = _python_config(profile, device_id="stable-device")
    assert [first[index] for index in (10, 11, 12)] == [second[index] for index in (10, 11, 12)]


def test_every_probe_candidate_exists_in_the_vm_with_the_pinned_rendering(tmp_path):
    """The candidate sets are only safe if the shim really has them."""
    dumped = _surface(tmp_path)
    nav_keys = set(dumped["navKeys"])
    for key, rendered in surface.NAVIGATOR_PROBES.items():
        assert key in nav_keys, f"{key} is not on the VM's Navigator.prototype"
        if rendered is not None:
            assert dumped["navValues"][key] == rendered, (key, dumped["navValues"][key], rendered)
    for key in surface.NAVIGATOR_PROFILE_PROBES:
        assert key in nav_keys, f"{key} is not on the VM's Navigator.prototype"
    missing_doc = sorted(set(surface.DOCUMENT_PROBES) - set(dumped["docKeys"]))
    missing_win = sorted(set(surface.WINDOW_PROBES) - set(dumped["winKeys"]))
    assert not missing_doc, f"document probes the VM does not have: {missing_doc}"
    assert not missing_win, f"window probes the VM does not have: {missing_win}"


def test_heap_limit_and_install_trigger_are_family_gated():
    """``p[2]``/``p[24]`` come from APIs that only some engines implement."""
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
    """Gecko-only global: the VM's own proof must agree with the hand-built one."""
    firefox = _vm_config(_profile("firefox144"))
    chrome = _vm_config(_profile("chrome146"))
    assert firefox[24] == 1 and chrome[24] == 0, (firefox[24], chrome[24])
    assert firefox[2] is None and isinstance(chrome[2], int), (firefox[2], chrome[2])


def test_accept_encoding_matches_the_engine_that_can_decode_it():
    """``zstd`` is Chrome 123+/Firefox 126+; WebKit only from Safari 26.3."""
    zstd = "gzip, deflate, br, zstd"
    plain = "gzip, deflate, br"
    for impersonate, expected in (
        ("chrome146", zstd),
        ("chrome124", zstd),
        ("firefox144", zstd),
        ("firefox133", zstd),
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
