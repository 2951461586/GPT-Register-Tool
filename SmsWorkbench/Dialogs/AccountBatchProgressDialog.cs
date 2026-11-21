namespace SmsWorkbench.Dialogs
{
    /// <summary>
    /// Adapter preserving the task runner's existing progress-dialog API while
    /// the window layout lives in <see cref="AccountBatchProgressWindow"/> XAML
    /// (moved 2026-11-20).
    /// </summary>
    /// <remarks>
    /// This is intentionally not a Window: `MainWindow.Tasks` owns the lifecycle
    /// and calls `Show` / `Update` / `Close` at its progress boundaries. User
    /// cancellation closes are intercepted by the XAML window; programmatic
    /// `Close` bypasses the cancellation callback.
    /// </remarks>
    internal sealed class AccountBatchProgressDialog
    {
        private readonly AccountBatchProgressWindow window;

        public AccountBatchProgressDialog(Window owner, string title, int total, Action cancel)
        {
            window = new AccountBatchProgressWindow(owner, title, total, cancel);
        }

        public void Show() => window.Show();

        public void Update(int completed, int total, string accountRef, string detail)
            => window.UpdateProgress(completed, total, accountRef, detail);

        public void Close() => window.ForceClose();
    }
}
