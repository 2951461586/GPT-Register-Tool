using SmsWorkbench.Localization;

namespace SmsWorkbench.Views
{
    public partial class MainWindow
    {
        private readonly IRegisterDialogService registerDialogs;
        private readonly RegistrationRequestBuilder registrationRequests;
        private readonly RegistrationWorkflowCoordinator registrationWorkflow;

        private void ImportChataiMailbox()
        {
            var dialog = new Microsoft.Win32.OpenFileDialog
            {
                Filter = "文本文件 (*.txt)|*.txt|所有文件 (*.*)|*.*",
                Title = Loc.T("选择邮箱文件")
            };
            if (dialog.ShowDialog() != true) return;

            string targetFile = GetMailboxTokenFile();
            (int imported, int skipped) result;
            try
            {
                result = RegistrationWorkflowCoordinator.ImportMailboxFile(dialog.FileName, targetFile);
            }
            catch (Exception ex)
            {
                ShowThemedInfoDialog(Loc.T("错误"), Loc.T("读取文件失败：") + ex.Message);
                return;
            }

            Register.ChataiMailboxFilePath = targetFile;
            RefreshPools();
            NotifySuccess(Loc.F("导入完成：成功 {0} 条，跳过 {1} 条。", result.imported, result.skipped));
        }

        private void ViewInbox() => RunUiTask(ViewInboxAsync);

        private async Task ViewInboxAsync()
        {
            PoolRow? row = SelectedEmailRowOrNotify("查看收件箱");
            if (row == null) return;
            string mailboxLine = await FindMailboxLineForRowAsync(row).ConfigureAwait(true);
            if (string.IsNullOrWhiteSpace(mailboxLine) || BackendCommandPlanner.MailboxArgumentForLine(mailboxLine).Length == 0)
            {
                ShowThemedInfoDialog(Loc.T("格式不匹配"), Loc.T("选中记录缺少可用的邮箱凭据或导入行。"));
                return;
            }
            ShowInboxDialog(row);
        }

        private void OneClickRegister() => RunUiTask(OneClickRegisterAsync);

        private async Task OneClickRegisterAsync()
        {
            PendingMailboxSelection pending = await registrationRequests
                .CreateUnregisteredBatchFileAsync(SelectedRowsOrCurrent().ToList(), _lifetimeCts.Token)
                .ConfigureAwait(true);
            if (pending.Selection is { } pendingSelection)
            {
                RegisterOptions? selectedOptions = registerDialogs.ShowSelectedRegisterOptions(
                    this, pendingSelection.Count, pending.SelectedRowCount, Register.DefaultWorkerCount());
                if (selectedOptions == null) return;
                var plan = BackendCommandPlanner.CreateMailboxFileRegistration(
                    "选中未注册邮箱注册",
                    pendingSelection.Arg,
                    pendingSelection.File,
                    pendingSelection.Count,
                    selectedOptions.Workers,
                    registrationAtOnly: true,
                    GetRegistrationProxyPool(),
                    disable2fa: selectedOptions.Disable2fa,
                    checkPromotion: selectedOptions.CheckPromotion,
                    temporaryFiles: new[] { pendingSelection.File });
                RunBackend(plan);
                return;
            }
            if (pending.PendingRowCount > 0)
            {
                ShowThemedInfoDialog(Loc.T("邮箱记录不完整"), Loc.T("选中的未注册邮箱缺少可用邮箱原始记录，无法直接注册。"));
                return;
            }

            if (SelectedRowsOrCurrent().Any(row => RegistrationStatusPresentation.IsPartial(row.RegistrationStatus)
                || RegistrationStatusPresentation.IsPartial(row.Status)))
            {
                ShowThemedInfoDialog(Loc.T("半注册"), Loc.T("所选邮箱已有服务端账号，不能再次新建注册。请使用已有账号恢复流程。"));
                return;
            }

            MailboxFileSelection? selected = await registrationRequests
                .CreateBatchFileAsync(SelectedRowsOrCurrent(), _lifetimeCts.Token)
                .ConfigureAwait(true);
            if (selected is { } selectedSelection)
            {
                RegisterOptions? selectedOptions = registerDialogs.ShowSelectedRegisterOptions(
                    this, selectedSelection.Count, checkedCount: 0, Register.DefaultWorkerCount());
                if (selectedOptions == null) return;
                var plan = BackendCommandPlanner.CreateMailboxFileRegistration(
                    Loc.T("选中邮箱注册"),
                    selectedSelection.Arg,
                    selectedSelection.File,
                    selectedSelection.Count,
                    selectedOptions.Workers,
                    registrationAtOnly: true,
                    GetRegistrationProxyPool(),
                    disable2fa: selectedOptions.Disable2fa,
                    checkPromotion: selectedOptions.CheckPromotion,
                    temporaryFiles: new[] { selectedSelection.File });
                RunBackend(plan);
                return;
            }

            RegisterOptions? options = registerDialogs.ShowRegisterOptions(
                this, Register.CountValue(), Register.DefaultWorkerCount());
            if (options == null) return;
            Register.CountText = options.Count.ToString();

            BackendCommandPlan? sourcePlan = RegistrationWorkflowCoordinator.TryBuildSourcePlan(
                options,
                GetRegistrationProxyPool(),
                GetConfiguredCfWorkerDomain,
                GetConfiguredSmailrDomain,
                GetChataiMailboxFilePath,
                Log,
                out string? errorTitle,
                out string? errorMessage);
            if (sourcePlan == null)
            {
                ShowThemedInfoDialog(errorTitle ?? Loc.T("一键注册"), errorMessage ?? "");
                return;
            }
            RunBackend(sourcePlan);
        }

