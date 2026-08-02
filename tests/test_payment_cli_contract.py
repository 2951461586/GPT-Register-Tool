from types import SimpleNamespace

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
