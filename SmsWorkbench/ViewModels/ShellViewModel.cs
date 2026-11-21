namespace SmsWorkbench.ViewModels
{
    /// <summary>
    /// Observable shell state and child ViewModel composition for the desktop window.
    /// Extracted from <c>MainWindow</c> so the window acts as a view host while shell
    /// state, navigation-in-flight tracking, and child ViewModels live in a dedicated
    /// ViewModel testable without a WPF visual tree.
    /// </summary>
    public sealed class ShellViewModel : ObservableObject
    {
        private bool sidebarCollapsed;
        private string activeNavKey = string.Empty;
        private string pendingNavKey = string.Empty;
        private DesktopShellPreferences shellPreferences = DesktopShellPreferences.Default;

        public LogViewModel LogPanel { get; } = new();

        public TaskListViewModel TaskList { get; } = new();

        public PoolViewModel Pool { get; } = new();

        public ExportViewModel Export { get; }

        public RegisterViewModel Register { get; } = new();

        public MetricsViewModel Metrics { get; } = new();

        public StatusBarViewModel StatusBar { get; } = new();

        public event EventHandler<bool>? SidebarCollapsedChanged;

        public ShellViewModel()
        {
            Export = new ExportViewModel(Pool);
            Pool.PageRefreshed += (_, _) => StatusBar.Text = Pool.SummaryText;
        }

        public bool SidebarCollapsed
        {
            get => sidebarCollapsed;
            set
            {
                if (!SetProperty(ref sidebarCollapsed, value))
                {
                    return;
                }
                SidebarCollapsedChanged?.Invoke(this, value);
            }
        }

        public string ActiveNavKey
        {
            get => activeNavKey;
            set => SetProperty(ref activeNavKey, value ?? string.Empty);
        }

        public DesktopShellPreferences ShellPreferences
        {
            get => shellPreferences;
            set => SetProperty(ref shellPreferences, value);
        }

        public void ParkPendingNavKey(string? key)
        {
            ActiveNavKey = string.Empty;
            pendingNavKey = key ?? string.Empty;
        }

        public string TakePendingNavKey()
        {
            string key = pendingNavKey;
            pendingNavKey = string.Empty;
            return key;
        }

        public void ClearActiveNavKey(string key)
        {
            if (key.Length > 0 && string.Equals(ActiveNavKey, key, StringComparison.Ordinal))
            {
                ActiveNavKey = string.Empty;
            }
        }
    }
}
