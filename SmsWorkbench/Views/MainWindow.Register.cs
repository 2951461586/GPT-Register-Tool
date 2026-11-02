// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch used to stay `annotations`.
#nullable enable

using SmsWorkbench.Localization;

namespace SmsWorkbench
{
    public partial class MainWindow
    {
        // Registration, SMS and scan actions.
        //
        // This partial only wires things together: CLI argument construction is
        // delegated to BackendCommandPlanner, mailbox batch-file assembly to
        // RegistrationRequestBuilder and the three option dialogs to
        // RegisterDialogService. The mailbox-line lookup stays here because the
        // inbox and export partials use it too.
        //
        // RegisterFromPool_Click removed (2026-09-02, round 6): dead event
        // handler, no XAML subscriber. BackendCommandPlanner.CreatePoolRegistration
        // stays -- it is unit-tested on the Contracts side.

        // `registerDialogs` is injected; `registrationRequests` is built in the
        // constructor from this window's mailbox-line lookup and CFWorker predicate.
        private readonly IRegisterDialogService registerDialogs;
        private readonly RegistrationRequestBuilder registrationRequests;

        private void ImportChataiMailbox()
        {
            var dialog = new Microsoft.Win32.OpenFileDialog
            {
                Filter = "文本文件 (*.txt)|*.txt|所有文件 (*.*)|*.*",
                Title = Loc.T("选择邮箱文件")
            };
            if (dialog.ShowDialog() != true) return;

            string path = dialog.FileName;
            string[] lines;
            try
            {
                lines = File.ReadAllLines(path, Encoding.UTF8);
            }
            catch (Exception ex)
            {
                MessageBox.Show(Loc.T("读取文件失败：") + ex.Message, Loc.T("错误"), MessageBoxButton.OK, MessageBoxImage.Error);
                return;
            }

            string targetFile = GetMailboxTokenFile();
            (int imported, int skipped) = MailboxPoolFileStore.ImportSupportedLines(targetFile, lines);
            Register.ChataiMailboxFilePath = targetFile;
            RefreshPools();
            NotifySuccess(Loc.F("导入完成：成功 {0} 条，跳过 {1} 条。", imported, skipped));
        }

        private async void ViewInbox()
        {
            PoolRow? row = SelectedEmailRowOrNotify("查看收件箱");
            if (row == null) return;
            string mailboxLine = await FindMailboxLineForRowAsync(row).ConfigureAwait(true);
            if (string.IsNullOrWhiteSpace(mailboxLine) || BackendCommandPlanner.MailboxArgumentForLine(mailboxLine).Length == 0)
            {
                MessageBox.Show(Loc.T("选中记录缺少可用的邮箱凭据或导入行。"), Loc.T("格式不匹配"), MessageBoxButton.OK, MessageBoxImage.Information);
                return;
            }
            ShowInboxDialog(row);
        }

        private async void OneClickRegister()
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
            // The dialog used to write this back in its OK handler; remembering the
            // accepted count after the fact is the same behaviour with the write in
            // one place.
            Register.CountText = options.Count.ToString();

            if (options.Source == "phone")
            {
                var plan = BackendCommandPlanner.CreatePhoneRegistration(
                    options.Count,
                    GetRegistrationProxyPool(),
                    disable2fa: options.Disable2fa,
                    checkPromotion: options.CheckPromotion);
                RunBackend(plan);
                return;
            }

            if (options.Source == "cfworker")
            {
                var plan = BackendCommandPlanner.CreateCfWorkerRegistration(
                    GetConfiguredCfWorkerDomain(),
                    options.Count,
                    options.Workers,
                    GetRegistrationProxyPool(),
                    disable2fa: options.Disable2fa,
                    checkPromotion: options.CheckPromotion);
                RunBackend(plan);
                return;
            }

            if (options.Source == "remail_target")
            {
                var plan = BackendCommandPlanner.CreateRemailTargetRegistration(
                    options.Count,
                    options.Workers,
                    GetRegistrationProxyPool(),
                    disable2fa: options.Disable2fa,
                    checkPromotion: options.CheckPromotion);
                RunBackend(plan);
                return;
            }

