namespace SmsWorkbench
{
    public sealed class AccountBatchProgressTracker
    {
        private readonly HashSet<string> completedAccounts = new(StringComparer.OrdinalIgnoreCase);

        public AccountBatchProgressTracker(string domain, int total)
        {
            Domain = domain ?? "";
            Total = Math.Max(0, total);
        }

        public string Domain { get; }
        public int Total { get; private set; }
        public int Completed => completedAccounts.Count;

        public bool Update(BackendProgressEvent progress)
        {
            if (progress == null || !string.Equals(progress.Domain, Domain, StringComparison.OrdinalIgnoreCase))
                return false;
            if (progress.Total > 0)
                Total = progress.Total;
            if (!progress.Terminal || string.IsNullOrWhiteSpace(progress.AccountRef))
                return false;
            return completedAccounts.Add(progress.AccountRef.Trim());
        }
    }

    internal sealed class AccountBatchProgressDialog
    {
        private readonly Window window;
        private readonly ProgressBar progressBar;
        private readonly TextBlock countText;
        private readonly TextBlock detailText;
        private readonly Button cancelButton;

        public AccountBatchProgressDialog(Window owner, string title, int total, Action cancel)
        {
            window = new Window
            {
                Title = title,
                Owner = owner,
                Width = 560,
                Height = 240,
                MinWidth = 520,
                MinHeight = 220,
                ResizeMode = ResizeMode.NoResize,
                WindowStartupLocation = WindowStartupLocation.CenterOwner,
                Background = (Brush)owner.FindResource("AppBg"),
                ShowInTaskbar = false,
            };

            var root = new Grid { Margin = new Thickness(24, 20, 24, 24) };
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });

            countText = new TextBlock
            {
                Text = $"0 / {Math.Max(0, total)}",
                FontSize = 18,
                FontWeight = FontWeights.SemiBold,
                Foreground = (Brush)owner.FindResource("TextMain"),
            };
            root.Children.Add(countText);

            progressBar = new ProgressBar
            {
                Minimum = 0,
                Maximum = Math.Max(1, total),
                Value = 0,
                Height = 16,
                Margin = new Thickness(0, 14, 0, 10),
            };
            Grid.SetRow(progressBar, 1);
            root.Children.Add(progressBar);

            detailText = new TextBlock
            {
                Text = "正在准备账号...",
                TextWrapping = TextWrapping.Wrap,
                Foreground = (Brush)owner.FindResource("TextSub"),
                MinHeight = 40,
            };
            Grid.SetRow(detailText, 2);
            root.Children.Add(detailText);

            cancelButton = new Button
            {
                Content = "取消任务",
                Width = 92,
                HorizontalAlignment = HorizontalAlignment.Right,
                VerticalAlignment = VerticalAlignment.Center,
                Margin = new Thickness(0, 10, 0, 0),
                Style = (Style)owner.FindResource("SecondaryButton"),
            };
            cancelButton.Click += (_, __) =>
            {
                cancelButton.IsEnabled = false;
                detailText.Text = "正在取消...";
                cancel?.Invoke();
            };
            Grid.SetRow(cancelButton, 3);
            root.Children.Add(cancelButton);

            window.Closing += (_, e) =>
            {
                if (!cancelButton.IsEnabled) return;
                e.Cancel = true;
                cancelButton.IsEnabled = false;
                detailText.Text = "正在取消...";
                cancel?.Invoke();
            };
            window.Content = root;
        }

        public void Show() => window.Show();

        public void Update(int completed, int total, string accountRef, string detail)
        {
            int safeTotal = Math.Max(0, total);
            int safeCompleted = Math.Min(Math.Max(0, completed), safeTotal > 0 ? safeTotal : completed);
            progressBar.Maximum = Math.Max(1, safeTotal);
            progressBar.Value = Math.Min(safeCompleted, progressBar.Maximum);
            countText.Text = $"{safeCompleted} / {safeTotal}";
            string account = string.IsNullOrWhiteSpace(accountRef) ? "" : accountRef.Trim();
            detailText.Text = account.Length > 0 && !string.IsNullOrWhiteSpace(detail)
                ? $"{account}  {detail.Trim()}"
                : account.Length > 0 ? account : detail ?? "";
        }

        public void Close()
        {
            cancelButton.IsEnabled = false;
            window.Close();
        }
    }
}
