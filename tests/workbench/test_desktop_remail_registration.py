from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]

#: The one-click options dialog is a XAML window (`RegisterOptionsDialog.xaml`),
#: while source-to-plan dispatch and mailbox-line resolution live in
#: `RegistrationWorkflowCoordinator.cs` behind `MainWindow.Register.cs`.
REGISTER_DIALOG_XAML = ROOT / "SmsWorkbench" / "Dialogs" / "RegisterOptionsDialog.xaml"
REGISTER_PARTIAL = ROOT / "SmsWorkbench" / "Views" / "MainWindow.Register.cs"
REGISTER_WORKFLOW = ROOT / "SmsWorkbench" / "Services" / "RegistrationWorkflowCoordinator.cs"
REGISTER_BUILDER = ROOT / "SmsWorkbench" / "Services" / "RegistrationRequestBuilder.cs"


def test_one_click_registration_uses_requested_provider_labels_and_defaults_to_remail():
    source = REGISTER_DIALOG_XAML.read_text(encoding="utf-8-sig")

    expected = [
        '<ComboBoxItem Content="{loc:Tr Key=\'ReMail 邮箱\'}" Tag="remail_target"/>',
        '<ComboBoxItem Content="{loc:Tr Key=\'Smailr 邮箱\'}" Tag="smailr"/>',
        '<ComboBoxItem Content="{loc:Tr Key=\'Outlook/Hotmail/iCloud 邮箱池\'}" Tag="pool"/>',
        '<ComboBoxItem Content="{loc:Tr Key=\'CF Worker 域名邮箱\'}" Tag="cfworker"/>',
        '<ComboBoxItem Content="{loc:Tr Key=\'手机号注册\'}" Tag="phone"/>',
    ]
    positions = [source.index(item) for item in expected]
    assert positions == sorted(positions)
    assert 'SelectedIndex="0"' in source
    assert '"--remail-service-mode", "purchase"' in (
        ROOT / "SmsWorkbench.Contracts" / "BackendCommandPlanner.cs"
    ).read_text(encoding="utf-8-sig")


def test_long_term_remail_disables_phone_reuse_by_default():
    workflow = REGISTER_WORKFLOW.read_text(encoding="utf-8-sig")
    dialog_xaml = REGISTER_DIALOG_XAML.read_text(encoding="utf-8-sig")
    planner = (ROOT / "SmsWorkbench.Contracts" / "BackendCommandPlanner.cs").read_text(encoding="utf-8-sig")

    # One-click ReMail long-term routes to the purchase-mode planner factory.
    start = workflow.index('if (options.Source == "remail_target")')
    end = workflow.index('if (options.Source == "smailr")', start)
    remail_branch = workflow[start:end]
    assert "BackendCommandPlanner.CreateRemailTargetRegistration(" in remail_branch
    assert "checkPromotion: options.CheckPromotion" in remail_branch

    # The promotion checkbox is declared in RegisterOptionsDialog.xaml with IsChecked="True".
    assert "Content=\"{loc:Tr Key='注册完成后查询试用优惠'}\"" in dialog_xaml
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

    mailbox_start = planner.index("public static BackendCommandPlan CreateMailboxFileRegistration")
    mailbox_end = planner.index("public static BackendCommandPlan Create", mailbox_start + 40)
    assert "AppendNoPhoneReuse(args);" in planner[mailbox_start:mailbox_end]

    # Phone registration uses --phone-register and must NOT disable phone reuse.
    phone_start = planner.index("public static BackendCommandPlan CreatePhoneRegistration")
    phone_end = planner.index("public static BackendCommandPlan CreateCfWorkerRegistration", phone_start)
    phone_block = planner[phone_start:phone_end]
    assert '"--phone-register"' in phone_block
    assert "AppendNoPhoneReuse" not in phone_block


def test_registered_remail_rows_can_build_one_click_sms_mailbox_files():
    planner = (ROOT / "SmsWorkbench.Contracts" / "BackendCommandPlanner.cs").read_text(encoding="utf-8-sig")
    partial_source = REGISTER_PARTIAL.read_text(encoding="utf-8-sig")
    workflow_source = REGISTER_WORKFLOW.read_text(encoding="utf-8-sig")
    combined = partial_source + "\n" + workflow_source

    # remail:// lines are still passed to the backend as mailbox files.
    assert 'value.StartsWith("remail://"' in planner
    # No private duplicate left behind in the WPF host.
    assert "private string MailboxArgForLine" not in combined
    assert "BackendCommandPlanner.MailboxArgumentForLine" in combined
    # Registered rows resolve mailbox credentials through the backend read
    # (desktop_read "mailbox-file"), which owns the canonical remail:// line
    # format; the behavioral coverage lives in tests/workbench/test_desktop_read.py.
    assert "FindMailboxLineFromBackend" in workflow_source
    assert "ReadMailboxLineAsync" in workflow_source


def test_icloud_registration_and_rerun_use_format_aware_mailbox_arguments():
    register_source = REGISTER_BUILDER.read_text(encoding="utf-8-sig")
    planner = (ROOT / "SmsWorkbench.Contracts" / "BackendCommandPlanner.cs").read_text(encoding="utf-8-sig")

    start = register_source.index("public async Task<PendingMailboxSelection> CreateUnregisteredBatchFileAsync")
    end = register_source.index("private async Task<bool> IsUnregisteredMailboxRowAsync", start)
    selected_block = register_source[start:end]
    assert "CreateBatchFileAsync(pending, ct)" in selected_block
    assert 'mailboxArg = "--chatai-mailbox-file"' not in selected_block
    assert '= "--chatai-mailbox-file"' not in selected_block

    assert "mailboxArgument" in planner
    assert "--chatai-mailbox-file" not in (
        ROOT / "SmsWorkbench" / "Services" / "RegistrationRequestBuilder.cs"
    ).read_text(encoding="utf-8-sig")
