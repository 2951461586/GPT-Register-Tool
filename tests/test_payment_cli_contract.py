from pathlib import Path
from types import SimpleNamespace

import pytest

from sms_tool import account_creation, cli, registration


ROOT = Path(__file__).resolve().parents[1]


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


def test_registration_and_batch_selector_excludes_blik():
    helper_source = (ROOT / "SmsWorkbench" / "MainWindow.Helpers.cs").read_text(encoding="utf-8-sig")
    start = helper_source.index("private void AddPaymentMethodItems")
    end = helper_source.index("private int CountValue", start)
    assert 'Tag = "blik"' not in helper_source[start:end]

    single_payment_source = (ROOT / "SmsWorkbench" / "MainWindow.Payment.cs").read_text(encoding="utf-8-sig")
    assert 'Tag = "blik|PL"' in single_payment_source
    assert 'args.AddRange(new[] { "--blik-code"' in single_payment_source
