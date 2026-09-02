// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch stays `annotations`.
#nullable enable

namespace SmsWorkbench
{
    public partial class MainWindow
    {
        // Sidebar navigation: single command routes 16 CommandParameters to the
        // existing Click handlers (kept as-is in their partial files).
        // Assigned in the constructor (property initializers cannot capture instance methods).
        public RelayCommand<string> NavCommand { get; } = null!;

        private void OnNavigate(string? key)
        {
            switch (key)
            {
                case "register": OneClickRegister_Click(this, new RoutedEventArgs()); break;
                case "sms": OneClickSms_Click(this, new RoutedEventArgs()); break;
                case "scan": OneClickScan_Click(this, new RoutedEventArgs()); break;
                case "promotion": CheckPromotion_Click(this, new RoutedEventArgs()); break;
                case "paylink": OpenPayPalLink_Click(this, new RoutedEventArgs()); break;
                case "batchpay": BatchProtocolPayment_Click(this, new RoutedEventArgs()); break;
                case "importmail": ImportChataiMailbox_Click(this, new RoutedEventArgs()); break;
                case "inbox": ViewInbox_Click(this, new RoutedEventArgs()); break;
                case "changeemail": ChangeEmail_Click(this, new RoutedEventArgs()); break;
                case "importcpa": ImportPaidCpa_Click(this, new RoutedEventArgs()); break;
                case "export": ExportAccounts_Click(this, new RoutedEventArgs()); break;
                case "delete": DeleteSelected_Click(this, new RoutedEventArgs()); break;
                case "refresh": Refresh_Click(this, new RoutedEventArgs()); break;
                case "settings": Settings_Click(this, new RoutedEventArgs()); break;
                case "theme": ToggleTheme_Click(this, new RoutedEventArgs()); break;
                case "cancelbatch": CancelBatch_Click(this, new RoutedEventArgs()); break;
            }
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
            PoolRow? row = SelectedRow ?? (AccountGrid.SelectedItem as PoolRow);
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
            var rows = allRows.Where(r => r.IsChecked).ToList();
            if (rows.Count == 0)
            {
                PoolRow? row = SelectedRow ?? (AccountGrid.SelectedItem as PoolRow);
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
            accountSortMember = member;
            accountSortDirection = next;
            currentPage = 1;
            RefreshPagedRows();
        }

        private void FirstPage_Click(object sender, RoutedEventArgs e)
        {
            currentPage = 1;
            RefreshPagedRows();
        }

        private void PrevPage_Click(object sender, RoutedEventArgs e)
        {
            currentPage--;
            RefreshPagedRows();
        }

        private void NextPage_Click(object sender, RoutedEventArgs e)
        {
            currentPage++;
            RefreshPagedRows();
        }

        private void LastPage_Click(object sender, RoutedEventArgs e)
        {
            int pageSize = PageSizeValue();
            int count = allRows.Count(FilterRow);
            currentPage = Math.Max(1, (int)Math.Ceiling(count / (double)pageSize));
            RefreshPagedRows();
        }

        private void ClearSelection_Click(object sender, RoutedEventArgs e)
        {
            foreach (PoolRow row in allRows) row.IsChecked = false;
            SelectedRow = null;
            OnPropertyChanged(nameof(SelectedRow));
            if (AccountGrid != null)
            {
                AccountGrid.SelectedItem = null;
                AccountGrid.SelectedIndex = -1;
                AccountGrid.UnselectAll();
            }
        }

        private void SelectAllFiltered_Click(object sender, RoutedEventArgs e)
        {
            foreach (PoolRow row in allRows.Where(FilterRow))
            {
                row.IsChecked = true;
            }
        }
    }
}
