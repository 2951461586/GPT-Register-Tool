from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_one_click_registration_only_exposes_long_term_remail():
    source = (ROOT / "SmsWorkbench" / "MainWindow.Register.cs").read_text(encoding="utf-8-sig")

    assert 'Content = "ReMail 长效邮箱"' in source
    assert "ReMail（短效接码）" not in source
    assert "ReMail（稳定 AT 200 目标）" not in source


def test_long_term_remail_enables_smsbower_instead_of_at_only_mode():
    source = (ROOT / "SmsWorkbench" / "MainWindow.Register.cs").read_text(encoding="utf-8-sig")
    start = source.index('if (options.Source == "remail_target")')
    end = source.index('string mailboxArg = "--chatai-mailbox-file"', start)
    remail_block = source[start:end]

    assert '"--remail-service-mode", "purchase"' in remail_block
    assert '"--phone-reuse", "--phone-source", "smsbower"' in remail_block
    assert '"--registration-at-only"' not in remail_block
    assert '"--no-phone-reuse"' not in remail_block


def test_registered_remail_rows_can_build_one_click_sms_mailbox_files():
    source = (ROOT / "SmsWorkbench" / "MainWindow.Register.cs").read_text(encoding="utf-8-sig")

    assert 'value.StartsWith("remail://"' in source
    assert 'provider.Equals("remail"' in source
    assert "BuildReMailLine(email, serviceToken, orderNo, purchaseId)" in source
