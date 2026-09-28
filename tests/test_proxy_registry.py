"""Tests for the unified proxy-pool registry (``sms_tool.proxy_registry``).

The registry fronts three resolvers that are deliberately different (Rule 19);
what these tests pin is the *facade* contract:

* canonical ``proxy.lanes`` wins when declared, and is invisible otherwise;
* the legacy path is used unchanged when canonical is absent;
* the census reports every lane, including pools declared inside a payment
  method's own section (which ``payment_routing`` does not know about).
"""

from __future__ import annotations

import pytest

from sms_tool import proxy_registry as pr


def test_endpoints_of_dedupes_and_keeps_non_proxy_entries():
    pool = ["http://u:p@Host.Example:8080", "http://x:y@host.example:8080", "127.0.0.1:7897"]
    assert pr.endpoints_of(pool) == ["127.0.0.1:7897", "host.example:8080"]


def test_resolve_registration_uses_legacy_pool_when_no_canonical():
    config = {"proxy": {"pool": ["http://u:p@h.example:1"]}}
    assert pr.resolve_registration(config) == ["http://u:p@h.example:1"]


def test_resolve_registration_prefers_canonical_lane():
    config = {
        "proxy": {
            "pool": ["http://u:p@legacy.example:1"],
            "lanes": {"protocol_registration": ["http://u:p@canon.example:2"]},
        }
    }
    assert pr.resolve_registration(config) == ["http://u:p@canon.example:2"]


def test_resolve_mailbox_prefers_canonical_lane():
    config = {"proxy": {"lanes": {"mailbox": ["http://u:p@mail.example:3"]}}}
    assert pr.resolve_mailbox(config) == ["http://u:p@mail.example:3"]


def test_resolve_mailbox_canonical_appends_operation_proxy():
    config = {"proxy": {"lanes": {"mailbox": ["http://u:p@mail.example:3"]}}}
    assert pr.resolve_mailbox(config, operation_proxy="http://u:p@op.example:4") == [
        "http://u:p@mail.example:3",
        "http://u:p@op.example:4",
    ]


def test_resolve_payment_legacy_reads_named_regions_and_method_local_pools():
    config = {
        "protocol_payments": {"proxy_pools": {"US": ["http://u:p@us.example:5"]}},
        "upi": {"stage_proxies": {"checkout": "http://u:p@upi.example:6"}},
    }
    resolved = pr.resolve_payment(config, method="upi")
    assert resolved["regions"] == {"US": ["http://u:p@us.example:5"]}
    assert resolved["regions_source"] == "protocol_payments.proxy_pools"
    assert resolved["stages"]["upi.stage_proxies.checkout"] == ["http://u:p@upi.example:6"]


def test_resolve_payment_prefers_canonical_regions():
    config = {
        "protocol_payments": {"proxy_pools": {"US": ["http://u:p@legacy.example:7"]}},
        "proxy": {
            "lanes": {
                "payment": {"pools": {"JP": ["http://u:p@canon.example:8"]}, "default": ["http://u:p@def.example:9"]}
            }
        },
    }
    resolved = pr.resolve_payment(config, method="paypal")
    assert resolved["regions"]["JP"] == ["http://u:p@canon.example:8"]
    assert resolved["regions"]["default"] == ["http://u:p@def.example:9"]
    assert resolved["regions_source"] == "proxy.lanes.payment"


def test_resolve_dispatches_and_rejects_unknown_lane():
    config = {"proxy": {"lanes": {"mailbox": ["http://u:p@mail.example:3"]}}}
    assert pr.resolve(config, "mailbox") == ["http://u:p@mail.example:3"]
    with pytest.raises(ValueError):
        pr.resolve(config, "not-a-lane")


def test_census_shape_and_union():
    config = {
        "proxy": {
            "pool": ["http://u:p@reg.example:1"],
            "lanes": {"mailbox": ["http://u:p@mail.example:2"]},
        },
        "protocol_payments": {"proxy_pools": {"US": ["http://u:p@pay.example:3"]}},
    }
    report = pr.census(config, methods=("paypal",))
    assert report[pr.REGISTRATION]["endpoints"] == ["reg.example:1"]
    assert report[pr.MAILBOX]["endpoints"] == ["mail.example:2"]
    assert "pay.example:3" in report["union_endpoints"]
    assert report["local_endpoints"] == []
    text = pr.format_census(report)
    assert "registration" in text and "mailbox" in text and "payment" in text


def test_census_flags_local_listener():
    config = {"proxy": {"pool": ["127.0.0.1:7897"]}}
    report = pr.census(config, methods=())
    assert report["local_endpoints"] == ["127.0.0.1:7897"]


def test_census_reports_lane_isolation_ok_when_pools_are_disjoint():
    config = {
        "proxy": {"pool": ["http://u:p@reg.example:1"]},
        "protocol_payments": {"proxy_pools": {"US": ["http://u:p@pay.example:2"]}},
    }
    report = pr.census(config, methods=("paypal",))
    assert report["lane_isolation_ok"] is True
    assert report["cross_lane_overlaps"] == {pr.MAILBOX: [], pr.PAYMENT: []}


def test_census_reports_registration_payment_overlap():
    cred = "http://u:p@shared.example:1"
    config = {
        "proxy": {"pool": [cred]},
        "protocol_payments": {"proxy_pools": {"US": [cred]}},
    }
    report = pr.census(config, methods=("paypal",))
    assert report["lane_isolation_ok"] is False
    assert report["cross_lane_overlaps"][pr.PAYMENT] == ["shared.example:1"]
    assert "CROSS-LANE OVERLAP" in pr.format_census(report)


def test_census_reports_registration_mailbox_overlap():
    cred = "http://u:p@shared.example:1"
    config = {"proxy": {"pool": [cred], "lanes": {"mailbox": [cred]}}}
    report = pr.census(config, methods=())
    assert report["lane_isolation_ok"] is False
    assert report["cross_lane_overlaps"][pr.MAILBOX] == ["shared.example:1"]
