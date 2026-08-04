from types import SimpleNamespace
from unittest.mock import patch

import pytest

from sms_tool import account_creation, cli, registration


def test_registration_has_no_payment_generation_entrypoint():
    assert not hasattr(registration, "_pipeline_payment_link")
    assert not hasattr(registration, "_generate_payment_link")
    assert not hasattr(account_creation, "_generate_payment_link")


def test_qr_only_registration_session_is_marked_ready():
    session = registration._build_session_file({
        "email": "qr@example.com",
        "access_token": "at-test",
        "paypal": {"ok": True, "payment_method": "momo", "qr_path": "qr.png"},
    })
    assert session["paypal_status"] == "qr_ready"


def test_blik_batch_requires_the_single_account_command():
    args = SimpleNamespace(
        payment_method="blik",
        email_file="accounts.txt",
        payment_probe_only=False,
    )
    with pytest.raises(SystemExit) as exc:
        cli._extract_payment_link(args)
    assert exc.value.code == 2


def test_single_account_probe_runs_checkout_capability_instead_of_stopping_after_auth():
    args = SimpleNamespace(
        payment_method="gopay",
        email_file="",
        email="probe@example.com",
        session_file="",
        at=None,
        proxy=None,
        proxy_explicit=False,
        refresh_timeout=30,
        no_jit_at_refresh=False,
        payment_probe_only=True,
        desktop_ipc=False,
    )
    auth = {
        "ok": True,
        "access_token": "secret-at",
        "auth_context": {"email": "probe@example.com"},
    }
    capability = {
        "ok": True,
        "operation": "payment_method_capability_probe",
        "classification": "eligible",
        "eligible": True,
    }

    with patch.object(cli, "CFG", {}), \
         patch.object(cli, "_resolve_payment_access_token", return_value=("", None)), \
         patch.object(cli, "_protocol_proxy_pool", return_value=[]), \
         patch("sms_tool.payment_auth.ensure_payment_access_token", return_value=auth), \
         patch("sms_tool.payment_link_manager.generate_payment_link", return_value=capability) as generate:
        cli._extract_payment_link(args)

    assert generate.call_count == 1
    assert generate.call_args.kwargs["access_token"] == "secret-at"
    assert generate.call_args.kwargs["payment_method"] == "gopay"
    assert generate.call_args.kwargs["probe_only"] is True


def test_probe_batch_returns_nonzero_when_capability_is_unknown(tmp_path):
    email_file = tmp_path / "accounts.txt"
    email_file.write_text("probe@example.com\n", encoding="utf-8")
    args = SimpleNamespace(
        payment_method="gopay",
        email_file=str(email_file),
        email=None,
        payment_probe_only=True,
        desktop_ipc=False,
        proxy=None,
        proxy_explicit=False,
    )
    report = {
        "ok": False,
        "counts": {"authenticated": 1, "capability_probed": 1},
        "results": [{
            "classification": "unknown",
            "error_code": "stripe_init_failed",
        }],
    }

    with patch.object(cli, "CFG", {}), \
         patch.object(cli, "_protocol_proxy_pool", return_value=[]), \
         patch("sms_tool.payment_batch.run_payment_batch", return_value=report):
        with pytest.raises(SystemExit) as exc:
            cli._extract_payment_link(args)

    assert exc.value.code == 3


def test_payment_stage_args_preserves_legacy_country_override_hook():
    args = SimpleNamespace(
        proxy="seed-proxy",
        proxy_explicit=True,
        checkout_proxy_country="PH",
        approve_proxy_country="",
    )
    expected = ("legacy", "checkout", "provider", "approve")

    with patch.object(cli, "CFG", {}), patch.object(
        cli, "_apply_stage_country_overrides", return_value=expected
    ) as apply:
        result = cli._at_payment_stage_args(args, "gopay")

    assert result == expected
    apply.assert_called_once()