            if (options.Source == "smailr")
            {
                var plan = BackendCommandPlanner.CreateSmailrRegistration(
                    GetConfiguredSmailrDomain(),
                    options.Count,
                    options.Workers,
                    GetRegistrationProxyPool(),
                    disable2fa: options.Disable2fa,
                    checkPromotion: options.CheckPromotion);
                RunBackend(plan);
                return;
            }

            if (options.Source != "pool")
            {
                // Every combo tag in CreateRegisterOptionsDialog must have a branch
                // above. Falling through to the mailbox-file path used to mean a new
                // tag silently ran a different source; say so instead.
                Log("未知注册方式：" + SensitiveDataSanitizer.Redact(options.Source));
                ShowThemedInfoDialog(
                    Loc.T("不支持的注册方式"),
                    Loc.F("注册方式“{0}”没有对应的执行路径。", options.Source));
                return;
            }

            // "pool": the imported mailbox file (Outlook/Hotmail/iCloud 邮箱池).
            // This is the operator's persistent pool, so it is deliberately NOT
            // declared as a temporary file.
            string mailboxFile = GetChataiMailboxFilePath();
            if (string.IsNullOrWhiteSpace(mailboxFile) || !File.Exists(mailboxFile))
            {
                ShowThemedInfoDialog(Loc.T("缺少邮箱文件"), Loc.T("未选择邮箱，且未找到 Chatai 邮箱文件。请先导入邮箱，或勾选要注册的邮箱记录。"));
                return;
            }
            var defaultPlan = BackendCommandPlanner.CreateMailboxFileRegistration(
                Loc.T("一键注册"),
                BackendCommandPlanner.ChataiMailboxFileFlag,
                mailboxFile,
                options.Count,
                options.Workers,
                registrationAtOnly: true,
                GetRegistrationProxyPool(),
                disable2fa: options.Disable2fa,
                checkPromotion: options.CheckPromotion);
            RunBackend(defaultPlan);
        }

