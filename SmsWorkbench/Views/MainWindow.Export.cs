// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch used to stay `annotations`.
#nullable enable

using SmsWorkbench.Localization;

namespace SmsWorkbench
{
    public partial class MainWindow
    {
        // Account import/export flow. Dialogs live in ExportDialogService and the
        // "row -> exportable record" rules live in AccountExportAssembler, so this
        // partial only owns the flow: pick a format, collect, write, convert.
        // CLI argument construction is delegated to BackendCommandPlanner.

        // Built in the constructor from window state (the log sink, the root
        // directory, the "open path" action), which is why they are not injected --
        // RegisterDialogService, which needs nothing from the window, is.
        private readonly IExportDialogService exportDialogs;
        private readonly AccountExportAssembler accountExport;

        private void ImportPaidCpa()
        {
            string target = exportDialogs.ShowImportTarget(this, Loc.T("一键导入"));
            if (target.Length == 0) return;

            var rows = Export.ImportableRows(SelectedRowsOrCurrent());

            if (rows.Count == 0)
            {
                MessageBox.Show(Loc.T("没有找到可导入账号。请先注册账号并获得 access_token/session。"), Loc.T("一键导入"), MessageBoxButton.OK, MessageBoxImage.Information);
                return;
            }

            var plan = BackendCommandPlanner.CreateAccountImport(
                target,
                rows.Select(r => r.Identifier.Trim()).ToList());
            // CreateAccountImport writes the email list to a temp file and declares it;
            // RunBackend(plan) is what actually deletes it afterwards.
            RunBackend(plan);
        }

        private async void ExportAccounts()
        {
            string format = exportDialogs.ShowExportFormat(this);
            if (format.Length == 0) return;

            var rows = Export.CandidateRows(SelectedRowsOrCurrent());
            if (format.Equals("txt", StringComparison.OrdinalIgnoreCase))
            {
                await ExportAccountsTxtAsync(rows).ConfigureAwait(true);
                return;
            }
            if (format.Equals("json", StringComparison.OrdinalIgnoreCase))
            {
                ExportAccountsJson(rows);
                return;
            }
            ExportAccountsConvertedJson(rows, format);
        }

        private async Task ExportAccountsTxtAsync(List<PoolRow> rows)
        {
            var lines = new List<string>();
            var seen = new HashSet<string>(StringComparer.Ordinal);
            int skipped = 0;
            foreach (PoolRow row in rows)
            {
                string? line = await accountExport.BuildExportLineAsync(row).ConfigureAwait(true);
                if (line is null)
                {
                    skipped++;
                }
                else if (seen.Add(line))
                {
                    lines.Add(line);
                }
            }

            if (lines.Count == 0)
            {
                ShowThemedInfoDialog(Loc.T("一键导出"), Loc.T("没有找到可导出的账号记录。仅支持包含邮箱、密码、客户端ID、刷新令牌的邮箱记录；CFWorker 或缺少密码/刷新令牌的记录会被跳过。"));
                return;
            }

            string outputDir = Path.Combine(rootDir, "runtime");
            Directory.CreateDirectory(outputDir);
            string outputPath = Path.Combine(outputDir, "account-" + DateTime.Now.ToString("yyyyMMdd_HHmmss") + ".txt");
            File.WriteAllLines(outputPath, lines, new UTF8Encoding(false));
            Log("One-click export wrote " + lines.Count + " account(s), skipped " + skipped + ": " + outputPath);
            exportDialogs.ShowExportComplete(this, outputPath, lines.Count, skipped, Loc.T("TXT"), Loc.T("账号----密码----客户端ID----刷新令牌"));
        }

        private void ExportAccountsJson(List<PoolRow> rows)
            => RunUiTask(() => ExportAccountsJsonAsync(rows, _lifetimeCts.Token));

        private async Task ExportAccountsJsonAsync(List<PoolRow> rows, CancellationToken ct = default)
        {
            var collected = await accountExport.CollectJsonAsync(rows, ct).ConfigureAwait(true);
            if (collected.Items.Count == 0)
            {
                ShowThemedInfoDialog(Loc.T("一键导出"), Loc.T("没有找到可导出的 JSON 账号记录。需要账号已生成 session/auth_session 或 SQLite 原始记录。"));
                return;
            }

            string outputDir = Path.Combine(rootDir, "runtime", "account_json");
            Directory.CreateDirectory(outputDir);
            string outputPath = Path.Combine(outputDir, "account-" + DateTime.Now.ToString("yyyyMMdd_HHmmss") + ".json");
            object payload = collected.Items.Count == 1 ? collected.Items[0] : collected.Items;
            var options = new JsonSerializerOptions { WriteIndented = true };
            await Task.Run(() => File.WriteAllText(outputPath, JsonSerializer.Serialize(payload, options), new UTF8Encoding(false)), ct);
            Log("One-click JSON export wrote " + collected.Items.Count + " account(s), skipped " + collected.Skipped + ": " + outputPath);
            exportDialogs.ShowExportComplete(this, outputPath, collected.Items.Count, collected.Skipped, Loc.T("JSON"), Loc.T("原始账号 session JSON；保留 RT 字段，未获取 RT 的账号默认留空"));
        }

