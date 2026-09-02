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
    planner = (ROOT / "SmsWorkbench.Contracts" / "BackendCommandPlanner.cs").read_text(encoding="utf-8-sig")

    # The two WPF entry points this test used to pin (MainWindow.Register.cs ->
    # RegisterFromPool_Click, MainWindow.Tasks.cs -> RerunFailed_Click) were
    # dead event handlers with no XAML subscriber and were removed on
    # 2026-09-02 (round 6). The invariant worth keeping is the planner side:
    # pool registration and failed-rerun both route through no-phone-reuse.
    pool_reg_start = planner.index("public static BackendCommandPlan CreatePoolRegistration")
    pool_reg_end = planner.index("public static BackendCommandPlan CreateMailboxFileRegistration", pool_reg_start)
    assert "AppendNoPhoneReuse(args);" in planner[pool_reg_start:pool_reg_end]

    # Not AppendNoPhoneReuse: the rerun plan expresses "do not reduce this run
    # to phone-only" through the registrationAtOnly flag instead.
    rerun_start = planner.index("public static BackendCommandPlan CreateRerunFailedRegistration")
    rerun_end = planner.index("public static BackendCommandPlan Create", rerun_start + 40)
    rerun_block = planner[rerun_start:rerun_end]
    assert "registrationAtOnly: false" in rerun_block

    # Phone registration uses --phone-register and must NOT disable phone reuse.
    phone_start = planner.index("public static BackendCommandPlan CreatePhoneRegistration")
    phone_end = planner.index("public static BackendCommandPlan CreateCfWorkerRegistration", phone_start)
    phone_block = planner[phone_start:phone_end]
    assert '"--phone-register"' in phone_block
    assert "AppendNoPhoneReuse" not in phone_block


def test_registered_remail_rows_can_build_one_click_sms_mailbox_files():
    # Mailbox-line classification now lives in BackendCommandPlanner -- the single
    # home for CLI argument construction. MainWindow.Register.cs used to carry a
    # private copy of it (MailboxArgForLine) that had drifted in form and had no
    # tests; it was removed on 2026-09-02 (round 6) and callers now delegate.
    planner = (ROOT / "SmsWorkbench.Contracts" / "BackendCommandPlanner.cs").read_text(encoding="utf-8-sig")
    source = (ROOT / "SmsWorkbench" / "MainWindow.Register.cs").read_text(encoding="utf-8-sig")

    # remail:// lines are still passed to the backend as mailbox files.
    assert 'value.StartsWith("remail://"' in planner
    # No private duplicate left behind in the WPF host.
    assert "private string MailboxArgForLine" not in source
    assert "BackendCommandPlanner.MailboxArgumentForLine" in source
    # Registered rows resolve mailbox credentials through the backend read
    # (desktop_read "mailbox-file"), which owns the canonical remail:// line
    # format; the behavioral coverage lives in tests/test_desktop_read.py.
    assert "FindMailboxLineFromBackend" in source
    assert "ReadMailboxLineAsync" in source


def test_icloud_registration_and_rerun_use_format_aware_mailbox_arguments():
    register_source = (ROOT / "SmsWorkbench" / "MainWindow.Register.cs").read_text(encoding="utf-8-sig")
    planner = (ROOT / "SmsWorkbench.Contracts" / "BackendCommandPlanner.cs").read_text(encoding="utf-8-sig")

    # Matched on the method name only, not on `private bool ...`: these helpers
    # became async (2026-09-02) because the backend read they depend on used to
    # block the UI thread for up to 120s. The point of this test is the
    # dispatch shape, not the signature.
    # Anchor on the *definition*, not the call site: the name also appears in
    # OneClickRegister_Click, and slicing from there picked up the wrong block.
    start = register_source.index("TryCreateSelectedUnregisteredMailboxFileAsync(CancellationToken")
    # `(PoolRow` identifies the definition; the bare name also appears as a call
    # inside the block above, which would cut the slice short.
    end = register_source.index("IsUnregisteredMailboxRowAsync(PoolRow", start)
    selected_block = register_source[start:end]
    assert "TryCreateMailboxFileAsync(pending, ct)" in selected_block
    assert 'mailboxArg = "--chatai-mailbox-file"' not in selected_block
    assert '= "--chatai-mailbox-file"' not in selected_block

    # RerunFailed_Click was a dead event handler (no XAML subscriber) and was
    # removed with the rest on 2026-09-02 (round 6). What survives is the
    # planner contract: the rerun plan takes the *resolved* mailbox argument and
    # file rather than hardcoding a chatai mailbox file argument inline.
    rerun_start = planner.index("public static BackendCommandPlan CreateRerunFailedRegistration")
    rerun_end = planner.index("public static BackendCommandPlan Create", rerun_start + 40)
    rerun_block = planner[rerun_start:rerun_end]
    assert "--chatai-mailbox-file" not in rerun_block
    assert "mailboxArg" in rerun_block
