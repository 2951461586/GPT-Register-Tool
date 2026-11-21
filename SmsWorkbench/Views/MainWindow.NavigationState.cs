namespace SmsWorkbench.Views
{
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
            set
            {
                SetValue(ActiveNavKeyProperty, value);
                Shell.ActiveNavKey = value;
            }
        }

        private string TakePendingNavKey() => Shell.TakePendingNavKey();

        private void ClearActiveNavKey(string key)
        {
            Shell.ClearActiveNavKey(key);
            if (!string.Equals(ActiveNavKey, Shell.ActiveNavKey, StringComparison.Ordinal))
            {
                SetValue(ActiveNavKeyProperty, Shell.ActiveNavKey);
            }
        }
    }
}
