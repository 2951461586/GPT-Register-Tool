from types import SimpleNamespace
from unittest.mock import patch

from sms_tool.batch_runner import RegistrationProxyPool, run_batch_impl


class _NoopGuard:
    def __init__(self, *_args, **_kwargs):
        pass

    def blocked_email_states(self):
        return {}

    def check(self, _email):
        return {"deferred": False, "dead_end": False, "quarantined": False}

    def record(self, *_args, **_kwargs):
        pass


def test_internal_failure_does_not_write_proxy_health():
    mailbox = SimpleNamespace(email="internal@example.com")

    def run_email(**_kwargs):
        return {
            "success": False,
            "email": mailbox.email,
            "error": "registration_internal_error:NameError: get_device_context",
            "failure_class": "internal",
            "retryable": False,
        }

    with patch("sms_tool.batch_runner.CFG", {"email_registration": {}, "registration": {}}), \
         patch("sms_tool.batch_runner.ProxyHealthTracker") as tracker, \
         patch("sms_tool.batch_runner.RegistrationRetryGuard", _NoopGuard), \
         patch("sms_tool.batch_runner.get_account_records", return_value={}), \
         patch("sms_tool.batch_runner.get_registration_checkpoints", return_value={}):
        results = run_batch_impl(
            count=1,
            proxy="http://proxy.invalid:8080",
            mailboxes=[mailbox],
            workers=1,
            max_attempts=1,
            retry_delay_seconds=0,
            run_email_func=run_email,
        )

    assert results[0]["failure_class"] == "internal"
    tracker.assert_not_called()


def test_protocol_result_carries_safe_proxy_audit():
    mailbox = SimpleNamespace(email="protocol@example.com")
    seen = []

    def run_email(**kwargs):
        seen.append(kwargs)
        return {
            "success": False,
            "email": mailbox.email,
            "error": "email_otp_send_stuck",
            "failure_class": "mailbox",
        }

    with patch(
        "sms_tool.batch_runner.CFG",
        {"email_registration": {}, "registration": {"driver": "protocol"}},
    ), patch(
        "sms_tool.batch_runner.select_registration_proxy_pool",
        return_value=RegistrationProxyPool(
            ["http://user:secret@proxy.invalid:8080"],
            actual_countries={"http://user:secret@proxy.invalid:8080": "IN"},
        ),
    ), patch(
        "sms_tool.batch_runner.RegistrationRetryGuard", _NoopGuard,
    ), patch(
        "sms_tool.batch_runner.get_account_records", return_value={},
    ), patch(
        "sms_tool.batch_runner.get_registration_checkpoints", return_value={},
    ):
        results = run_batch_impl(
            count=1,
            proxy_pool=["http://user:secret@proxy.invalid:8080"],
            mailboxes=[mailbox],
            workers=1,
            max_attempts=1,
            run_email_func=run_email,
            registration_driver="protocol",
        )

    assert seen[0]["proxy_metadata"]["pool_index"] == 0
    assert results[0]["proxy_audit"] == {
        "pool_index": 0,
        "expected_country": "",
        "actual_country": "IN",
        "scheme": "http",
        "rotation_generation": 0,
    }
    assert "secret" not in repr(results[0]["proxy_audit"])


