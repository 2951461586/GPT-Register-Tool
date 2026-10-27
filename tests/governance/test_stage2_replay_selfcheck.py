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


# ── Stage 2, batch "checkout session" (2026-10-28) ─────────────────────────
#
# ``create_checkout`` (109 / 109 / 102 lines -- blik's gap is one ``dump_http``
# call collapsed onto a single line) and ``warmup_approve_context`` (22 lines
# each, down to the unused ``checkout_page_url`` parameter) were AST-identical in
# ideal / twint / blik once the provider token was normalised away, and moved
# into ``common/checkout_session.py``. The plan requires "HEAD vs parameterised"
# evidence before such a move is accepted; a live recording is still missing, so
# the "before" side is the mechanically captured body in
# ``tests/payment/stage2_checkout_session_head.py`` (``git show HEAD:...``, only
# ambient names rewritten to ``seams.*``). Both sides run under the same
# environ, the same replay transport and the same injected seams, so the only
# thing under test is the body that moved.

CHECKOUT_PROVIDERS = {"blik": "blik_qr_extract", "ideal": "ideal_qr_extract", "twint": "twint_extract"}

#: ``(coupon-fallback flag, warm-up dump flag)`` per provider. blik runs the
#: *ideal* runtime env (``pay_link/adapters.py::_build_blik_env`` passes the
#: ``IDEAL`` prefix), which is why it reads ``IDEAL_*``; twint has its own pair.
CHECKOUT_FLAGS = {
    "blik": ("IDEAL_COUPON_FALLBACK_PROMO_CAMPAIGN", "IDEAL_DUMP_WARMUP"),
    "ideal": ("IDEAL_COUPON_FALLBACK_PROMO_CAMPAIGN", "IDEAL_DUMP_WARMUP"),
    "twint": ("TWINT_COUPON_FALLBACK_PROMO_CAMPAIGN", "TWINT_DUMP_WARMUP"),
}

CHECKOUT_ENV_NAMES = (
    "PP_PROMO_MODE",
    "PP_PROMO_ID",
    "PP_ENTRY_POINT",
    "PP_TRIAL_DAYS",
    "IDEAL_COUPON_FALLBACK_PROMO_CAMPAIGN",
    "IDEAL_DUMP_WARMUP",
    "TWINT_COUPON_FALLBACK_PROMO_CAMPAIGN",
    "TWINT_DUMP_WARMUP",
)

CHECKOUT_URL = "https://chatgpt.com/backend-api/payments/checkout"
SENTINEL_PING_URL = "https://chatgpt.com/backend-api/sentinel/ping"

CS_OK = {
    "checkout_session_id": "cs_test_1",
    "stripe_publishable_key": "pk_live_ABC123",
    "processor_entity": "openai_ie",
}
PROMO_SEEN = {"promo_campaign": {"promo_campaign_id": "plus-1-month-free"}}


def _ok(body: dict, url: str = CHECKOUT_URL) -> dict:
    return {
        "request": {"method": "POST", "url": url},
        "response": {"status_code": 200, "body": json.dumps(body), "url": url},
    }


def _err(status: int, text: str, url: str = CHECKOUT_URL) -> dict:
    return {
        "request": {"method": "POST", "url": url},
        "response": {"status_code": status, "body": text, "url": url},
    }


