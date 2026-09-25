from sms_tool.registration_funnel import (
    combine_registration_funnels,
    summarize_registration_funnel,
)


def test_funnel_separates_registration_and_successful_promotion_probes():
    results = [
        {"success": True, "email": "private@example.test", "access_token": "secret"},
        {
            "success": False,
            "error": "auth_flow_transport:Bearer secret",
            "failure_class": "network",
            "registration_machine": {
                "history": [{"state": "auth_flow", "detail": "Bearer secret"},
                            {"state": "failed", "detail": "Bearer secret"}],
            },
        },
        {"success": False, "error": "user_already_exists", "failure_class": "account"},
    ]
    report = summarize_registration_funnel(
        results, attempted=4,
        promotion={"total": 1, "success": 1, "trial_eligible": 0, "unauthorized": 0},
    )
    assert report["registered"] == 1
    assert report["registered_per_attempted"] == 0.25
    assert report["unreported"] == 1
    assert report["registration_failures_by_class"] == {"account": 1, "network": 1}
    assert report["registration_failures_by_stage"] == {"auth_flow": 1, "unknown": 1}
    assert report["promotion"]["successful_per_registered"] == 1
    assert report["promotion"]["eligible_per_successful_probe"] == 0
    assert "secret" not in str(report)
    assert "private@example.test" not in str(report)


def test_unchecked_and_failed_probes_never_count_as_no_offer():
    registered = [{"success": True}]
    unchecked = summarize_registration_funnel(registered, attempted=1)
    failed = summarize_registration_funnel(
        registered, attempted=1,
        promotion={"total": 1, "success": 0, "failed": 1, "unauthorized": 1},
    )
    assert unchecked["promotion"]["checked"] is False
    assert unchecked["promotion"]["trial_eligible"] is None
    assert unchecked["promotion"]["successful_per_registered"] is None
    assert unchecked["promotion"]["eligible_per_successful_probe"] is None
    assert failed["promotion"]["checked"] is True
    assert failed["promotion"]["eligible_per_successful_probe"] is None
    assert failed["promotion"]["unauthorized"] == 1


def test_failure_stage_is_allowlisted_even_when_machine_has_unknown_state():
    report = summarize_registration_funnel(
        [{"success": False, "error": "Authorization secret", "failure_class": "secret",
          "registration_machine": {"history": [{"state": "Authorization secret", "detail": "token"}]}}],
        attempted=1,
    )
    assert report["registration_failures_by_stage"] == {"unknown": 1}
    assert report["registration_failures_by_class"] == {"unknown": 1}
    assert "secret" not in str(report)


def test_identity_and_egress_diagnostics_report_only_allowlisted_verdicts():
    report = summarize_registration_funnel(
        [{"success": True, "device_id": "private-device", "auth_fingerprint_profile": "firefox144",
          "identity_context": {"device_id": "private-device", "fingerprint_key": "chrome146"},
          "proxy_audit": {"expected_country": "IN", "actual_country": "US",
                          "proxy": "http://secret@example.test"}}],
        attempted=1,
        promotion={
            "total": 1, "success": 1, "trial_eligible": 0,
            "results": [{"probe": {"proxy_source": "operation_pool",
                                   "error": "Bearer private-access-token"}}],
        },
    )
    assert report["fingerprint_consistency"] == {"mismatch": 1}
    assert report["device_consistency"] == {"matched": 1}
    assert report["egress_country_consistency"] == {"mismatch": 1}
    assert report["promotion"]["probe_proxy_sources"] == {"operation_pool": 1}
    assert "private" not in str(report)
    assert "secret" not in str(report)


def test_rounds_combine_counters_without_reusing_raw_account_rows():
    first = summarize_registration_funnel(
        [{"success": True}, {"success": False, "failure_class": "network"}],
        attempted=2, promotion={"total": 1, "success": 1, "trial_eligible": 1},
    )
    second = summarize_registration_funnel([{"success": True}], attempted=1)
    aggregate = combine_registration_funnels([first, second])
    assert aggregate["attempted"] == 3
    assert aggregate["registered"] == 2
    assert aggregate["registration_failures_by_class"] == {"network": 1}
    assert aggregate["promotion"]["total"] == 1
    assert aggregate["promotion"]["successful_per_registered"] == 0.5
    assert aggregate["promotion"]["eligible_per_successful_probe"] == 1
