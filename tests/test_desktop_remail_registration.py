from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_one_click_registration_uses_requested_provider_labels_and_defaults_to_remail():
    source = (ROOT / "SmsWorkbench" / "MainWindow.Register.cs").read_text(encoding="utf-8-sig")

    expected = [
        'Content = "ReMail 邮箱", Tag = "remail_target"',
        'Content = "Smailr 邮箱", Tag = "smailr"',
        'Content = "Outlook/Hotmail/iCloud 邮箱池", Tag = "pool"',
        'Content = "CF Worker 域名邮箱", Tag = "cfworker"',
        'Content = "手机号注册", Tag = "phone"',
    ]
    positions = [source.index(item) for item in expected]
    assert positions == sorted(positions)
    assert "sourceBox.SelectedIndex = 0" in source
    assert '"--remail-service-mode", "purchase"' in (
        ROOT / "SmsWorkbench.Contracts" / "BackendCommandPlanner.cs"
    ).read_text(encoding="utf-8-sig")


def test_long_term_remail_disables_phone_reuse_by_default():
    register = (ROOT / "SmsWorkbench" / "MainWindow.Register.cs").read_text(encoding="utf-8-sig")
    planner = (ROOT / "SmsWorkbench.Contracts" / "BackendCommandPlanner.cs").read_text(encoding="utf-8-sig")

    # One-click ReMail long-term routes to the purchase-mode planner factory.
    start = register.index('if (options.Source == "remail_target")')
    end = register.index('if (options.Source == "smailr")', start)
    remail_branch = register[start:end]
    assert "BackendCommandPlanner.CreateRemailTargetRegistration(" in remail_branch
    assert "checkPromotion: options.CheckPromotion" in remail_branch

    assert 'Content = "注册完成后查询试用优惠"' in register
    assert "--check-promotion-after-registration" in planner

    # The planner keeps ReMail on purchase mode with phone reuse disabled and
    # never forces phone-reuse / phone-source / registration-at-only.
    p_start = planner.index("public static BackendCommandPlan CreateRemailTargetRegistration")
    p_end = planner.index("public static BackendCommandPlan CreateSmailrRegistration", p_start)
    remail_block = planner[p_start:p_end]
    assert '"--remail-service-mode", "purchase"' in remail_block
    assert "AppendNoPhoneReuse(args);" in remail_block
    assert '"--phone-reuse"' not in remail_block
    assert '"--phone-source"' not in remail_block
    assert '"--registration-at-only"' not in remail_block


def test_only_phone_registration_selects_phone_flow():
    register = (ROOT / "SmsWorkbench" / "MainWindow.Register.cs").read_text(encoding="utf-8-sig")
    tasks_source = (ROOT / "SmsWorkbench" / "MainWindow.Tasks.cs").read_text(encoding="utf-8-sig")
    planner = (ROOT / "SmsWorkbench.Contracts" / "BackendCommandPlanner.cs").read_text(encoding="utf-8-sig")

    # Pool registration and failed-rerun both route through no-phone-reuse planners.
    pool_start = register.index("private void RegisterFromPool_Click")
    pool_end = register.index("private void ImportChataiMailbox_Click", pool_start)
    assert "BackendCommandPlanner.CreatePoolRegistration(" in register[pool_start:pool_end]
    assert "BackendCommandPlanner.CreateRerunFailedRegistration(" in tasks_source

    pool_reg_start = planner.index("public static BackendCommandPlan CreatePoolRegistration")
    pool_reg_end = planner.index("public static BackendCommandPlan CreateMailboxFileRegistration", pool_reg_start)
    assert "AppendNoPhoneReuse(args);" in planner[pool_reg_start:pool_reg_end]

    # Phone registration uses --phone-register and must NOT disable phone reuse.
    phone_start = planner.index("public static BackendCommandPlan CreatePhoneRegistration")
    phone_end = planner.index("public static BackendCommandPlan CreateCfWorkerRegistration", phone_start)
    phone_block = planner[phone_start:phone_end]
    assert '"--phone-register"' in phone_block
    assert "AppendNoPhoneReuse" not in phone_block


def test_registered_remail_rows_can_build_one_click_sms_mailbox_files():
    source = (ROOT / "SmsWorkbench" / "MainWindow.Register.cs").read_text(encoding="utf-8-sig")

    # remail:// lines are still passed to the backend as mailbox files.
    assert 'value.StartsWith("remail://"' in source
    # Registered rows resolve mailbox credentials through the backend read
    # (desktop_read "mailbox-file"), which owns the canonical remail:// line
    # format; the behavioral coverage lives in tests/test_desktop_read.py.
    assert "FindMailboxLineFromBackend" in source
    assert "ReadMailboxLineAsync" in source


def test_icloud_registration_and_rerun_use_format_aware_mailbox_arguments():
    register_source = (ROOT / "SmsWorkbench" / "MainWindow.Register.cs").read_text(encoding="utf-8-sig")
    tasks_source = (ROOT / "SmsWorkbench" / "MainWindow.Tasks.cs").read_text(encoding="utf-8-sig")

    start = register_source.index("private bool TryCreateSelectedUnregisteredMailboxFile")
    end = register_source.index("private bool IsUnregisteredMailboxRow", start)
    selected_block = register_source[start:end]
    assert "TryCreateMailboxFile(rows, out mailboxArg, out mailboxFile, out selectedCount)" in selected_block
    assert 'mailboxArg = "--chatai-mailbox-file"' not in selected_block

    assert "TryCreateMailboxFile(failedRows, out string mailboxArg, out string tempFile" in tasks_source
    # The resolved, format-aware mailbox argument is forwarded to the planner
    # rather than hardcoding a chatai mailbox file argument inline.
    assert "CreateRerunFailedRegistration(" in tasks_source
    assert "mailboxArg," in tasks_source