#: scenario -> (environ overrides, recorded exchanges, requested country)
CHECKOUT_SCENARIOS: dict[str, tuple[dict[str, str], list[dict], str]] = {
    "campaign_default": ({}, [_ok(CS_OK)], "NL"),
    "query_promo": ({"PP_PROMO_MODE": "query"}, [_ok(CS_OK)], "NL"),
    "trial_days": ({"PP_PROMO_MODE": "trial", "PP_TRIAL_DAYS": "7"}, [_ok(CS_OK)], "NL"),
    "promotion_code": ({"PP_PROMO_MODE": "code", "PP_PROMO_ID": "XMAS-1"}, [_ok(CS_OK)], "NL"),
    "coupon_falls_back": ({"PP_PROMO_MODE": "coupon"}, [_ok({}), _ok(PROMO_SEEN)], "NL"),
    "coupon_no_fallback_when_promo_seen": ({"PP_PROMO_MODE": "coupon"}, [_ok(PROMO_SEEN)], "NL"),
    "coupon_fallback_disabled": (
        {
            "PP_PROMO_MODE": "coupon",
            "IDEAL_COUPON_FALLBACK_PROMO_CAMPAIGN": "0",
            "TWINT_COUPON_FALLBACK_PROMO_CAMPAIGN": "0",
        },
        [_ok({})],
        "NL",
    ),
    "promo_off": ({"PP_PROMO_MODE": "off"}, [_ok(CS_OK)], "NL"),
    "promo_unknown": ({"PP_PROMO_MODE": "bogus"}, [_ok(CS_OK)], "NL"),
    "already_paid": ({}, [_err(400, '{"detail": "User is already paid"}')], "NL"),
    "http_error": ({}, [_err(503, "upstream unavailable")], "NL"),
    "missing_cs_id": ({}, [_ok({"id": "not_a_session"})], "NL"),
    "pk_from_module_default": ({}, [_ok({"checkout_session_id": "cs_test_2"})], "NL"),
    "fallback_second_fails": ({"PP_PROMO_MODE": "coupon"}, [_ok({}), _err(400, "User is already paid")], "NL"),
    "country_ch": ({}, [_ok(CS_OK)], "CH"),
    "country_unknown": ({}, [_ok(CS_OK)], "ZZ"),
}

#: scenario -> (environ overrides, recorded exchanges)
WARMUP_SCENARIOS: dict[str, tuple[dict[str, str], list[dict]]] = {
    "ping_ok": ({}, [_ok({}, SENTINEL_PING_URL)]),
    "ping_ok_dump_on": ({"IDEAL_DUMP_WARMUP": "1", "TWINT_DUMP_WARMUP": "1"}, [_ok({}, SENTINEL_PING_URL)]),
    "ping_500": ({}, [_err(500, "boom", SENTINEL_PING_URL)]),
    # An exhausted cursor makes the session raise, which is the pre-merge
    # ``except Exception`` arm: the warm-up must swallow it and log.
    "session_raises": ({}, []),
}


