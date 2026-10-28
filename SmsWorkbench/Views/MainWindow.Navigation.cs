// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch stays `annotations`.
#nullable enable

using SmsWorkbench.Localization;

namespace SmsWorkbench
{
    public partial class MainWindow
    {
        // Sidebar navigation: single command routes the CommandParameter keys to
        // the action methods in the other MainWindow partials.
        // Assigned in the constructor (property initializers cannot capture instance methods).
        public RelayCommand<string> NavCommand { get; } = null!;

        private Dictionary<string, Action>? navActions;

        private Dictionary<string, Action> NavActions => navActions ??= new Dictionary<string, Action>
        {
            ["register"] = OneClickRegister,
            ["sms"] = OneClickSms,
            ["scan"] = OneClickScan,
            ["promotion"] = CheckPromotion,
            // 支付资格 (2026-10-26): batch checkout-capability probe. Keyed on the
            // badge's stable machine state (sms_tool/payment/qualification_states.py).
            ["paymentqualification"] = CheckPaymentQualification,
            ["paylink"] = OpenPayPalLink,
            ["batchpay"] = BatchProtocolPayment,
            ["importmail"] = ImportChataiMailbox,
            ["inbox"] = ViewInbox,
            ["changeemail"] = ChangeEmail,
            ["importcpa"] = ImportPaidCpa,
            ["export"] = ExportAccounts,
            ["delete"] = DeleteSelected,
            ["refresh"] = RefreshPools,
            ["proxypool"] = ShowProxyPool,
            ["settings"] = ShowConfigDialog,
            ["shell"] = ShowShellSettings,
            ["theme"] = ToggleTheme,
            ["language"] = ToggleLanguage,
            ["cancelbatch"] = CancelBatch,
            // Workspace archive: the state that cannot be regenerated (account rows,
            // config shards, UI preferences). Restore keeps pre-restore copies.
            ["backup"] = BackupWorkspace,
            ["restore"] = RestoreWorkspace,
        };

        private void OnNavigate(string? key)
        {
            // Every row here is an action, not a destination (see
            // MainWindow.NavigationState): park the key so the backend funnel can
            // light the row while its batch runs. The highlight is cleared first, so
            // a dialog-only action can never inherit the previous batch's mark.
            ActiveNavKey = string.Empty;
            pendingNavKey = key ?? string.Empty;
            if (key != null && NavActions.TryGetValue(key, out Action? action)) action();
        }

        /// <summary>
        /// Sidebar entry point for the read-only proxy-pool admin window.
        /// <para>
        /// Only one backend task may run at a time (the coordinator enforces it),
        /// and every command in that window is a backend run. Refuse to open it
        /// while a batch is live instead of letting "加载配置池" fail with
        /// <see cref="BackendTaskAlreadyRunningException"/>.
        /// </para>
        /// </summary>
        private void ShowProxyPool()
        {
            if (backendTasks.IsRunning)
            {
                ShowThemedInfoDialog(Loc.T("代理池管理"), Loc.T("已有批次正在运行，请先取消或等待完成。"));
                return;
            }

            proxyPoolDialogs.ShowDialog(this);
            RefreshPools();
        }

        // NOTE (2026-09-02): a "refresh session for one account" handler used to
        // live here (`--email <id> --refresh-session [--session-file <path>]`).
        // Nothing referenced it - not XAML, not the NavCommand switch - so the
        // feature had no UI entry point and the handler was deleted. Restore
        // from git history if that capability is wanted again.

        private void AddSessionFileArg(List<string> args, PoolRow row)
        {
            string jsonPath = SessionFileFor(row);
            if (jsonPath.Length > 0)
            {
                BackendCommandPlanner.AppendSessionFile(args, jsonPath);
            }
        }

        private static string SessionFileFor(PoolRow row)
        {
            if (row == null) return "";
            string jsonPath = File.Exists(row.Notes) && row.Notes.EndsWith(".json", StringComparison.OrdinalIgnoreCase)
                ? row.Notes
                : row.SourcePath;
            return File.Exists(jsonPath) && jsonPath.EndsWith(".json", StringComparison.OrdinalIgnoreCase)
                ? jsonPath
                : "";
        }

        private PoolRow? SelectedEmailRowOrNotify(string action)
        {
            PoolRow? row = Pool.SelectedRow ?? accountGridControl.SelectedRow;
            if (row == null)
            {
                ShowEmailSelectionRequired(action);
            }
            return row;
        }

        private List<PoolRow> SelectedEmailRowsOrNotify(string action)
        {
            var rows = SelectedRowsOrCurrent()
                .Where(row => !string.IsNullOrWhiteSpace(row.Identifier))
                .GroupBy(row => row.Identifier.Trim().ToLowerInvariant())
                .Select(group => group.First())
                .ToList();
            if (rows.Count == 0)
            {
                ShowEmailSelectionRequired(action);
            }
            return rows;
        }

        private void ShowEmailSelectionRequired(string action)
        {
            string detail = string.IsNullOrWhiteSpace(action) ? Loc.T("执行此操作") : action.Trim();
            ShowThemedInfoDialog(Loc.T("未选择邮箱"), Loc.F("请先勾选或选择邮箱账号后再{0}。", detail));
        }

        private List<PoolRow> SelectedRowsOrCurrent()
        {
            var rows = Pool.CheckedRows();
            if (rows.Count == 0)
            {
                PoolRow? row = Pool.SelectedRow ?? accountGridControl.SelectedRow;
                if (row != null) rows.Add(row);
            }
            return rows;
        }

        private void OnPoolSelectionCleared(object? sender, EventArgs e)
            => accountGridControl?.ClearSelection();
    }
}
