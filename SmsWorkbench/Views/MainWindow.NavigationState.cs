// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch stays `annotations`.
#nullable enable

namespace SmsWorkbench
{
    /// <summary>
    /// Sidebar "in flight" state.
    ///
    /// The sidebar is a command list, not a navigation rail: every row runs a
    /// batch or opens a dialog (<c>MainWindow.Navigation.cs</c>), and the workbench
    /// itself is a single page (account table + console). There is therefore no
    /// "current page" to highlight — a fabricated one would make the row for
    /// "删除选中" look selected. What is real is *which action is executing*, and a
    /// registration batch runs for 60–100 s, so that state is worth showing.
    ///
    /// Wiring: <c>OnNavigate</c> parks the key it dispatched in
    /// <see cref="pendingNavKey"/>, <c>RunBackendCoreAsync</c> promotes it to
    /// <see cref="ActiveNavKey"/> and clears it in its <c>finally</c>. Dialog-only
    /// actions never promote, so they leave no highlight behind.
    /// </summary>
    public partial class MainWindow
    {
        public static readonly DependencyProperty ActiveNavKeyProperty = DependencyProperty.Register(
            nameof(ActiveNavKey),
            typeof(string),
            typeof(MainWindow),
            new PropertyMetadata(string.Empty));

        /// <summary>Sidebar <c>CommandParameter</c> of the action currently running, or empty.</summary>
        public string ActiveNavKey
        {
            get => (string)GetValue(ActiveNavKeyProperty);
            set => SetValue(ActiveNavKeyProperty, value);
        }

        private string pendingNavKey = string.Empty;

        /// <summary>Reads and resets the key parked by the last navigation.</summary>
        private string TakePendingNavKey()
        {
            string key = pendingNavKey;
            pendingNavKey = string.Empty;
            return key;
        }

        /// <summary>Clears the highlight, but only if another action has not replaced it.</summary>
        private void ClearActiveNavKey(string key)
        {
            if (key.Length > 0 && string.Equals(ActiveNavKey, key, StringComparison.Ordinal))
            {
                ActiveNavKey = string.Empty;
            }
        }
    }
}