        private void ExportAccountsConvertedJson(List<PoolRow> rows, string format)
            => RunUiTask(() => ExportAccountsConvertedJsonAsync(rows, format, _lifetimeCts.Token));

        private async Task ExportAccountsConvertedJsonAsync(List<PoolRow> rows, string format, CancellationToken ct = default)
        {
            var collected = await accountExport.CollectJsonAsync(rows, ct).ConfigureAwait(true);
            if (collected.Items.Count == 0)
            {
                ShowThemedInfoDialog(Loc.T("一键导出"), Loc.T("没有找到可转换的账号 session。需要账号已生成 access_token/session/auth_session 或 SQLite 原始记录。"));
                return;
            }
            IReadOnlyList<Dictionary<string, object>> items = collected.Items;
            int skipped = collected.Skipped;

            string normalized = (format ?? "cpa").Trim().ToLowerInvariant();
            string outputDir = Path.Combine(rootDir, "runtime", "account_json");
            Directory.CreateDirectory(outputDir);
            string stamp = DateTime.Now.ToString("yyyyMMdd_HHmmss");
            string sourcePath = Path.Combine(Path.GetTempPath(), "account_export_source_" + stamp + ".json");
            string outputPath = Path.Combine(outputDir, "account-" + normalized + "-" + stamp + ".json");
            object payload = items.Count == 1 ? items[0] : items;
            var options = new JsonSerializerOptions { WriteIndented = true };
            await Task.Run(() => File.WriteAllText(sourcePath, JsonSerializer.Serialize(payload, options), new UTF8Encoding(false)), ct);

            try
            {
                var plan = BackendCommandPlanner.CreateSessionConversion(sourcePath, normalized, outputPath);
                await RunBackendWithResultAsync(plan.TaskName, plan.Arguments.ToList(), ct: ct);
            }
            catch (Exception ex)
            {
                Log("账号格式转换失败：" + ex.Message);
                ShowThemedInfoDialog(Loc.T("一键导出"), Loc.T("账号格式转换失败：") + ex.Message);
                return;
            }
            finally
            {
                try { if (File.Exists(sourcePath)) File.Delete(sourcePath); } catch { }
            }

            if (!File.Exists(outputPath) || new FileInfo(outputPath).Length == 0)
            {
                ShowThemedInfoDialog(Loc.T("一键导出"), Loc.T("账号格式转换没有生成输出文件，请查看下方日志确认 converter 结果。"));
                return;
            }

            Log("One-click converted export wrote " + items.Count + " account(s), skipped " + skipped + ", format=" + normalized + ": " + outputPath);
            exportDialogs.ShowExportComplete(this, outputPath, items.Count, skipped, ExportViewModel.FormatLabel(normalized), ExportViewModel.FormatDescription(normalized));
        }

        /// <summary>
        /// Account-batch result panel. The panel itself lives in
        /// <see cref="ExportDialogService"/>; when the backend emitted no summary
        /// there is nothing to show, and that is reported here with the same copy as
        /// before.
        /// </summary>
        private void ShowAccountScanResultDialog(string backendOutput, BackendAccountResultKind resultKind)
        {
            if (exportDialogs.ShowAccountScanResult(this, backendOutput, resultKind)) return;

            string title = BackendResultInterpreter.AccountResultTitle(resultKind);
            ShowThemedInfoDialog(title, Loc.F("{0}已结束，但未解析到结果汇总。请查看下方日志确认详情。", title));
        }

        // TryParseMailboxExportParts / LooksMicrosoftClientId migrated to
        // SmsWorkbench.Contracts.MailboxCredentialLineParser (placement rule 8:
        // parsing stays window-independent and xunit-testable).

        private string DefaultMailboxClientId()
        {
            string configured = settingsService.GetString("email_registration.oauth_client_id").Trim();
            return configured.Length > 0 ? configured : "9e5f94bc-e8a4-4e73-b8be-63364c29d753";
        }
    }
}
