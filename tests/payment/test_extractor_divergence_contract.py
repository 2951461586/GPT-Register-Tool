"""Offline contract for the deliberate provider divergences in the extractors.

Why this exists
---------------
``scripts/provider_decoupling_ratchet.py`` freezes only the *count* of divergent
same-named functions: it makes growth fail but says nothing about which function
diverged or why.  The 2026-10-18 scan found that gap material: 27 functions are
same-named-but-different across ``services/protocol-payment/``, and merging any
of them "changes production behaviour and needs fresh evidence and an explicit
decision" (``docs/architecture.md`` Rule 19).

This file is that evidence, at the cheapest level that can actually catch a
regression:

* the exact ``(file, function)`` set is frozen, so a new divergence or a silent
  removal fails;
* each entry carries a category and a one-line reason, so the only way to touch
  the set is to classify it;
* the pure ones get semantic input/output assertions, so a "convergence" that
  keeps the name but changes the value fails immediately.

No network, provider session, proxy request or Checkout is started.  The
locale trio for iDEAL/TWINT/BLIK is *not* re-asserted here -- it already has its
own offline contract in ``test_protocol_payment_provider_differences.py``.
"""

from __future__ import annotations

import importlib
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PROTOCOL_ROOT = ROOT / "services" / "protocol-payment"
if str(PROTOCOL_ROOT) not in sys.path:
    sys.path.insert(0, str(PROTOCOL_ROOT))

sys.path.insert(0, str(ROOT / "scripts"))
import provider_decoupling_ratchet as ratchet  # noqa: E402  # type: ignore


#: ``(provider file, function)`` -> ``(category, reason)``.
#:
#: ``provider_data``  -- provider-owned data table (country, locale, entity, domain pool)
#: ``provider_state`` -- provider-owned mutable state (redaction registry, proxy health/seed)
#: ``provider_io``    -- provider-owned file/state location or dump format
#: ``provider_wire``  -- provider-owned wire value (URL mapping, error vocabulary, sid tagging)
EXPECTED_DIVERGENCES: dict[tuple[str, str], tuple[str, str]] = {
    # BLIK (12)
    ("blik/blik_qr_extract.py", "build_email"): (
        "provider_data",
        "random gmail/outlook/icloud/hotmail local-part pool; the other providers use a different seed shape",
    ),
    ("blik/blik_qr_extract.py", "normalize_country"): (
        "provider_data",
        "validates against BLIK's COUNTRY_CURRENCY table and falls back to the method's default country",
    ),
    ("blik/blik_qr_extract.py", "payment_browser_locale"): (
        "provider_data",
        "PL default; iDEAL is NL and TWINT is de-CH",
    ),
    ("blik/blik_qr_extract.py", "payment_elements_locale"): (
        "provider_data",
        "Stripe elements locale follows the BLIK browser locale",
    ),
    ("blik/blik_qr_extract.py", "payment_browser_timezone"): (
        "provider_data",
        "Europe/Warsaw default; pinned by the locale contract test as well",
    ),
    ("blik/blik_qr_extract.py", "register_proxy_for_redaction"): (
        "provider_state",
        "registers into BLIK's own redaction registry using BLIK's user:pass:host:port reading",
    ),
    ("blik/blik_qr_extract.py", "prune_proxy_state"): (
        "provider_state",
        "BLIK keeps a two-leg checkout/provider chain, so its prune signature differs",
    ),
    ("blik/blik_qr_extract.py", "record_failure_by_stage"): (
        "provider_state",
        "BLIK attributes a failure to a checkout/provider leg before penalising a proxy",
    ),
    ("blik/blik_qr_extract.py", "remove_failed_proxies"): (
        "provider_state",
        "BLIK removes by pair health, not by per-group counters",
    ),
    ("blik/blik_qr_extract.py", "is_preferred_proxy"): (
        "provider_state",
        "BLIK prefers the pair that already produced a zero-amount checkout",
    ),
    ("blik/blik_qr_extract.py", "dump_http"): (
        "provider_io",
        "BLIK's dump prefix/format is its own; shared only as a helper",
    ),
    ("blik/blik_qr_extract.py", "is_redirect_like_url"): (
        "provider_wire",
        "treats a form action field as a redirect and excludes resource URLs",
    ),
    # direct_card (1)
    ("direct_card/direct_card_extract.py", "processor_entity_for_country"): (
        "provider_data",
        "US checkout pages use openai_llc, every other country openai_ie",
    ),
    # iDEAL (2)
    ("ideal/ideal_qr_extract.py", "register_proxy_for_redaction"): (
        "provider_state",
        "iDEAL's own redaction registry; the shared helper takes it as a parameter",
    ),
    ("ideal/ideal_qr_extract.py", "dump_http"): (
        "provider_io",
        "iDEAL dump naming/format is provider-owned",
    ),
    # Kakao (7)
    ("kakao/kakao_extract.py", "register_proxy_for_redaction"): (
        "provider_state",
        "Kakao registers into its own redaction registry so its proxy lines stay redacted",
    ),
    ("kakao/kakao_extract.py", "proxy_chain_key"): (
        "provider_wire",
        "folds the 2-letter sid country suffix into the base sid and hashes 16 hex (common uses 10)",
    ),
    ("kakao/kakao_extract.py", "proxy_for_country"): (
        "provider_wire",
        "retargets country selectors and tags the sid with the uppercase country code",
    ),
    ("kakao/kakao_extract.py", "proxy_seed_file"): (
        "provider_io",
        "Kakao reads its dedicated multi-seed file, not the generic proxy seed file",
    ),
    ("kakao/kakao_extract.py", "load_proxy_state"): (
        "provider_io",
        "Kakao's proxy state lives in its own file",
    ),
    ("kakao/kakao_extract.py", "save_proxy_state"): (
        "provider_io",
        "writes the same Kakao-owned proxy state file",
    ),
    ("kakao/kakao_extract.py", "load_token"): (
        "provider_io",
        "reads a Kakao token file instead of the adapter-injected PP_TOKEN",
    ),
    # MoMo (3)
    ("momo/ac_paylink_core.py", "to_openai_pay_url"): (
        "provider_wire",
        "maps Stripe hosted checkout URLs onto pay.openai.com for the MoMo redirect",
    ),
    ("momo/momo_qr_extract.py", "normalize_proxy_url"): (
        "provider_wire",
        "udealproxy sticky lines are forced to socks5h; a bare host:port is left untouched",
    ),
    ("momo/momo_qr_extract.py", "is_user_already_paid_error"): (
        "provider_wire",
        "MoMo's already-paid vocabulary (already_subscribed, active_subscription, ...)",
    ),
    # TWINT (2)
    ("twint/twint_extract.py", "register_proxy_for_redaction"): (
        "provider_state",
        "TWINT registers into its own redaction registry so its proxy lines stay redacted",
    ),
    ("twint/twint_extract.py", "dump_http"): (
        "provider_io",
        "TWINT dump naming/format is provider-owned",
    ),
}

