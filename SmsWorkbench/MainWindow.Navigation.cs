// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch stays `annotations`.
#nullable enable

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
            ["payment_eligibility"] = CheckPaymentEligibility,
            ["paylink"] = OpenPayPalLink,
            ["batchpay"] = BatchProtocolPayment,
            ["importmail"] = ImportChataiMailbox,
            ["inbox"] = ViewInbox,
            ["changeemail"] = ChangeEmail,
            ["importcpa"] = ImportPaidCpa,
            ["export"] = ExportAccounts,
            ["delete"] = DeleteSelected,
            ["refresh"] = RefreshPools,
            ["settings"] = ShowConfigDialog,
            ["theme"] = ToggleTheme,
            ["cancelbatch"] = CancelBatch,
        };

        private void OnNavigate(string? key)
        {
            if (key != null && NavActions.TryGetValue(key, out Action? action)) action();
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
                args.Add("--session-file");
                args.Add(jsonPath);
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
            PoolRow? row = Pool.SelectedRow ?? (AccountGrid.SelectedItem as PoolRow);
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
            string detail = string.IsNullOrWhiteSpace(action) ? "执行此操作" : action.Trim();
            ShowThemedInfoDialog("未选择邮箱", $"请先勾选或选择邮箱账号后再{detail}。");
        }

        private List<PoolRow> SelectedRowsOrCurrent()
        {
            var rows = Pool.CheckedRows();
            if (rows.Count == 0)
            {
                PoolRow? row = Pool.SelectedRow ?? (AccountGrid.SelectedItem as PoolRow);
                if (row != null) rows.Add(row);
            }
            return rows;
        }

        private void AccountGrid_Sorting(object sender, DataGridSortingEventArgs e)
        {
            string member = (e.Column.SortMemberPath ?? "").Trim();
            if (member.Length == 0) return;

            e.Handled = true;
            ListSortDirection next = e.Column.SortDirection == ListSortDirection.Ascending
                ? ListSortDirection.Descending
                : ListSortDirection.Ascending;
            foreach (DataGridColumn column in AccountGrid.Columns)
            {
                column.SortDirection = null;
            }
            e.Column.SortDirection = next;
            Pool.SortBy(member, next);
        }

        private void OnPoolSelectionCleared(object? sender, EventArgs e)
        {
            if (AccountGrid != null)
            {
                AccountGrid.SelectedItem = null;
                AccountGrid.SelectedIndex = -1;
                AccountGrid.UnselectAll();
            }
        }
    }
}
