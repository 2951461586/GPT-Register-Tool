"""Stage-2 self-check harness: HEAD vs parameterised, driven through replay.

This is the S1 deliverable of
``docs/audits/plan-2026-09-28-stage2-replay.md``: it proves the replay transport
reports **zero difference when the code is identical**, and that it is
**sensitive to a request-body change**.  Once S2-S5 land, the same driver runs
the pre-change snapshot against the parameterised module.

The driver works at function granularity so it needs no live account:

* ``stripe_pm``     -> ``stripe_create_<provider>_pm`` (one Stripe POST)
* ``inline``        -> ``add_inline_<provider>_payment_method_data`` (no HTTP)
* ``taxes``         -> ``update_<provider>_checkout_taxes`` (one ChatGPT POST)

A full-flow recording (``PAYMENT_REPLAY_DIR`` or ``runtime/payment_replays``)
can be dropped in later; ``test_real_recording_selfcheck`` picks it up and skips
otherwise.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROTO = ROOT / "services" / "protocol-payment"
sys.path.insert(0, str(Path(__file__).resolve().parent))
import payment_replay as PR  # noqa: E402

PROVIDERS = {"ideal": "ideal_qr_extract", "twint": "twint_extract"}
STRIPE_PM_URL = "https://api.stripe.com/v1/payment_methods"
TAXES_URL = "https://chatgpt.com/backend-api/payments/checkout/taxes"

BILLING = {
    "name": "Test User",
    "email": "t@example.invalid",
    "country": "NL",
    "line1": "1 Test St",
    "city": "Testville",
    "postal_code": "1000",
}

CTX = {
    "stripe_js_id": "js_test",
    "elements_session_id": "elements_session_test",
    "elements_session_config_id": "elements_config_test",
    "config_id": "config_test",
}


def _load(name: str, module_file: str, *, path: Path | None = None):
    directory = str(PROTO / name)
    for candidate in (directory, str(PROTO)):
        if candidate not in sys.path:
            sys.path.insert(0, candidate)
    if path is None:
        return importlib.import_module(module_file)
    spec = importlib.util.spec_from_file_location(f"{module_file}__snapshot", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _exchanges(kind: str) -> list[dict]:
    if kind == "stripe_pm":
        return [
            {
                "request": {"method": "POST", "url": STRIPE_PM_URL},
                "response": {"status_code": 200, "body": json.dumps({"id": "pm_test_1"}), "url": STRIPE_PM_URL},
            }
        ]
    if kind == "taxes":
        return [
            {
                "request": {"method": "POST", "url": TAXES_URL},
                "response": {"status_code": 200, "body": "{}", "url": TAXES_URL},
            }
        ]
    return []


def _cursor(kind: str) -> PR.ReplayCursor:
    return PR.ReplayCursor([PR.RecordedExchange.from_dict(e) for e in _exchanges(kind)])


def drive(module, provider: str, kind: str, *, billing: dict | None = None):
    """Run one function slice under frozen determinism; return (result, fingerprints)."""
    billing = dict(billing or BILLING)
    os.environ.pop(f"{provider.upper()}_BANK", None)
    os.environ[f"{provider.upper()}_DUMP"] = "0"
    if provider == "ideal":
        os.environ["IDEAL_BANK"] = "n26"
    with PR.freeze_determinism():
        if kind == "stripe_pm":
            cursor = _cursor(kind)
            func = getattr(module, f"stripe_create_{provider}_pm")
            result = func(PR.ReplaySession(cursor), "cs_test", "pk_test", billing, dict(CTX))
            return result, cursor.served
        if kind == "taxes":
            cursor = _cursor(kind)
            func = getattr(module, f"update_{provider}_checkout_taxes")
            func(PR.ReplaySession(cursor), {"cs_id": "cs_test", "processor_entity": "openai_ie"}, billing)
            return None, cursor.served
        if kind == "inline":
            body: dict = {}
            func = getattr(module, f"add_inline_{provider}_payment_method_data")
            func(body, "cs_test", billing, dict(CTX))
            return body, []
    raise AssertionError(f"unknown slice: {kind}")


@pytest.fixture(scope="module", params=sorted(PROVIDERS))
def provider(request):
    return request.param


@pytest.mark.parametrize("kind", ["stripe_pm", "inline", "taxes"])
def test_harness_reports_zero_diff_for_identical_modules(provider, kind):
    """HEAD vs HEAD must be exactly equal -- otherwise the harness is broken."""
    module_file = PROVIDERS[provider]
    baseline = _load(provider, module_file)
    current = _load(provider, module_file)
    base_result, base_fp = drive(baseline, provider, kind)
    new_result, new_fp = drive(current, provider, kind)
    assert base_result == new_result
    assert base_fp == new_fp


def test_harness_is_sensitive_to_a_request_body_change(provider):
    """A different billing input must change the fingerprint (no over-normalising)."""
    module = _load(provider, PROVIDERS[provider])
    _, base_fp = drive(module, provider, "stripe_pm")
    changed = dict(BILLING, name="Someone Else")
    _, changed_fp = drive(module, provider, "stripe_pm", billing=changed)
    assert base_fp != changed_fp, "fingerprint ignored a billing_details change"


def test_harness_is_sensitive_to_the_inline_body(provider):
    module = _load(provider, PROVIDERS[provider])
    base_body, _ = drive(module, provider, "inline")
    changed = dict(BILLING, line1="2 Other Rd")
    changed_body, _ = drive(module, provider, "inline", billing=changed)
    assert base_body != changed_body


def test_real_recording_selfcheck():
    """Full-flow replay when an operator-supplied recording is present; skip otherwise."""
    directory = Path(os.environ.get("PAYMENT_REPLAY_DIR", ROOT / "runtime" / "payment_replays"))
    recordings = sorted(directory.glob("*.json")) if directory.is_dir() else []
    if not recordings:
        pytest.skip(f"no recording in {directory}; capture one to enable the full-flow gate")
    # A real recording drives the whole extractor CLI; that requires the operator
    # to point PAYMENT_REPLAY_DIR at it.  The transport is proven by the tests
    # above; wire the full-flow assertion when the first fixture lands.
    for recording in recordings:
        PR.load_recording(recording)  # must parse