def test_blocked_canary_rotates_the_pool_cursor_for_future_accounts():
    mailboxes = [
        SimpleNamespace(email=f"protocol{index}@example.com")
        for index in range(3)
    ]
    audits = []

    def run_email(**kwargs):
        audits.append((kwargs["mailbox"].email, dict(kwargs["proxy_metadata"])))
        if len(audits) == 1:
            return {
                "success": False,
                "email": kwargs["mailbox"].email,
                "error": "email_otp_send_stuck",
                "failure_class": "mailbox",
            }
        return {"success": True, "email": kwargs["mailbox"].email}

    config = {
        "email_registration": {},
        "registration": {
            "driver": "protocol",
            "pulse": {
                "enabled": True,
                "wave_size": 2,
                "wave_delay_seconds": 0,
                "ban_pause_seconds": 0,
                "canary_enabled": True,
            },
        },
    }
    with patch("sms_tool.batch_runner.CFG", config), patch(
        "sms_tool.batch_runner.select_registration_proxy_pool",
        side_effect=lambda pool, _fallback: pool,
    ), patch(
        "sms_tool.batch_runner.RegistrationRetryGuard", _NoopGuard,
    ), patch(
        "sms_tool.batch_runner.get_account_records", return_value={},
    ), patch(
        "sms_tool.batch_runner.get_registration_checkpoints", return_value={},
    ):
        results = run_batch_impl(
            count=3,
            proxy_pool=[
                "http://proxy-a.invalid:8080",
                "http://proxy-b.invalid:8080",
                "http://proxy-c.invalid:8080",
            ],
            mailboxes=mailboxes,
            workers=3,
            max_attempts=1,
            run_email_func=run_email,
            registration_driver="protocol",
        )

    ordered_audits = [audit for _email, audit in sorted(audits)]
    assert [audit["pool_index"] for audit in ordered_audits] == [0, 2, 0]
    assert [audit["rotation_generation"] for audit in ordered_audits] == [0, 1, 1]
    assert results[1]["proxy_rotation_count"] == 1


def test_prewarmed_future_account_uses_rotated_proxy_after_blocked_canary():
    mailboxes = [SimpleNamespace(email=f"warm{index}@example.com") for index in range(3)]
    seen = []
    sequence = iter(range(12))

    def refresh(proxy):
        return f"{proxy}?sid={next(sequence)}"

    def run_email(**kwargs):
        seen.append(kwargs)
        email = kwargs["mailbox"].email
        if email == mailboxes[0].email:
            return {"success": False, "email": email,
                    "error": "email_otp_send_stuck", "failure_class": "mailbox"}
        if email == mailboxes[1].email and kwargs["registration_attempt"] == 1:
            return {"success": False, "email": email,
                    "error": "connection reset", "failure_class": "network"}
        return {"success": True, "email": email}

    config = {
        "email_registration": {"sentinel_backend": "legacy", "sentinel_prewarm_window": 2},
        "registration": {"driver": "protocol", "pulse": {
            "enabled": True, "wave_size": 2, "wave_delay_seconds": 0,
            "ban_pause_seconds": 0, "canary_enabled": True,
        }},
    }
    pool = ["http://proxy-a.invalid:8080", "http://proxy-b.invalid:8080",
            "http://proxy-c.invalid:8080"]
    with patch("sms_tool.batch_runner.CFG", config), \
         patch("sms_tool.batch_runner.select_registration_proxy_pool", side_effect=lambda values, _: values), \
         patch("sms_tool.batch_runner.refresh_proxy_sid", side_effect=refresh), \
         patch("sms_tool.sentinel_tokens._extract_sentinel",
               side_effect=lambda **kwargs: {"prewarmed_for": kwargs["proxy"]}), \
         patch("sms_tool.batch_runner.ProxyHealthTracker"), \
         patch("sms_tool.batch_runner.RegistrationRetryGuard", _NoopGuard), \
         patch("sms_tool.batch_runner.get_account_records", return_value={}), \
         patch("sms_tool.batch_runner.get_registration_checkpoints", return_value={}):
        results = run_batch_impl(
            count=3, proxy_pool=pool, mailboxes=mailboxes, workers=3,
            max_attempts=2, retry_delay_seconds=0,
            run_email_func=run_email, registration_driver="protocol",
        )

    account = [row for row in seen if row["mailbox"].email == mailboxes[1].email]
    assert len(account) == 2
    assert all(row["proxy"].startswith(pool[2]) for row in account)
    assert account[0]["proxy"] != account[1]["proxy"]
    assert all(row["proxy_metadata"]["pool_index"] == 2 for row in account)
    assert all(row["proxy_metadata"]["rotation_generation"] == 1 for row in account)
    assert account[0]["sentinel_data"] is None  # Prewarm was bound to proxy-b.
    assert results[1]["proxy_session_refresh_count"] == 2