def _load_frozen_checkout_head():
    path = ROOT / "tests" / "payment" / "stage2_checkout_session_head.py"
    spec = importlib.util.spec_from_file_location("stage2_checkout_session_head", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _checkout_seams(module, provider: str):
    """Build the pre-merge seam bundle from the extractor under test.

    ``endpoints`` / ``CHATGPT_TIMEOUT`` / the predicates are read off the
    extractor rather than imported here: the frozen head deliberately avoids a
    ``common`` import (pyright cannot resolve that package from ``tests/``), and
    the extractor has already resolved all of them.
    """
    head = _load_frozen_checkout_head()
    coupon_flag, dump_flag = CHECKOUT_FLAGS[provider]
    return head.Seams(
        normalize_country=module.normalize_country,
        currency_for_country=module.currency_for_country,
        env_bool=module.env_bool,
        env_int=module.env_int,
        log=module.log,
        dump_http=module.dump_http,
        default_stripe_pk=module.DEFAULT_STRIPE_PK,
        coupon_fallback_flag=coupon_flag,
        warmup_dump_flag=dump_flag,
        chatgpt_base=module.endpoints.CHATGPT_BASE,
        checkout_api=module.endpoints.CHATGPT_CHECKOUT_API,
        sentinel_ping=module.endpoints.CHATGPT_SENTINEL_PING,
        timeout=module.CHATGPT_TIMEOUT,
        is_user_already_paid_error=module.is_user_already_paid_error,
        checkout_response_has_promo=module.checkout_response_has_promo,
        checkout_response_has_trial=module.checkout_response_has_trial,
    )


def _isolate_dumps(monkeypatch, module, provider: str, tmp_path) -> None:
    """Point the extractor's dumper at a throw-away dir.

    ``force=True`` writes a dump even with ``IDEAL_DUMP`` unset, and ideal/twint
    only ``mkdir`` their dump directory **at import time** -- so redirecting
    ``DUMP_DIR`` without creating it turns the diagnostic writer into a
    ``FileNotFoundError`` that both sides would raise differently (the file name
    carries an incrementing counter). blik creates the dir per call, so this
    helper is what makes the two shapes comparable.
    """
    target = tmp_path / provider
    target.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(module, "DUMP_DIR", target, raising=False)


def _apply_checkout_env(monkeypatch, environ: dict[str, str]) -> None:
    for name in CHECKOUT_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    for name, value in environ.items():
        monkeypatch.setenv(name, value)


def _cursor_of(records: list[dict]) -> PR.ReplayCursor:
    return PR.ReplayCursor([PR.RecordedExchange.from_dict(record) for record in records])


def _call(func, *args, **kwargs):
    """Run ``func`` and return ``(result, (exception type, message) | None)``.

    Both sides of the differential must fail *identically*: same type, same
    message. Comparing only successful runs would let an error-path change slip
    through, and the checkout error strings are operator-facing.
    """
    try:
        return func(*args, **kwargs), None
    except Exception as exc:  # the raised value is the observation, not a test bug
        return None, (type(exc).__name__, str(exc))


@pytest.mark.parametrize("provider", sorted(CHECKOUT_PROVIDERS))
@pytest.mark.parametrize("scenario", sorted(CHECKOUT_SCENARIOS))
def test_shared_create_checkout_matches_the_frozen_pre_merge_body(provider, scenario, monkeypatch, tmp_path):
    """The moved body must produce the same request bytes, result and error."""
    module = _load(provider, CHECKOUT_PROVIDERS[provider])
    head = _load_frozen_checkout_head()
    environ, records, country = CHECKOUT_SCENARIOS[scenario]
    _apply_checkout_env(monkeypatch, environ)
    _isolate_dumps(monkeypatch, module, provider, tmp_path)
    seams = _checkout_seams(module, provider)

    expected_cursor = _cursor_of(records)
    with PR.freeze_determinism():
        expected, expected_error = _call(head.create_checkout, PR.ReplaySession(expected_cursor), country, seams=seams)

    actual_cursor = _cursor_of(records)
    with PR.freeze_determinism():
        actual, actual_error = _call(module.create_checkout, PR.ReplaySession(actual_cursor), country)

    assert actual_cursor.served == expected_cursor.served, (
        f"request stream diverged from the pre-merge body ({scenario})"
    )
    assert actual == expected, f"return value diverged from the pre-merge body ({scenario})"
    assert actual_error == expected_error, f"error diverged from the pre-merge body ({scenario})"


@pytest.mark.parametrize("provider", sorted(CHECKOUT_PROVIDERS))
@pytest.mark.parametrize("scenario", sorted(WARMUP_SCENARIOS))
def test_shared_warmup_matches_the_frozen_pre_merge_body(provider, scenario, monkeypatch, tmp_path):
    module = _load(provider, CHECKOUT_PROVIDERS[provider])
    head = _load_frozen_checkout_head()
    environ, records = WARMUP_SCENARIOS[scenario]
    _apply_checkout_env(monkeypatch, environ)
    _isolate_dumps(monkeypatch, module, provider, tmp_path)
    seams = _checkout_seams(module, provider)
    page_url = "https://chatgpt.com/checkout/cs_test_1"

    expected_cursor = _cursor_of(records)
    with PR.freeze_determinism():
        expected, expected_error = _call(
            head.warmup_approve_context, PR.ReplaySession(expected_cursor), page_url, seams=seams
        )

    actual_cursor = _cursor_of(records)
    with PR.freeze_determinism():
        actual, actual_error = _call(module.warmup_approve_context, PR.ReplaySession(actual_cursor), page_url)

    assert actual_cursor.served == expected_cursor.served, f"request stream diverged ({scenario})"
    assert actual is None and expected is None
    assert actual_error is None and expected_error is None, "warm-up must swallow transport errors"


def test_checkout_differential_is_sensitive_to_the_request_body():
    """Guard against a vacuous comparison: another promo mode must change the bytes."""
    module = _load("ideal", CHECKOUT_PROVIDERS["ideal"])
    bodies = {}
    for scenario in ("campaign_default", "trial_days", "promotion_code"):
        environ, records, country = CHECKOUT_SCENARIOS[scenario]
        saved = {name: os.environ.get(name) for name in CHECKOUT_ENV_NAMES}
        try:
            for name in CHECKOUT_ENV_NAMES:
                os.environ.pop(name, None)
            os.environ.update(environ)
            cursor = _cursor_of(records)
            with PR.freeze_determinism():
                _call(module.create_checkout, PR.ReplaySession(cursor), country)
            bodies[scenario] = cursor.served
        finally:
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
    assert len({json.dumps(served) for served in bodies.values()}) == len(bodies)


@pytest.mark.parametrize("provider", sorted(CHECKOUT_PROVIDERS))
def test_coupon_fallback_flag_name_is_the_providers_own(provider, monkeypatch):
    """A hardcoded prefix would make one extractor read another's switch.

    ``common/checkout_session.py`` takes the env-var *name* for this reason; if a
    future edit baked ``IDEAL_COUPON_FALLBACK_PROMO_CAMPAIGN`` in, twint would
    silently follow ideal's switch and nothing else in the suite would notice.
    """
    module = _load(provider, CHECKOUT_PROVIDERS[provider])
    coupon_flag, _dump_flag = CHECKOUT_FLAGS[provider]
    seen: list[str] = []

    def record(name, default=False):
        seen.append(name)
        return default

    monkeypatch.setattr(module, "env_bool", record)
    _apply_checkout_env(monkeypatch, {"PP_PROMO_MODE": "coupon"})
    cursor = _cursor_of([_ok({}), _ok(PROMO_SEEN)])
    with PR.freeze_determinism():
        _call(module.create_checkout, PR.ReplaySession(cursor), "NL")

    assert coupon_flag in seen, seen
    other = "TWINT_COUPON_FALLBACK_PROMO_CAMPAIGN" if provider != "twint" else "IDEAL_COUPON_FALLBACK_PROMO_CAMPAIGN"
    assert other not in seen, seen


@pytest.mark.parametrize("provider", sorted(CHECKOUT_PROVIDERS))
def test_warmup_dump_flag_name_is_the_providers_own(provider, monkeypatch):
    module = _load(provider, CHECKOUT_PROVIDERS[provider])
    _coupon_flag, dump_flag = CHECKOUT_FLAGS[provider]
    seen: list[str] = []

    def record(name, default=False):
        seen.append(name)
        return default

    monkeypatch.setattr(module, "env_bool", record)
    _apply_checkout_env(monkeypatch, {})
    cursor = _cursor_of([_ok({}, SENTINEL_PING_URL)])
    with PR.freeze_determinism():
        _call(module.warmup_approve_context, PR.ReplaySession(cursor), "https://chatgpt.com/checkout/cs_test_1")

    assert dump_flag in seen, seen


@pytest.mark.parametrize("provider", sorted(CHECKOUT_PROVIDERS))
def test_default_stripe_pk_seam_reaches_the_returned_mapping(provider, monkeypatch, tmp_path):
    """The fallback key is each extractor's own module constant, read per call."""
    module = _load(provider, CHECKOUT_PROVIDERS[provider])
    monkeypatch.setattr(module, "DEFAULT_STRIPE_PK", f"pk_live_default_{provider}")
    _isolate_dumps(monkeypatch, module, provider, tmp_path)
    _apply_checkout_env(monkeypatch, {})
    cursor = _cursor_of([_ok({"checkout_session_id": "cs_test_9"})])
    with PR.freeze_determinism():
        checkout, error = _call(module.create_checkout, PR.ReplaySession(cursor), "NL")
    assert error is None
    assert checkout is not None and checkout["stripe_pk"] == f"pk_live_default_{provider}"


def test_country_and_currency_seams_stay_per_provider(monkeypatch, tmp_path):
    """A shared ``normalize_country`` / ``currency_for_country`` would change blik.

    ideal maps ``CH`` to its NL fallback (EUR), twint maps it to CHF, and blik
    resolves an unknown country through its own table and
    ``default_payment_country()``. One shared import cannot serve all three,
    which is why both functions are injected rather than imported.
    """
    currencies: dict[str, str] = {}
    for provider, module_file in sorted(CHECKOUT_PROVIDERS.items()):
        module = _load(provider, module_file)
        _isolate_dumps(monkeypatch, module, provider, tmp_path)
        _apply_checkout_env(monkeypatch, {})
        cursor = _cursor_of([_ok(CS_OK)])
        with PR.freeze_determinism():
            checkout, error = _call(module.create_checkout, PR.ReplaySession(cursor), "CH")
        assert error is None and checkout is not None
        assert checkout["billing_country"] == module.normalize_country("CH")
        assert checkout["currency"] == module.currency_for_country(checkout["billing_country"])
        currencies[provider] = checkout["currency"]
    assert len(set(currencies.values())) > 1, f"every provider resolved CH identically: {currencies}"