EXPECTED_CATEGORIES = {
    "provider_data": 6,
    "provider_state": 8,
    "provider_io": 7,
    "provider_wire": 6,
}


@pytest.fixture(scope="module")
def extractors():
    """Import the providers under test with the child-process path layout."""
    for directory in ("blik", "momo", "direct_card", "kakao"):
        source = str(PROTOCOL_ROOT / directory)
        if source not in sys.path:
            sys.path.insert(0, source)
    return {
        "blik": importlib.import_module("blik_qr_extract"),
        "momo": importlib.import_module("momo_qr_extract"),
        "momo_core": importlib.import_module("ac_paylink_core"),
        "direct_card": importlib.import_module("direct_card_extract"),
        "kakao": importlib.import_module("kakao_extract"),
    }


# --------------------------------------------------------------- inventory


def _actual_divergences() -> dict[tuple[str, str], str]:
    analysis = ratchet.analyze(ROOT)
    return {
        (pair.provider_file, pair.name): pair.common_file
        for pair in analysis.pairs
        if pair.kind == "divergent"
    }


def test_divergent_set_is_frozen():
    actual = _actual_divergences()
    expected = set(EXPECTED_DIVERGENCES)
    assert set(actual) == expected, {
        "new_divergences": sorted(set(actual) - expected),
        "removed_divergences": sorted(expected - set(actual)),
    }


def test_every_divergence_carries_a_category_and_reason():
    for (path, name), (category, reason) in EXPECTED_DIVERGENCES.items():
        assert category in EXPECTED_CATEGORIES, (path, name, category)
        assert len(reason.split()) >= 5, (path, name, "reason must explain the divergence")


def test_category_counts_are_frozen():
    counts: dict[str, int] = {category: 0 for category in EXPECTED_CATEGORIES}
    for category, _reason in EXPECTED_DIVERGENCES.values():
        counts[category] += 1
    assert counts == EXPECTED_CATEGORIES


