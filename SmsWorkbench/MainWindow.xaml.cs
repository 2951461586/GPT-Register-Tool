namespace SmsWorkbench
{
    public partial class MainWindow : FluentWindow, INotifyPropertyChanged
    {
        private Wpf.Ui.Appearance.ApplicationTheme _currentTheme = Wpf.Ui.Appearance.ApplicationTheme.Light;
        private static readonly HttpClient httpClient = new HttpClient();
        private const string LocalNonPaymentProxy = "http://127.0.0.1:7897";
        private readonly IBackendClient backendClient;
        private readonly IBackendTaskCoordinator backendTasks;
        private readonly IDesktopReadClient desktopRead;
        private readonly Serilog.ILogger logger;
        private readonly IPaymentBatchDialogService paymentBatchDialogs;
        private readonly IProtocolPaymentDialogService protocolPaymentDialogs;
        private readonly Wpf.Ui.ISnackbarService snackbarService;
        private readonly ISettingsDialogService settingsDialogs;
        private readonly ISettingsService settingsService;
        private readonly IPaymentBatchService paymentBatchService;
        private readonly string rootDir;
        private readonly CancellationTokenSource _lifetimeCts = new();
        private int taskSeq = 1;
        private string searchText = "";
        private string countText = "1";
        private string pageSizeText = "100";
        private object scopeFilter = "全部";
        private string logText = "";
        private string statusText = "就绪";
        private string pageStatusText = "第 0/0 页";
        private string totalCountText = "0";
        private string trialCountText = "0";
        private string registeredCountText = "0";
        private string attentionCountText = "0";
        private bool sidebarCollapsed;
        private string sidebarToggleGlyph = "‹";
        private Geometry sidebarToggleGeometry = Geometry.Parse("M5 4H19A1 1 0 0 1 20 5V19A1 1 0 0 1 19 20H5A1 1 0 0 1 4 19V5A1 1 0 0 1 5 4Z M10 4V20");
        private Geometry themeIconGeometry;
        private double sidebarAnimTarget;
        private double sidebarAnimStart;
        private EventHandler sidebarRenderingHandler;
        private Stopwatch sidebarAnimStopwatch;

        // Sun icon (light mode): circle + rays
        private static readonly Geometry SunIcon = Geometry.Parse(
            "M12 3V1m0 22v-2M4.22 4.22l1.42 1.42m12.73 12.73l1.42 1.42M1 12h2m18 0h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42 " +
            "M12 7a5 5 0 1 0 0 10 5 5 0 0 0 0-10z");

        // Moon icon (dark mode): crescent
        private static readonly Geometry MoonIcon = Geometry.Parse(
            "M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z");
        private string chataiMailboxFilePath = "";

        public bool SidebarCollapsed
        {
            get => sidebarCollapsed;
            set
            {
                if (sidebarCollapsed == value) return;
                sidebarCollapsed = value;
                OnPropertyChanged(nameof(SidebarCollapsed));
                ApplySidebarCompact(value);
            }
        }

        public string SidebarToggleGlyph
        {
            get => sidebarToggleGlyph;
            set
            {
                if (sidebarToggleGlyph == value) return;
                sidebarToggleGlyph = value ?? "";
                OnPropertyChanged(nameof(SidebarToggleGlyph));
            }
        }

        public Geometry SidebarToggleGeometry
        {
            get => sidebarToggleGeometry;
            set
            {
                if (Equals(sidebarToggleGeometry, value)) return;
                sidebarToggleGeometry = value;
                OnPropertyChanged(nameof(SidebarToggleGeometry));
            }
        }

        public Geometry ThemeIconGeometry
        {
            get => themeIconGeometry;
            set
            {
                if (Equals(themeIconGeometry, value)) return;
                themeIconGeometry = value;
                OnPropertyChanged(nameof(ThemeIconGeometry));
            }
        }

        public event PropertyChangedEventHandler PropertyChanged;

        public ObservableCollection<TaskRow> Tasks { get; } = new ObservableCollection<TaskRow>();

        public int SelectedTabIndex { get; set; }

        public string SearchText
        {
            get => searchText;
            set { searchText = value ?? ""; OnPropertyChanged(nameof(SearchText)); currentPage = 1; RefreshPagedRows(); UpdateSearchClearVisibility(); }
        }

        public string CountText
        {
            get => countText;
            set { countText = value ?? "1"; OnPropertyChanged(nameof(CountText)); }
        }

        public string PageSizeText
        {
            get => pageSizeText;
            set { pageSizeText = value ?? "100"; OnPropertyChanged(nameof(PageSizeText)); currentPage = 1; RefreshPagedRows(); }
        }

        public object ScopeFilter
        {
            get => scopeFilter;
            set { scopeFilter = value; OnPropertyChanged(nameof(ScopeFilter)); currentPage = 1; RefreshPagedRows(); }
        }

        public string ChataiMailboxFilePath
        {
            get => chataiMailboxFilePath;
            set { chataiMailboxFilePath = value ?? ""; OnPropertyChanged(nameof(ChataiMailboxFilePath)); }
        }

        public string LogText
        {
            get => logText;
            set { logText = value ?? ""; OnPropertyChanged(nameof(LogText)); }
        }

        public string StatusText
        {
            get => statusText;
            set { statusText = value ?? ""; OnPropertyChanged(nameof(StatusText)); }
        }

        public string PageStatusText
        {
            get => pageStatusText;
            set { pageStatusText = value ?? ""; OnPropertyChanged(nameof(PageStatusText)); }
        }

        public string TotalCountText
        {
            get => totalCountText;
            set { totalCountText = value ?? "0"; OnPropertyChanged(nameof(TotalCountText)); }
        }

        public string TrialCountText
        {
            get => trialCountText;
            set { trialCountText = value ?? "0"; OnPropertyChanged(nameof(TrialCountText)); }
        }

        public string RegisteredCountText
        {
            get => registeredCountText;
            set { registeredCountText = value ?? "0"; OnPropertyChanged(nameof(RegisteredCountText)); }
        }

        public string AttentionCountText
        {
            get => attentionCountText;
            set { attentionCountText = value ?? "0"; OnPropertyChanged(nameof(AttentionCountText)); }
        }

        public MainWindow(
            IApplicationPaths paths,
            IBackendClient backendClient,
            IBackendTaskCoordinator backendTasks,
            IDesktopReadClient desktopRead,
            IPaymentBatchDialogService paymentBatchDialogs,
            IProtocolPaymentDialogService protocolPaymentDialogs,
            IPaymentBatchService paymentBatchService,
            Wpf.Ui.ISnackbarService snackbarService,
            ISettingsDialogService settingsDialogs,
            ISettingsService settingsService,
            Serilog.ILogger logger)
        {
            this.backendClient = backendClient;
            this.backendTasks = backendTasks;
            this.desktopRead = desktopRead;
            this.paymentBatchDialogs = paymentBatchDialogs;
            this.protocolPaymentDialogs = protocolPaymentDialogs;
            this.paymentBatchService = paymentBatchService;
            this.snackbarService = snackbarService;
            this.settingsDialogs = settingsDialogs;
            this.settingsService = settingsService;
            this.logger = logger;
            rootDir = paths.RootDirectory;
            InitializeComponent();
            snackbarService.SetSnackbarPresenter(SnackbarPresenter);
            DataContext = this;

            // Initialize theme colors on startup
            _currentTheme = Wpf.Ui.Appearance.ApplicationThemeManager.GetAppTheme();
            ApplyCustomThemeColors(_currentTheme);
            ThemeIconGeometry = _currentTheme == Wpf.Ui.Appearance.ApplicationTheme.Dark ? MoonIcon : SunIcon;

            ScopeFilter = "全部";
            RefreshPools();
            ApplySidebarCompact(false);
            Closing += OnWindowClosing;
        }

        private void OnWindowClosing(object? sender, System.ComponentModel.CancelEventArgs e)
        {
            // Signal in-flight async operations (refresh / export / detail / payment …)
            // to abort instead of touching the UI after the window is gone. We do not
            // cancel the close itself — the window still shuts down.
            _lifetimeCts.Cancel();
        }

        internal MainWindow(
            IApplicationPaths paths,
            IBackendClient backendClient,
            IBackendTaskCoordinator backendTasks,
            IDesktopReadClient desktopRead,
            IPaymentBatchDialogService paymentBatchDialogs,
            IPaymentBatchService paymentBatchService,
            Wpf.Ui.ISnackbarService snackbarService,
            ISettingsDialogService settingsDialogs,
            ISettingsService settingsService,
            Serilog.ILogger logger)
            : this(
                paths,
                backendClient,
                backendTasks,
                desktopRead,
                paymentBatchDialogs,
                null,
                paymentBatchService,
                snackbarService,
                settingsDialogs,
                settingsService,
                logger)
        {
        }

        // Moved to MainWindow.Pools.cs: Pool/session loading, filtering, overview.
        // Moved to MainWindow.Register.cs: Registration, SMS and selection mailbox argument builders.

        // Moved to MainWindow.Tasks.cs: Backend process, task list, deletion and cancellation actions.

        // Moved to MainWindow.Theme.cs: Theme, window chrome and sidebar animation.

        // Moved to MainWindow.Payment.cs: Payment link and AT BA-link actions.
        // Moved to MainWindow.Export.cs: Account import/export, scan result and export JSON helpers.

        // Moved to MainWindow.Navigation.cs: Session refresh, row selection and paging filters.

        // Moved to MainWindow.Inbox.cs: Inbox view and mail detail dialog.

        // Moved to MainWindow.Detail.cs: Account detail dialog and detail formatting.
        // Moved to MainWindow.Config.cs: Settings dialog and config persistence.

        // Moved to MainWindow.Helpers.cs: Path/config helpers, status formatting, external open/copy/log helpers.
    }

    public sealed partial class PoolRow : ObservableObject
    {
        [ObservableProperty] private bool isChecked;
        public string Id { get; set; } = "";
        public string CreatedAt { get; set; } = "";
        public string CompletedAt { get; set; } = "";
        public string Identifier { get; set; } = "";
        public string AccountType { get; set; } = "";
        public string AccountPlanType { get; set; } = "";
        public string Source { get; set; } = "";
        public string RegisterMethod { get; set; } = "";
        public string SessionType { get; set; } = "";
        public string PlanType { get; set; } = "";
        public string RegistrationCountry { get; set; } = "";
        public string Status { get; set; } = "";
        public string PayPalStatus { get; set; } = "";
        public string PayPalAmount { get; set; } = "";
        public string PromotionStatus { get; set; } = "";
        public string RefreshTokenStatus { get; set; } = "";
        public string TwoFactorStatus { get; set; } = "未设置";
        public string Phone { get; set; } = "";
        public bool HasAccessToken { get; set; }
        public string AccessTokenProbeStatusCode { get; set; } = "";
        public string AccessTokenStatus => AccessTokenState.Display(HasAccessToken, AccessTokenProbeStatusCode);
        public string PayPalUrl { get; set; } = "";
        public string RefreshToken { get; set; } = "";
        public string Proxy { get; set; } = "";
        public string Notes { get; set; } = "";
        public string SourcePath { get; set; } = "";
        public string RawLine { get; set; } = "";
        public string MailboxLine { get; set; } = "";
        public string ClientId { get; set; } = "";
        public string RawRefreshToken { get; set; } = "";
        public string MailboxProvider { get; set; } = "";
        public string MailboxToken { get; set; } = "";
    }

    public sealed class RegisterOptions
    {
        public string Source { get; set; } = "pool";
        public int Count { get; set; } = 1;
        public int Workers { get; set; } = 4;
        public bool Disable2fa { get; set; } = true;
        public bool CheckPromotion { get; set; }
    }

    public sealed class ScanOptions
    {
        public int Workers { get; set; } = 4;
        public bool AutoRelogin { get; set; }
    }

    public sealed partial class TaskRow : ObservableObject
    {
        [ObservableProperty] private string status = "";
        [ObservableProperty] private string cost = "";
        [ObservableProperty] private string doneAt = "";
        public string Name { get; set; } = "";
        public string Task { get; set; } = "";
        public string Info { get; set; } = "";
        public string Retry { get; set; } = "0";
    }

    /// <summary>
    /// Converts a raw RefreshTokenStatus value (e.g. "oauth_present",
    /// "legacy_present", "no_rt") into a short display label.
    /// </summary>
    public sealed class RtDisplayConverter : IValueConverter
    {
        public object Convert(object value, Type targetType, object parameter, CultureInfo culture)
        {
            string s = (value as string ?? "").Trim();
            if (s.Equals("oauth_present", StringComparison.OrdinalIgnoreCase)) return "已获取";
            if (s.Equals("legacy_present", StringComparison.OrdinalIgnoreCase)) return "旧Token";
            if (s.Equals("no_rt", StringComparison.OrdinalIgnoreCase)) return "无RT";
            if (s.Equals("missing", StringComparison.OrdinalIgnoreCase)) return "缺失";
            return s.Length > 0 ? s : "—";
        }

        public object ConvertBack(object value, Type targetType, object parameter, CultureInfo culture)
        {
            throw new NotSupportedException();
        }
    }

}
