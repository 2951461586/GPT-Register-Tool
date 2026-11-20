// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch stays `annotations`.
#nullable enable

using SmsWorkbench.Localization;

namespace SmsWorkbench
{
    /// <summary>
    /// Visual progress window for a long account batch. Layout is XAML since
    /// 2026-11-20; <see cref="AccountBatchProgressDialog"/> preserves the existing
    /// non-Window adapter API used by the task runner.
    /// </summary>
    internal partial class AccountBatchProgressWindow : Window
    {
        private readonly Action cancel;
        private readonly string cancellingText = Loc.T("正在取消...");
        private bool cancellationRequested;
        private bool allowClose;

        public bool CancellationRequested => cancellationRequested;

        public void ForceClose()
        {
            allowClose = true;
            Close();
        }

        public AccountBatchProgressWindow(Window owner, string title, int total, Action cancel)
        {
            InitializeComponent();
            Owner = owner;
            Title = title;
            this.cancel = cancel;
            ProgressIndicator.Maximum = Math.Max(1, total);
            CountText.Text = $"0 / {Math.Max(0, total)}";
            Closing += OnClosing;
        }

        public void UpdateProgress(int completed, int total, string accountRef, string detail)
        {
            int safeTotal = Math.Max(0, total);
            int safeCompleted = Math.Min(Math.Max(0, completed), safeTotal > 0 ? safeTotal : completed);
            ProgressIndicator.Maximum = Math.Max(1, safeTotal);
            ProgressIndicator.Value = Math.Min(safeCompleted, ProgressIndicator.Maximum);
            CountText.Text = $"{safeCompleted} / {safeTotal}";
            string account = string.IsNullOrWhiteSpace(accountRef) ? "" : accountRef.Trim();
            DetailText.Text = account.Length > 0 && !string.IsNullOrWhiteSpace(detail)
                ? $"{account}  {detail.Trim()}"
                : account.Length > 0 ? account : detail ?? "";
        }

        private void Cancel_Click(object sender, RoutedEventArgs e) => RequestCancellation();

        private void OnClosing(object? sender, CancelEventArgs e)
        {
            if (allowClose || cancellationRequested) return;
            e.Cancel = true;
            RequestCancellation();
        }

        private void RequestCancellation()
        {
            if (cancellationRequested) return;
            cancellationRequested = true;
            CancelButton.IsEnabled = false;
            DetailText.Text = cancellingText;
            cancel();
        }
    }
}
