from unittest.mock import patch

from sms_tool import registration_concurrency


def test_stage_groups_are_owned_by_concurrency_module():
    assert registration_concurrency.registration_stage_group("auth_flow") == "auth"
    assert registration_concurrency.registration_stage_group("access_token_probe") == "at_probe"
    assert registration_concurrency.registration_stage_group("payment_link") == "payment"
    assert registration_concurrency.registration_stage_group("completed") == ""


def test_stage_transitions_release_previous_group_and_record_metrics():
    registration_concurrency.release_registration_stage()
    registration_concurrency.registration_stage_metrics(reset=True)
    with patch.object(
        registration_concurrency,
        "CFG",
        {"registration": {"stage_concurrency": {"auth": 1, "network": 1, "at_probe": 1}}},
    ):
        try:
            registration_concurrency.enter_registration_stage("auth_flow")
            registration_concurrency.enter_registration_stage("email_otp_send")
            registration_concurrency.enter_registration_stage("access_token_probe")
        finally:
            registration_concurrency.release_registration_stage()

    metrics = registration_concurrency.registration_stage_metrics(reset=True)
    assert metrics["auth"]["transitions"] == 1
    assert metrics["network"]["transitions"] == 1
    assert metrics["at_probe"]["transitions"] == 1


def test_rate_limit_circuit_blocks_new_auth_admission():
    registration_concurrency.release_registration_stage()
    registration_concurrency.clear_registration_rate_limit()
    try:
        registration_concurrency.mark_registration_rate_limited(60)
        with patch.object(
            registration_concurrency,
            "CFG",
            {"registration": {"stage_concurrency": {"auth": 1, "cross_process": False}}},
        ):
            try:
                registration_concurrency.enter_registration_stage("auth_flow")
            except RuntimeError as exc:
                assert "registration_rate_limit_circuit_open" in str(exc)
            else:
                raise AssertionError("rate-limit circuit did not block auth admission")
    finally:
        registration_concurrency.clear_registration_rate_limit()
