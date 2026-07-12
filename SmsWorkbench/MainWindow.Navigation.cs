namespace SmsWorkbench
{
    public partial class MainWindow
    {
        // Session refresh, row selection and paging filters
        private void RefreshSession_Click(object sender, RoutedEventArgs e)
        {
            PoolRow row = SelectedAccountRow();
            if (row == null) return;
            var args = new List<string> { "--email", row.Identifier, "--refresh-session" };
            AddSessionFileArg(args, row);
            RunBackend("刷新Session", args);
        }

        private void AddSessionFileArg(List<string> args, PoolRow row)
        {
            string jsonPath = File.Exists(row.Notes) && row.Notes.EndsWith(".json", StringComparison.OrdinalIgnoreCase)
                ? row.Notes
                : row.SourcePath;
            if (File.Exists(jsonPath) && jsonPath.EndsWith(".json", StringComparison.OrdinalIgnoreCase))
            {
                args.Add("--session-file");
                args.Add(jsonPath);
            }
        }

        private PoolRow SelectedAccountRow()
        {
            PoolRow row = SelectedRow ?? (AccountGrid.SelectedItem as PoolRow);
            if (row == null)
            {
                MessageBox.Show("请先选择一条账号记录。", "未选择账号", MessageBoxButton.OK, MessageBoxImage.Information);
            }
            return row;
        }

        private List<PoolRow> SelectedRowsOrCurrent()
        {
            var rows = allRows.Where(r => r.IsChecked).ToList();
            if (rows.Count == 0)
            {
                PoolRow row = SelectedRow ?? (AccountGrid.SelectedItem as PoolRow);
                if (row != null) rows.Add(row);
            }
            return rows;
        }

        private void ApplyFilter_Click(object sender, RoutedEventArgs e)
        {
            currentPage = 1;
            RefreshPagedRows();
        }

        private void ShowAll_Click(object sender, RoutedEventArgs e) => SetScope("全部");

        private void ShowMailboxPool_Click(object sender, RoutedEventArgs e) => SetScope("邮箱池");

        private void ShowRegistered_Click(object sender, RoutedEventArgs e) => SetScope("已注册");

        private void ShowPending_Click(object sender, RoutedEventArgs e) => SetScope("待处理");

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

        private void SetScope(string scope)
        {
            ScopeFilter = scope;
            currentPage = 1;
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
