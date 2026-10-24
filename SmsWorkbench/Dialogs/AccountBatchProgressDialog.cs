// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch stays `annotations`.
#nullable enable

using SmsWorkbench.Localization;

namespace SmsWorkbench
{
    /// <summary>
    /// Modal progress window for a long account batch (registration, liveness).
    ///
    /// <para>
    /// Lives in <c>Dialogs/</c> rather than next to <see cref="AccountBatchProgressTracker"/>
    /// because it is a window: <c>Models/</c> owns desktop-side contracts, and the
    /// folder layout test (<c>tests/workbench/test_sms_workbench_layout.py</c>) plus
    /// <c>docs/current/ui-architecture.md</c> put every window in <c>Views/</c> or
    /// <c>Dialogs/</c>. It was moved here on 2026-10-25; the namespace is unchanged,
    /// so no caller changed.
    /// </para>
    /// <para>
    /// The four row/count/detail strings go through <see cref="Loc"/>. They used to be
    /// hard-coded Chinese inside a file the localization gate never looked at, so the
    /// English UI still showed 正在准备账号... / 取消任务 / 正在取消...
    /// </para>
    /// </summary>
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

            // Resolved once per dialog, not per update: the language can change while
            // the window is open, and re-reading Loc.T on every progress tick would
            // leave a half-translated window (WPF already re-runs bindings, not code).
            string preparing = Loc.T("正在准备账号...");
            string cancelling = Loc.T("正在取消...");

            detailText = new TextBlock
            {
                Text = preparing,
                TextWrapping = TextWrapping.Wrap,
                Foreground = (Brush)owner.FindResource("TextSub"),
                MinHeight = 40,
            };
            Grid.SetRow(detailText, 2);
            root.Children.Add(detailText);

            cancelButton = new Button
            {
                Content = Loc.T("取消任务"),
                Width = 92,
                HorizontalAlignment = HorizontalAlignment.Right,
                VerticalAlignment = VerticalAlignment.Center,
                Margin = new Thickness(0, 10, 0, 0),
                Style = (Style)owner.FindResource("SecondaryButton"),
            };
            cancelButton.Click += (_, __) =>
            {
                cancelButton.IsEnabled = false;
                detailText.Text = cancelling;
                cancel?.Invoke();
            };
            Grid.SetRow(cancelButton, 3);
            root.Children.Add(cancelButton);

            window.Closing += (_, e) =>
            {
                if (!cancelButton.IsEnabled) return;
                e.Cancel = true;
                cancelButton.IsEnabled = false;
                detailText.Text = cancelling;
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
