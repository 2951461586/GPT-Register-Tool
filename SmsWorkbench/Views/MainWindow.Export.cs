using SmsWorkbench.Localization;

namespace SmsWorkbench.Views
{
    public partial class MainWindow
    {
        private readonly IExportDialogService exportDialogs;
        private readonly AccountExportAssembler accountExport;
        private readonly AccountExportCoordinator accountExportCoordinator;

        private void ImportPaidCpa()
        {
            string target = exportDialogs.ShowImportTarget(this, Loc.T("一键导入"));
            if (target.Length == 0) return;

            var rows = Export.ImportableRows(SelectedRowsOrCurrent());
            if (rows.Count == 0)
            {
                ShowThemedInfoDialog(Loc.T("一键导入"), Loc.T("没有找到可导入账号。请先注册账号并获得 access_token/session。"));
                return;
            }

            var plan = BackendCommandPlanner.CreateAccountImport(
                target,
                rows.Select(r => r.Identifier.Trim()).ToList());
            RunBackend(plan);
        }

        private void ExportAccounts() => RunUiTask(ExportAccountsAsync);

        private async Task ExportAccountsAsync()
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
                await ExportAccountsJsonAsync(rows, _lifetimeCts.Token).ConfigureAwait(true);
                return;
            }
            await ExportAccountsConvertedJsonAsync(rows, format, _lifetimeCts.Token).ConfigureAwait(true);
        }

        private async Task ExportAccountsTxtAsync(List<PoolRow> rows)
        {
            AccountExportRunOutcome outcome = await accountExportCoordinator
                .ExportTxtAsync(accountExport, rows, rootDir, Log)
                .ConfigureAwait(true);
            if (outcome.OutputPath is null)
            {
                ShowThemedInfoDialog(Loc.T("一键导出"), Loc.T("没有找到可导出的账号记录。仅支持包含邮箱、密码、客户端ID、刷新令牌的邮箱记录；CFWorker 或缺少密码/刷新令牌的记录会被跳过。"));
                return;
            }
            exportDialogs.ShowExportComplete(this, outcome.OutputPath, outcome.ExportedCount, outcome.SkippedCount, Loc.T("TXT"), Loc.T("账号----密码----客户端ID----刷新令牌"));
        }

        private async Task ExportAccountsJsonAsync(List<PoolRow> rows, CancellationToken ct = default)
        {
            AccountExportRunOutcome outcome = await accountExportCoordinator
                .ExportJsonAsync(accountExport, rows, rootDir, Log, ct)
                .ConfigureAwait(true);
            if (outcome.OutputPath is null)
            {
                ShowThemedInfoDialog(Loc.T("一键导出"), Loc.T("没有找到可导出的 JSON 账号记录。需要账号已生成 session/auth_session 或 SQLite 原始记录。"));
                return;
            }
            exportDialogs.ShowExportComplete(this, outcome.OutputPath, outcome.ExportedCount, outcome.SkippedCount, Loc.T("JSON"), Loc.T("原始账号 session JSON；保留 RT 字段，未获取 RT 的账号默认留空"));
        }

        private async Task ExportAccountsConvertedJsonAsync(List<PoolRow> rows, string format, CancellationToken ct = default)
        {
            AccountExportRunOutcome outcome = await accountExportCoordinator
                .ExportConvertedJsonAsync(
                    accountExport,
                    rows,
                    format,
                    rootDir,
                    (taskName, args, token) => RunBackendWithResultAsync(taskName, args, ct: token),
                    Log,
                    ct)
                .ConfigureAwait(true);

            if (outcome.ErrorMessage is not null)
            {
                ShowThemedInfoDialog(Loc.T("一键导出"), Loc.T("账号格式转换失败：") + outcome.ErrorMessage);
                return;
            }
            if (outcome.MissingOutputFile)
            {
                ShowThemedInfoDialog(Loc.T("一键导出"), Loc.T("账号格式转换没有生成输出文件，请查看下方日志确认 converter 结果。"));
                return;
            }
            if (outcome.OutputPath is null)
            {
                ShowThemedInfoDialog(Loc.T("一键导出"), Loc.T("没有找到可转换的账号 session。需要账号已生成 access_token/session/auth_session 或 SQLite 原始记录。"));
                return;
            }

            exportDialogs.ShowExportComplete(
                this,
                outcome.OutputPath,
                outcome.ExportedCount,
                outcome.SkippedCount,
                ExportViewModel.FormatLabel(outcome.NormalizedFormat),
                ExportViewModel.FormatDescription(outcome.NormalizedFormat));
        }

        private void ShowAccountScanResultDialog(string backendOutput, BackendAccountResultKind resultKind)
        {
            if (exportDialogs.ShowAccountScanResult(this, backendOutput, resultKind)) return;

            string title = BackendResultInterpreter.AccountResultTitle(resultKind);
            ShowThemedInfoDialog(title, Loc.F("{0}已结束，但未解析到结果汇总。请查看下方日志确认详情。", title));
        }

        private string DefaultMailboxClientId()
            => AccountExportCoordinator.ResolveDefaultMailboxClientId(settingsService);
    }
}