        // AddRegistrationAtOnlyArgs removed (2026-09-02, round 6): no caller.
        // AddNoPhoneRegistrationArgs removed (2026-10-18): no caller; the flag
        // literal now lives only in BackendCommandPlanner.AppendNoPhoneReuse.

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
            // RunBackend(plan) hands plan.TempFiles to the runner, which deletes the
            // selected-mailbox batch file in its finally. The string-only overload
            // cannot see those paths, and that is what left mailbox credentials in
            // %TEMP% after every one-click batch.
            RunBackend(plan);
        }

        private void OneClickScan()
        {
            var rows = SelectedRowsOrCurrent()
                .Where(r => !string.IsNullOrWhiteSpace(r.Identifier))
                .ToList();
            if (rows.Count == 0)
            {
                rows = Pool.AllRows
                    .Where(Pool.Matches)
                    .Where(r => !string.IsNullOrWhiteSpace(r.Identifier))
                    .ToList();
            }
            rows = rows
                .GroupBy(r => r.Identifier.Trim().ToLowerInvariant())
                .Select(g => g.First())
                .ToList();
            if (rows.Count == 0)
            {
                ShowThemedInfoDialog(Loc.T("账号测活"), Loc.T("没有找到可测活的账号。请先勾选账号，或切换到包含账号的筛选范围。"));
                return;
            }

            ScanOptions? options = registerDialogs.ShowScanOptions(this, rows.Count);
            if (options == null) return;

            var plan = BackendCommandPlanner.CreateAccountScan(
                rows.Select(r => r.Identifier.Trim()).ToList(),
                rows.Count == 1 ? SessionFileFor(rows[0]) : "",
                options.Workers,
                options.AutoRelogin,
                GetLivenessProxyPool());
            RunAccountBatchBackend(plan, "account_scan", rows.Count);
        }

        private void CheckPromotion()
        {
            var rows = SelectedRowsOrCurrent()
                .Where(r => r != null && !string.IsNullOrWhiteSpace(r.Identifier))
                .GroupBy(r => r.Identifier.Trim(), StringComparer.OrdinalIgnoreCase)
                .Select(group => group.First())
                .ToList();
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
            var rows = SelectedRowsOrCurrent()
                .Where(r => r != null && !string.IsNullOrWhiteSpace(r.Identifier))
                .GroupBy(r => r.Identifier.Trim(), StringComparer.OrdinalIgnoreCase)
                .Select(group => group.First())
                .ToList();
            if (rows.Count == 0)
            {
                ShowThemedInfoDialog(Loc.T("支付资格检测"), Loc.T("没有找到可检测的账号。请先勾选账号，或切换到包含账号的筛选范围。"));
                return;
            }

            // The method comes from the backend's payment_qualification.method
            // config (default auto: methods for the exit country); leaving it blank here keeps one authority
            // for the default instead of duplicating it in the UI.
            var plan = BackendCommandPlanner.CreatePaymentQualificationCheck(
                rows.Select(r => r.Identifier.Trim()).ToList(),
                Register.DefaultWorkerCount(),
                GetLivenessProxyPool());
            RunAccountBatchBackend(plan, "account_payment_qualification", rows.Count);
        }

        /// <summary>
        /// The one mailbox-line lookup in the window: resident backend first, then
        /// the configured pool files. Shared by the registration request builder,
        /// the inbox dialog and the export path, which is why it did not move.
        /// </summary>
        private async Task<string> FindMailboxLineForRowAsync(PoolRow? row, CancellationToken ct = default)
        {
            // The old body tested `row?.` and then dereferenced `row.` in the
            // same expression - defensive in shape only. An explicit early
            // return says it outright, and the compiler can then prove the rest.
            if (row == null) return "";
            if (!string.IsNullOrWhiteSpace(row.MailboxLine)) return row.MailboxLine.Trim();

            string fromDb = await FindMailboxLineFromBackendAsync(row, ct).ConfigureAwait(true);
            if (fromDb.Length > 0) return fromDb;

            string email = (row.Identifier ?? "").Trim();
            if (email.Length == 0) return "";
            var candidateEmails = new List<string> { email };

            var paths = new List<string> { row.SourcePath, GetChataiMailboxFilePath(), GetMailboxTokenFile() };
            foreach (string path in paths.Where(p => !string.IsNullOrWhiteSpace(p)).Distinct(StringComparer.OrdinalIgnoreCase))
            {
                if (!File.Exists(path) || !path.EndsWith(".txt", StringComparison.OrdinalIgnoreCase)) continue;
                foreach (string raw in File.ReadAllLines(path, Encoding.UTF8))
                {
                    string value = raw.Trim().TrimStart('\ufeff');
                    bool matched = candidateEmails.Any(candidate =>
                        value.StartsWith("gmail://" + candidate, StringComparison.OrdinalIgnoreCase)
                        || value.StartsWith(candidate + "----", StringComparison.OrdinalIgnoreCase)
                        || value.StartsWith(candidate + "---", StringComparison.OrdinalIgnoreCase));
                    if (matched && BackendCommandPlanner.MailboxArgumentForLine(value).Length > 0)
                    {
                        return value;
                    }
                }
            }
            return "";
        }

        private async Task<string> FindMailboxLineFromBackendAsync(PoolRow row, CancellationToken ct = default)
        {
            if (row == null) return "";
            try
            {
                // Was `.GetAwaiter().GetResult()` on the UI thread. That did not
                // deadlock (DesktopReadClient configures ConfigureAwait(false)),
                // but the resident-channel timeout is 120s, so a stalled backend
                // froze the whole window - close button included - for two
                // minutes. This was the only sync-over-async path in shipped
                // code. ConfigureAwait(true) because the caller resumes into UI
                // work; it is the default, stated explicitly so nobody "optimises"
                // it away later.
                string line = await desktopRead
                    .ReadMailboxLineAsync(OnlyDigits(row.RawLine), row.Identifier, ct)
                    .ConfigureAwait(true);
                return (line ?? "").Trim();
            }
            catch (Exception ex)
            {
                Log("读取邮箱 backend 失败：" + SensitiveDataSanitizer.Redact(ex.Message));
            }
            return "";
        }
    }
}
