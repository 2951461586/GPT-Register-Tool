"""``StageProxies`` -- one immutable value for the seven checkout egress stages.

Why this exists
---------------
The seven proxy strings used to travel as seven independent keyword arguments
between ``pay_link.registry``, ``generate_pp_link`` and ``PPLinkExtractor``, and
the adapter kept its own copy of their names. That copy is how
``max_stage_retries`` came to be forwarded to a function that never accepted it.

These tests pin the three properties the convergence depends on:

* the ``Mapping`` surface is real, because every existing call site still reads
  ``proxies["checkout"]`` / ``proxies.get("stripe_init", fallback)``;
* ``from_mapping`` / ``from_options`` / ``overridden`` express the three ways a
  stage value arrives (config, adapter options, explicit keyword) and the
  precedence between them -- an empty value means "not supplied", which is the
  rule ``_resolve_stage_proxy`` already applies;
* the value is frozen and hashable, so it cannot be mutated into a stale copy
  halfway through a flow.
"""

from __future__ import annotations

import pytest

from sms_tool.paypal.proxy import STAGE_PROXY_STAGES, StageProxies


def test_all_seven_stages_exist_and_default_to_empty():
    proxies = StageProxies()
    assert tuple(proxies) == STAGE_PROXY_STAGES
    assert len(proxies) == 7
    assert proxies.as_dict() == {stage: "" for stage in STAGE_PROXY_STAGES}


def test_mapping_access_keeps_the_old_call_sites_working():
    proxies = StageProxies.from_mapping({"checkout": "http://c", "provider": "http://p"})
    assert proxies["checkout"] == "http://c"
    assert proxies["provider"] == "http://p"
    assert proxies.get("stripe_init") == ""
    assert proxies.get("stripe_init", "fallback") == ""
    assert proxies.get("nosuchstage", "fallback") == "fallback"


def test_unknown_stage_lookup_raises_keyerror_like_a_mapping():
    proxies = StageProxies()
    with pytest.raises(KeyError):
        proxies["checkout_v2"]


def test_from_mapping_ignores_non_mappings_and_trims_whitespace():
    assert StageProxies.from_mapping(None).as_dict() == StageProxies().as_dict()
    assert StageProxies.from_mapping(["checkout"]).as_dict() == StageProxies().as_dict()
    assert StageProxies.from_mapping({"checkout": "  http://c  "})["checkout"] == "http://c"


def test_from_options_reads_the_adapter_spelling():
    proxies = StageProxies.from_options(
        {
            "checkout_proxy": "http://c",
            "provider_proxy": "http://p",
            "approve_proxy": "http://a",
            "approve_country": "JP",
            "stage_proxies": {"checkout": "http://ignored"},
        }
    )
    assert proxies.as_dict() == {
        "checkout": "http://c",
        "promotion": "",
        "provider": "http://p",
        "stripe_init": "",
        "payment_method": "",
        "confirm": "",
        "approve": "http://a",
    }


def test_overridden_lets_a_non_empty_value_win_and_ignores_empty_ones():
    base = StageProxies.from_mapping({"checkout": "http://configured", "provider": "http://configured-p"})
    merged = base.overridden({"checkout": "http://explicit", "provider": "", "approve": "http://new"})
    assert merged["checkout"] == "http://explicit"
    assert merged["provider"] == "http://configured-p", "an empty override must not blank a configured stage"
    assert merged["approve"] == "http://new"
    assert base["checkout"] == "http://configured", "the original value must stay untouched"


def test_the_value_is_frozen_and_hashable():
    proxies = StageProxies.from_mapping({"checkout": "http://c"})
    with pytest.raises(Exception):
        proxies.checkout = "http://mutated"  # type: ignore[misc]
    assert hash(proxies) == hash(StageProxies.from_mapping({"checkout": "http://c"}))
    assert proxies == StageProxies.from_mapping({"checkout": "http://c"})


def test_the_resolver_returns_the_dataclass():
    """``_proxies_from_config`` is typed, not a bare dict."""
    from sms_tool.paypal.proxy import _proxies_from_config

    proxies = _proxies_from_config(
        {"paypal": {"stage_proxies": {"checkout": "http://c", "provider": "http://p"}}, "proxy": {}},
        checkout_country="US",
        target_country="US",
    )
    assert isinstance(proxies, StageProxies)
    assert proxies["checkout"] == "http://c"
    assert proxies["provider"] == "http://p"
