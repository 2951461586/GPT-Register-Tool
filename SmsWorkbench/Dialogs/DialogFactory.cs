namespace SmsWorkbench.Dialogs
{
    /// <summary>
    /// Thin facade over <see cref="InfoConfirmDialogWindow"/> for standard info
    /// and confirmation dialogs. All window chrome and layout live in XAML.
    /// </summary>
    public static class DialogFactory
    {
        public static async Task ShowInfoAsync(Window owner, string title, string message, string? okText = null)
        {
            var dialog = new InfoConfirmDialogWindow(owner, title, message, confirmText: okText, showCancel: false);
            await DialogShowAsync(dialog);
        }

        public static async Task<bool> ShowConfirmAsync(
            Window owner,
            string title,
            string message,
            string? confirmText = null,
            bool isDanger = false)
        {
            var dialog = new InfoConfirmDialogWindow(
                owner,
                title,
                message,
                confirmText: confirmText,
                showCancel: true,
                isDanger: isDanger);
            await DialogShowAsync(dialog);
            return dialog.Confirmed;
        }

        /// <summary>
        /// Show a dialog window without blocking the calling async chain.
        /// Resolves when the window closes.
        /// </summary>
        private static Task DialogShowAsync(Window dialog)
        {
            var tcs = new TaskCompletionSource<bool>();
            dialog.Closed += (_, __) => tcs.TrySetResult(true);
            dialog.ShowDialog();
            return tcs.Task;
        }
    }
}