        private void OneClickSms()
            => RunUiTask(() => OneClickSmsAsync(_lifetimeCts.Token));

        private async Task OneClickSmsAsync(CancellationToken ct = default)
        {
            var rows = SelectedEmailRowsOrNotify("接码");
            if (rows.Count == 0) return;

            if (!await ShowSmsProviderOneClickDialogAsync(ct))
            {
                return;
            }

            MailboxFileSelection? mailbox =
                await registrationRequests.CreateBatchFileAsync(rows, ct).ConfigureAwait(true);
            if (mailbox is null || mailbox.Count != rows.Count)
            {
                ShowThemedInfoDialog(Loc.T("未选择邮箱"), Loc.T("一键接码需要读取邮箱验证码。请先导入并选择包含完整邮箱凭据的账号。"));
                return;
            }

            var plan = BackendCommandPlanner.CreateOneClickSms(
                mailbox.Arg,
                mailbox.File,
                rows.Select(r => r.Identifier.Trim()).ToList(),
                rows.Count == 1 ? SessionFileFor(rows[0]) : "",
                GetRegistrationProxyPool(),
                SmsProviderCatalog.Resolve(settingsService.GetString("phone_reuse.source")).Key,
                callerTemporaryFiles: new[] { mailbox.File });
            RunBackend(plan);
        }

        private void OneClickScan()
        {
            var rows = RegistrationWorkflowCoordinator.SelectDistinctAccountRows(SelectedRowsOrCurrent(), Pool);
            if (rows.Count == 0)
            {
                ShowThemedInfoDialog(Loc.T("账号测活"), Loc.T("没有找到可测活的账号。请先勾选账号，或切换到包含账号的筛选范围。"));
                return;
            }

            ScanOptions? options = registerDialogs.ShowScanOptions(this, rows.Count);
            if (options == null) return;

            var emails = rows.Select(r => r.Identifier.Trim()).ToList();
            string sessionFile = rows.Count == 1 ? SessionFileFor(rows[0]) : "";
            var proxyPool = GetLivenessProxyPool();
            var plan = options.LightweightInspect
                ? BackendCommandPlanner.CreateAccountInspection(
                    emails, sessionFile, options.Workers, proxyPool, refreshPromotion: options.InspectRefreshPromotion)
                : BackendCommandPlanner.CreateAccountScan(emails, sessionFile, options.Workers, options.AutoRelogin, proxyPool);
            RunAccountBatchBackend(plan, "account_scan", rows.Count);
        }

        private void CheckPromotion()
        {
            var rows = RegistrationWorkflowCoordinator.SelectDistinctAccountRows(SelectedRowsOrCurrent());
            if (rows.Count == 0)
            {
                ShowThemedInfoDialog(Loc.T("账号优惠检测"), Loc.T("没有找到可检测的账号。请先勾选账号，或切换到包含账号的筛选范围。"));
                return;
            }

            var plan = BackendCommandPlanner.CreatePromotionCheck(
                rows.Select(r => r.Identifier.Trim()).ToList(),
                Register.DefaultWorkerCount(),
                GetLivenessProxyPool());
            RunAccountBatchBackend(plan, "account_promotion", rows.Count);
        }

        private void CheckPaymentQualification()
        {
            var rows = RegistrationWorkflowCoordinator.SelectDistinctAccountRows(SelectedRowsOrCurrent());
            if (rows.Count == 0)
            {
                ShowThemedInfoDialog(Loc.T("支付资格检测"), Loc.T("没有找到可检测的账号。请先勾选账号，或切换到包含账号的筛选范围。"));
                return;
            }

            var plan = BackendCommandPlanner.CreatePaymentQualificationCheck(
                rows.Select(r => r.Identifier.Trim()).ToList(),
                Register.DefaultWorkerCount(),
                GetLivenessProxyPool());
            RunAccountBatchBackend(plan, "account_payment_qualification", rows.Count);
        }

        private Task<string> FindMailboxLineForRowAsync(PoolRow? row, CancellationToken ct = default)
            => registrationWorkflow.FindMailboxLineForRowAsync(
                desktopRead,
                row,
                GetChataiMailboxFilePath(),
                GetMailboxTokenFile(),
                OnlyDigits,
                Log,
                ct);
    }
}
