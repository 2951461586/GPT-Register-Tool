from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from sms_tool import cli, registration


ROOT = Path(__file__).resolve().parents[1]


def test_registration_payment_accepts_link_qr_and_completed_payment_artifacts():
    assert cli._payment_result_has_artifact({"ok": True, "url": "https://example.test/pay"})
    assert cli._payment_result_has_artifact({"ok": True, "qr_path": "qr.png"})
    assert cli._payment_result_has_artifact({"ok": True, "qr_data": "upi://pay"})
    assert cli._payment_result_has_artifact({
        "ok": True,
        "operation": "execute_payment",
        "status": "completed",
    })
    assert not cli._payment_result_has_artifact({"ok": True})
    assert not cli._payment_result_has_artifact({"ok": False, "url": "https://example.test/pay"})


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


def test_registration_rejects_blik_before_loading_or_buying_mailboxes():
    cfg = {
        "output": {"directory": "sessions"},
        "proxy": {},
        "paypal": {"auto_generate": True},
        "blik": {"auto_generate": True},
    }
    argv = ["chatgpt_phone_reg.py", "--email", "user@example.com", "--payment-method", "blik"]
    with patch.object(cli, "CFG", cfg), \
         patch("sys.argv", argv), \
         patch.object(cli, "_load_mailbox_pool") as load_mailboxes, \
         pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
    load_mailboxes.assert_not_called()


def test_registration_and_batch_selector_excludes_blik():
    helper_source = (ROOT / "SmsWorkbench" / "MainWindow.Helpers.cs").read_text(encoding="utf-8-sig")
    start = helper_source.index("private void AddPaymentMethodItems")
    end = helper_source.index("private int CountValue", start)
    assert 'Tag = "blik"' not in helper_source[start:end]

    single_payment_source = (ROOT / "SmsWorkbench" / "MainWindow.Payment.cs").read_text(encoding="utf-8-sig")
    assert 'Tag = "blik|PL"' in single_payment_source
    assert 'args.AddRange(new[] { "--blik-code"' in single_payment_source
