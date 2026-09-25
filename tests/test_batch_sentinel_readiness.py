"""Offline batch startup gate: no mailbox lookup or proxy preflight on failure."""

from unittest.mock import patch

import pytest

from sms_tool import batch_runner
from sms_tool.sentinel.runner import SentinelRunnerError


def _batch():
    return batch_runner.run_batch_impl(
        count=0,
        mailboxes=[object()],
        registration_driver="protocol",
        run_email_func=lambda **_kwargs: pytest.fail("registration must not run"),
    )


def test_missing_runner_stops_before_account_lookup_and_proxy_preflight(capsys):
    with (
        patch.object(batch_runner, "CFG", {
            "email_registration": {"sentinel_backend": "node_runner", "sentinel_legacy_fallback": False}
        }),
        patch.object(batch_runner, "check_node_runner_readiness",
              side_effect=SentinelRunnerError("sentinel_runner_node_missing")),
        patch.object(batch_runner, "_drop_already_registered") as accounts,
        patch.object(batch_runner, "select_registration_proxy_pool") as proxies,
    ):
        with pytest.raises(SentinelRunnerError, match="^sentinel_runner_node_missing$"):
            _batch()
    accounts.assert_not_called()
    proxies.assert_not_called()
    assert capsys.readouterr().out == ""


def test_enabled_fallback_reports_safe_reason_and_keeps_batch(capsys):
    with (
        patch.object(batch_runner, "CFG", {
            "email_registration": {"sentinel_backend": "node_runner", "sentinel_legacy_fallback": True}
        }),
        patch.object(batch_runner, "check_node_runner_readiness",
              side_effect=SentinelRunnerError("sentinel_runtime_hash_mismatch")),
        patch.object(batch_runner, "_drop_already_registered", return_value=([object()], [], [])),
        patch.object(batch_runner, "select_registration_proxy_pool", return_value=[]),
    ):
        assert _batch() == []
    output = capsys.readouterr().out
    assert "sentinel_runtime_hash_mismatch" in output
    assert "configured legacy fallback remains available" in output


def test_legacy_backend_does_not_check_node(capsys):
    with (
        patch.object(batch_runner, "CFG", {"email_registration": {"sentinel_backend": "legacy"}}),
        patch.object(batch_runner, "check_node_runner_readiness") as readiness,
        patch.object(batch_runner, "_drop_already_registered", return_value=([object()], [], [])),
        patch.object(batch_runner, "select_registration_proxy_pool", return_value=[]),
    ):
        assert _batch() == []
    readiness.assert_not_called()
    assert "readiness failed" not in capsys.readouterr().out


def test_unexpected_readiness_failure_never_discloses_sensitive_path(capsys):
    secret = "password-in-configured-node-path"
    with (
        patch.object(batch_runner, "CFG", {
            "email_registration": {"sentinel_backend": "node_runner", "sentinel_legacy_fallback": True}
        }),
        patch.object(batch_runner, "check_node_runner_readiness",
              side_effect=SentinelRunnerError(secret)),
        patch.object(batch_runner, "_drop_already_registered", return_value=([object()], [], [])),
        patch.object(batch_runner, "select_registration_proxy_pool", return_value=[]),
    ):
        assert _batch() == []
    output = capsys.readouterr().out
    assert "sentinel_runner_readiness_failed" in output
    assert secret not in output