def test_no_divergence_is_a_convergence_candidate():
    """A divergent function must not also be AST-identical to ``common/``."""
    analysis = ratchet.analyze(ROOT)
    identical = {(pair.provider_file, pair.name) for pair in analysis.pairs if pair.kind == "identical"}
    assert identical == set()


# --------------------------------------------------------------- behaviour pins


def test_blik_country_normalization_uses_its_own_table(extractors, monkeypatch):
    blik = extractors["blik"]
    monkeypatch.setenv("IDEAL_PAYMENT_METHOD", "blik")

    assert blik.normalize_country("pl") == "PL"
    assert blik.normalize_country("nl") == "NL"
    # Unknown/blank falls back to the method default, not to the raw input.
    assert blik.normalize_country("zz") == "PL"
    assert blik.normalize_country("") == "PL"


def test_blik_build_email_uses_its_own_domain_pool(extractors):
    blik = extractors["blik"]
    pattern = re.compile(r"^[a-z]+\.?[a-z]+\d{5,6}@[a-z.]+\.[a-z]+$")
    samples = [blik.build_email("Ann", "Lee") for _ in range(20)]

    assert all(pattern.match(address) for address in samples)
    assert {address.split("@", 1)[1] for address in samples} <= set(blik.EMAIL_DOMAINS)


def test_blik_redirect_predicate_honours_action_field_and_resources(extractors):
    blik = extractors["blik"]

    assert blik.is_redirect_like_url("not-a-url") is False
    assert blik.is_redirect_like_url("https://example.com/done", from_action_field=True) is True
    assert blik.is_redirect_like_url("https://chatgpt.com/assets/app.js") is False
    assert blik.is_resource_url("https://chatgpt.com/assets/app.js") is True


def test_momo_url_mapping_targets_openai_pay(extractors):
    core = extractors["momo_core"]

    assert core.to_openai_pay_url("https://checkout.stripe.com/c/pay/cs_1") == (
        "https://pay.openai.com/c/pay/cs_1"
    )
    assert core.to_openai_pay_url(core.STRIPE_PAY_PREFIX + "cs_2") == core.OPENAI_PAY_PREFIX + "cs_2"
    # Already-mapped and non-Stripe URLs are returned untouched.
    assert core.to_openai_pay_url("https://pay.openai.com/c/pay/cs_1") == (
        "https://pay.openai.com/c/pay/cs_1"
    )
    assert core.to_openai_pay_url("") == ""


def test_momo_proxy_normalization_forces_socks5h_only_for_udeal(extractors):
    momo = extractors["momo"]

    assert momo.normalize_proxy_url("gate.udealproxy.com:8000:user:pass") == (
        "socks5h://user:pass@gate.udealproxy.com:8000"
    )
    # Deliberately unlike common/: a bare host:port stays bare here.
    assert momo.normalize_proxy_url("1.2.3.4:8080") == "1.2.3.4:8080"
    assert momo.normalize_proxy_url("") == ""


def test_momo_already_paid_vocabulary(extractors):
    momo = extractors["momo"]

    assert momo.is_user_already_paid_error("error: already_subscribed") is True
    assert momo.is_user_already_paid_error("has an active plus plan") is True
    assert momo.is_user_already_paid_error("card_declined") is False


def test_direct_card_processor_entity_per_country(extractors):
    direct_card = extractors["direct_card"]

    assert direct_card.processor_entity_for_country("US") == "openai_llc"
    assert direct_card.processor_entity_for_country("us") == "openai_llc"
    assert direct_card.processor_entity_for_country("PL") == "openai_ie"
    assert direct_card.processor_entity_for_country("PL", "custom") == "custom"


def test_kakao_chain_key_folds_the_sid_country_suffix(extractors):
    kakao = extractors["kakao"]

    base = kakao.proxy_chain_key("socks5h://u-sid-abc:p@h:1")
    tagged_kr = kakao.proxy_chain_key("socks5h://u-sid-abcKR:p@h:1")
    tagged_vn = kakao.proxy_chain_key("socks5h://u-sid-abcVN:p@h:1")
    other_seed = kakao.proxy_chain_key("socks5h://u-sid-xyz:p@h:1")

    assert base == tagged_kr == tagged_vn
    assert other_seed != base
    assert re.fullmatch(r"[0-9a-f]{16}", base)
