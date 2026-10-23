"""Stage-2 self-check harness: HEAD vs parameterised, driven through replay.

This is the S1 deliverable of
``docs/audits/plans/plan-2026-09-28-stage2-replay.md``: it proves the replay transport
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

ROOT = Path(__file__).resolve().parents[2]
PROTO = ROOT / "services" / "protocol-payment"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import payment_replay as PR  # noqa: E402

PROVIDERS = {"ideal": "ideal_qr_extract", "twint": "twint_extract"}
STRIPE_PM_URL = "https://api.stripe.com/v1/payment_methods"
TAXES_URL = "https://chatgpt.com/backend-api/payments/checkout/taxes"
PAGE_URL = "https://api.stripe.com/v1/payment_pages/cs_test"

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
    "locale": "nl",
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
    if kind in {"customer_data", "tax_region"}:
        return [
            {
                "request": {"method": "POST", "url": PAGE_URL},
                "response": {"status_code": 200, "body": "{}", "url": PAGE_URL},
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
        if kind in {"customer_data", "tax_region"}:
            cursor = _cursor(kind)
            func = getattr(module, f"stripe_update_{kind}")
            result = func(PR.ReplaySession(cursor), "cs_test", "pk_test", dict(CTX), billing)
            return result, cursor.served
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


# ── Stage 2, batch "stripe page writers" (2026-10-22) ───────────────────────
#
# ``stripe_update_customer_data`` and ``stripe_update_tax_region`` were
# byte-identical in ideal / twint / blik and moved into
# ``common/stripe_flow.py``. The plan requires "HEAD vs parameterised" evidence
# before such a move is accepted; a live recording is still missing, so the
# "before" side is the frozen pre-merge body captured in
# ``tests/payment/stage2_stripe_page_head.py``. Both sides are driven through the
# same replay transport with the same seams, so the only thing under test is the
# body that moved.

PAGE_WRITERS = ("customer_data", "tax_region")

#: Billing shapes that reach the optional branches (``line2``, ``state``).
BILLING_MATRIX = (
    BILLING,
    dict(BILLING, line2="Apt 4", state="NH"),
    dict(BILLING, country="CH", postal_code="8001", city="Zurich", line1="Bahnhofstrasse 1"),
)


def _load_frozen_head():
    path = ROOT / "tests" / "payment" / "stage2_stripe_page_head.py"
    spec = importlib.util.spec_from_file_location("stage2_stripe_page_head", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _seams(module):
    """Build the pre-merge seam bundle from the extractor under test.

    ``endpoints`` and ``DEFAULT_TIMEOUT`` are read off the extractor rather than
    imported here: the extractor already imports them, and a direct ``common``
    import from ``tests/`` is a ``reportMissingImports`` finding pyright cannot
    resolve (the package only exists after the child-process ``sys.path`` setup).
    """
    head = _load_frozen_head()
    return head.Seams(
        dump_http=module.dump_http,
        log=module.log,
        saved_payment_value=module.saved_payment_value,
        checkout_not_active=module.is_checkout_not_active_error,
        stripe_version=module.STRIPE_VERSION_FULL,
        stripe_payment_pages=module.endpoints.STRIPE_PAYMENT_PAGES,
        timeout=module.DEFAULT_TIMEOUT,
    )


@pytest.mark.parametrize("kind", PAGE_WRITERS)
def test_shared_page_writer_matches_the_frozen_pre_merge_body(provider, kind):
    """The moved body must produce the same request bytes and the same verdict."""
    module = _load(provider, PROVIDERS[provider])
    head = _load_frozen_head()
    seams = _seams(module)
    head_func = getattr(head, f"stripe_update_{kind}")
    for billing in BILLING_MATRIX:
        expected_cursor = _cursor(kind)
        expected = head_func(
            PR.ReplaySession(expected_cursor), "cs_test", "pk_test", dict(CTX), dict(billing), seams=seams
        )
        actual, served = drive(module, provider, kind, billing=dict(billing))
        assert served == expected_cursor.served, f"request stream diverged from the pre-merge body for {billing}"
        assert actual == expected, f"return value diverged from the pre-merge body for {billing}"


def test_page_writer_differential_is_sensitive_to_the_body(provider):
    """Guard against a vacuous comparison: a changed body must change the bytes."""
    module = _load(provider, PROVIDERS[provider])
    _, base = drive(module, provider, "tax_region", billing=BILLING)
    changed = dict(BILLING, postal_code="9999")
    _, other = drive(module, provider, "tax_region", billing=changed)
    assert base != other


@pytest.mark.parametrize("value", ["1", "0"])
def test_tax_region_carries_the_provider_saved_payment_switch(provider, value, monkeypatch):
    """The one provider-varying value in the tax body must reach the wire.

    ``saved_payment_value`` is the reason this function takes hooks at all; if a
    future edit hardcoded it, ideal and twint would silently post each other's
    value and nothing else in the suite would notice.
    """
    monkeypatch.setenv(f"{provider.upper()}_SAVED_PAYMENT_VALUE", value)
    module = _load(provider, PROVIDERS[provider])
    _, served = drive(module, provider, "tax_region")
    body = "\n".join(fingerprint[2] for fingerprint in served)
    save = '"elements_options_client[saved_payment_method][enable_save]": '
    redisplay = '"elements_options_client[saved_payment_method][enable_redisplay]": '
    assert f'{save}"{value}"' in body, body
    assert f'{redisplay}"{value}"' in body, body


# ── Stage 2, batch "pre-proxy resolver" (2026-10-23) ────────────────────────
#
# ``pre_proxy_url`` was AST-identical in ideal / twint / blik apart from the
# env-var prefix (and blik's dead ``raw = ""`` tail). The plan requires "HEAD vs
# parameterised" evidence before such a move is accepted; a live recording is
# still missing, so the "before" side is the frozen pre-merge body captured in
# ``tests/payment/stage2_pre_proxy_head.py``. Both sides receive the same
# ``environ`` mapping and the same injected normaliser/registrar, so the only
# thing under test is the body that moved.

PRE_PROXY_ENV: dict[str, tuple[str, str, str]] = {
    "ideal": ("IDEAL_PRE_PROXY", "PP_PRE_PROXY", "PP_LOCAL_PROXY"),
    "twint": ("TWINT_PRE_PROXY", "PP_PRE_PROXY", "PP_LOCAL_PROXY"),
}

#: Every env name any extractor reads, so a scenario starts from a clean slate.
PRE_PROXY_ENV_NAMES = ("IDEAL_PRE_PROXY", "TWINT_PRE_PROXY", "PP_PRE_PROXY", "PP_LOCAL_PROXY")

BLIK_PRE_PROXY_ENV = ("IDEAL_PRE_PROXY", "PP_PRE_PROXY", "PP_LOCAL_PROXY")


def _load_frozen_pre_proxy_head():
    path = ROOT / "tests" / "payment" / "stage2_pre_proxy_head.py"
    spec = importlib.util.spec_from_file_location("stage2_pre_proxy_head", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _pre_proxy_scenarios(env_names: tuple[str, str, str]) -> tuple[dict[str, str], ...]:
    """Inputs that reach every branch of the resolver (presence, disabled, trim)."""
    first, second = env_names[0], env_names[1]
    return (
        {},
        {first: "127.0.0.1:7890"},
        {first: "socks5h://user:pass@127.0.0.1:7890"},
        {first: "http://a:b@h:1"},
        {first: "off"},
        {first: "  "},
        {second: "socks5h://second:2"},
        {first: "http://a:b@h:1", second: "socks5h://second:2"},
    )


def _assert_pre_proxy_matches_head(module, env_names: tuple[str, str, str], monkeypatch) -> None:
    head = _load_frozen_pre_proxy_head()
    for scenario in _pre_proxy_scenarios(env_names):
        for name in PRE_PROXY_ENV_NAMES:
            monkeypatch.delenv(name, raising=False)
        for name, value in scenario.items():
            monkeypatch.setenv(name, value)
        expected_registered: list[str] = []
        expected = head.pre_proxy_url(
            env_names,
            seams=head.Seams(
                environ=scenario,
                normalize=module.normalize_pre_proxy_url,
                register=expected_registered.append,
            ),
        )
        actual_registered: list[str] = []
        monkeypatch.setattr(module, "register_proxy_for_redaction", actual_registered.append)
        actual = module.pre_proxy_url()
        assert actual == expected, f"value diverged for {scenario}"
        assert actual_registered == expected_registered, f"registrar diverged for {scenario}"


@pytest.mark.parametrize("provider", sorted(PRE_PROXY_ENV))
def test_extractor_pre_proxy_url_matches_the_frozen_pre_merge_body(provider, monkeypatch):
    module = _load(provider, PROVIDERS[provider])
    _assert_pre_proxy_matches_head(module, PRE_PROXY_ENV[provider], monkeypatch)


def test_blik_pre_proxy_url_matches_the_frozen_pre_merge_body(monkeypatch):
    module = _load("blik", "blik_qr_extract")
    _assert_pre_proxy_matches_head(module, BLIK_PRE_PROXY_ENV, monkeypatch)


def test_pre_proxy_differential_is_sensitive_to_the_environment(monkeypatch):
    """Guard against a vacuous comparison: a different env value must differ."""
    module = _load("ideal", PROVIDERS["ideal"])
    for name in PRE_PROXY_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(module, "register_proxy_for_redaction", lambda proxy: None)
    monkeypatch.setenv("PP_PRE_PROXY", "127.0.0.1:7890")
    proxied = module.pre_proxy_url()
    monkeypatch.setenv("PP_PRE_PROXY", "off")
    disabled = module.pre_proxy_url()
    assert proxied == "socks5h://127.0.0.1:7890"
    assert disabled == ""
    assert proxied != disabled
