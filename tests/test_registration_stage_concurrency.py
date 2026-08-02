from unittest.mock import patch

from sms_tool import registration_concurrency


def test_stage_groups_are_owned_by_concurrency_module():
    assert registration_concurrency.registration_stage_group("auth_flow") == "network"
    assert registration_concurrency.registration_stage_group("access_token_probe") == "at_probe"
    assert registration_concurrency.registration_stage_group("payment_link") == "payment"
    assert registration_concurrency.registration_stage_group("completed") == ""


def test_stage_transitions_release_previous_group_and_record_metrics():
    registration_concurrency.release_registration_stage()
    registration_concurrency.registration_stage_metrics(reset=True)
    with patch.object(
        registration_concurrency,
        "CFG",
        {"registration": {"stage_concurrency": {"network": 1, "at_probe": 1}}},
    ):
        try:
            registration_concurrency.enter_registration_stage("auth_flow")
            registration_concurrency.enter_registration_stage("email_otp_send")
            registration_concurrency.enter_registration_stage("access_token_probe")
        finally:
            registration_concurrency.release_registration_stage()

    metrics = registration_concurrency.registration_stage_metrics(reset=True)
    assert metrics["network"]["transitions"] == 1
    assert metrics["at_probe"]["transitions"] == 1
