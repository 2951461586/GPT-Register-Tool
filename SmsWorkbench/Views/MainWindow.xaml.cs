// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch stays `annotations`.
#nullable enable

using SmsWorkbench.Localization;

namespace SmsWorkbench
{
    [System.Diagnostics.CodeAnalysis.SuppressMessage(
        "Design",
        "CA1001:Types that own disposable fields should be disposable",
        Justification = "WPF Window lifetime is bound to Closing/Closed events.")]
    public partial class MainWindow : FluentWindow, INotifyPropertyChanged
    {
        private Wpf.Ui.Appearance.ApplicationTheme _currentTheme = Wpf.Ui.Appearance.ApplicationTheme.Light;

        // The operator's mode, not a palette: System keeps following the OS (see
        // MainWindow.Theme.cs). Persisted in runtime/desktop_theme.txt. Named with the
        // underscore prefix on purpose: .NET 9+ puts a `ThemeMode` on Window for its own
        // system-theme support, and an unqualified member of that name would shadow it.
        private DesktopThemeMode _themeMode = DesktopThemeMode.System;

        // Mirrors SystemThemeWatcher's own bookkeeping so arming/disarming is
        // idempotent; a second Watch() for the same window would double the hooks.
        private bool systemThemeWatcherArmed;
        // Shared instance for the one-click SMS provider catalog/balance reads in
        // MainWindow.SmsProvider.cs (SmsProviderCatalogClient takes the HttpClient
        // as a parameter). One static instance per window is deliberate: a
        // per-call `new HttpClient()` leaks sockets, and these are a handful of
        // small GETs against a provider API, not a long-lived backend channel.
        private static readonly HttpClient httpClient = new HttpClient();
        private readonly IBackendClient backendClient;
        private readonly IBackendTaskCoordinator backendTasks;
        private readonly IDesktopReadClient desktopRead;
        private readonly Serilog.ILogger logger;
        private readonly IPaymentBatchDialogService paymentBatchDialogs;
        private readonly IProxyPoolDialogService proxyPoolDialogs;
        private readonly Wpf.Ui.ISnackbarService snackbarService;
        private readonly ISettingsDialogService settingsDialogs;
        private readonly ISettingsService settingsService;
        private readonly IPaymentBatchService paymentBatchService;
        private readonly IFileLauncher fileLauncher;
        // Shell chrome handed in by the composition root. Both hold OS handles and
        // outlive a Hide (that is the point of close-to-tray); see OnWindowClosing.
        private readonly ITrayIconService trayIcon;
        private readonly IWorkspaceBackupService workspaceBackup;
        private readonly IGlobalHotkeyService globalHotkey;
        private readonly string rootDir;
        private readonly string configDir;
        private readonly CancellationTokenSource _lifetimeCts = new();
        // Resolved by name instead of using the XAML-generated fields. The
        // generated fields only exist after the WPF markup compiler runs, which
        // per-file analyzers (and a fresh LSP) do not see; `FindName` keeps
        // these code-behind references resolvable.
        private readonly AccountGridView accountGridControl;
        private readonly WorkbenchConsole consoleControl;
        public LogViewModel LogPanel { get; } = new LogViewModel();
        private bool sidebarCollapsed;
        // Non-null from the constructor (MainWindow.xaml.cs assigns it right
        // after InitializeComponent), but the compiler cannot see that through
        // the property setter, so give it a real empty geometry rather than
        // `= null!` - a null here would leave the theme button icon-less.
        private Geometry themeIconGeometry = Geometry.Empty;
        private double sidebarAnimTarget;
        private double sidebarAnimStart;
        // Declared here but assigned and read from MainWindow.Sidebar.cs - this
        // is a partial class, so "no references" in this file means nothing.
        private EventHandler? sidebarRenderingHandler;
        private Stopwatch? sidebarAnimStopwatch;
        private readonly DispatcherTimer metricsTimer;

        // Sun icon (light mode): circle + rays
        // Theme row icons come from the shared glyph registry (Themes/Glyphs.xaml)
        // like every other icon. They used to be inline Feather-style paths here,
        // which is exactly how a second copy of an icon family starts drifting away
        // from the first; the registry now holds the whole set.
        private Geometry sunIcon = Geometry.Empty;

        // Moon icon (dark mode): crescent
        private Geometry moonIcon = Geometry.Empty;

        // "Auto" icon (follow the OS): the mode has no palette of its own, so it gets
        // its own glyph rather than borrowing whichever palette happens to be active.
        private Geometry systemIcon = Geometry.Empty;

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

        // An event with no subscribers is null - that is the framework's own
        // contract, so the field is nullable rather than force-initialised.
        public event PropertyChangedEventHandler? PropertyChanged;

        public TaskListViewModel TaskList { get; } = new TaskListViewModel();

        public PoolViewModel Pool { get; } = new PoolViewModel();

        public ExportViewModel Export { get; }

        public RegisterViewModel Register { get; } = new RegisterViewModel();

        /// <summary>Rolling throughput / success strip in the status bar.</summary>
        public MetricsViewModel Metrics { get; } = new MetricsViewModel();

        public StatusBarViewModel StatusBar { get; } = new StatusBarViewModel();

        // SelectedTabIndex removed (2026-09-02, round 6): no binding and no
        // code reads it. Tab selection is driven by the sidebar NavCommand.

        public MainWindow(
            IApplicationPaths paths,
            IBackendClient backendClient,
            IBackendTaskCoordinator backendTasks,
            IDesktopReadClient desktopRead,
            IPaymentBatchDialogService paymentBatchDialogs,
            IProxyPoolDialogService proxyPoolDialogs,
            IPaymentBatchService paymentBatchService,
            Wpf.Ui.ISnackbarService snackbarService,
            ISettingsDialogService settingsDialogs,
            ISettingsService settingsService,
            Serilog.ILogger logger,
            IFileLauncher fileLauncher,
            IUpdateChecker updateChecker,
            IWorkspaceBackupService workspaceBackupService,
            ITrayIconService trayIconService,
            IGlobalHotkeyService globalHotkeyService)
        {
            this.backendClient = backendClient;
            this.backendTasks = backendTasks;
            this.desktopRead = desktopRead;
            this.paymentBatchDialogs = paymentBatchDialogs;
            this.proxyPoolDialogs = proxyPoolDialogs;
            this.paymentBatchService = paymentBatchService;
            this.snackbarService = snackbarService;
            this.settingsDialogs = settingsDialogs;
            this.settingsService = settingsService;
            this.logger = logger;
            this.fileLauncher = fileLauncher;
            this.updateChecker = updateChecker;
            this.workspaceBackup = workspaceBackupService;
            this.trayIcon = trayIconService;
            this.globalHotkey = globalHotkeyService;
            rootDir = paths.RootDirectory;
            configDir = paths.ConfigDirectory;
            Export = new ExportViewModel(Pool);
            InitializeComponent();
            accountGridControl = (AccountGridView)(
                FindName("AccountGridControl")
                ?? throw new InvalidOperationException("MainWindow.xaml is missing x:Name=\"AccountGridControl\""));
            consoleControl = (WorkbenchConsole)(
                FindName("ConsoleControl")
                ?? throw new InvalidOperationException("MainWindow.xaml is missing x:Name=\"ConsoleControl\""));
            snackbarService.SetSnackbarPresenter(SnackbarPresenter);
            // Before DataContext: the header binds VersionText and the two update
            // commands, and assigning them first lets those bindings take their
            // first value without an extra notification round-trip.
            InitializeUpdateCheck();
            DataContext = this;
            NavCommand = new RelayCommand<string>(OnNavigate);

            // The strip is a rolling window, so it has to advance even when nothing
            // finishes: a stalled batch must visibly decay instead of looking busy.
            metricsTimer = new DispatcherTimer(
                TimeSpan.FromSeconds(2),
                DispatcherPriority.Background,
                (_, _) => Metrics.Sample(),
                Dispatcher);
            metricsTimer.Start();

            // Seed the persisted mode for a window built outside App.OnStartup (the
            // smoke tests) — deliberately silent. App.OnStartup applies the same mode
            // and that is the one call that logs; when this one logged too, every start
            // printed two lines (the guess, then the truth).
            sunIcon = (Geometry)FindResource("Glyph.Sun");
            moonIcon = (Geometry)FindResource("Glyph.Moon");
            systemIcon = (Geometry)FindResource("Glyph.ThemeSystem");
            // A System-mode shell has to keep following the OS after startup: the
            // watcher re-applies the Wpf.Ui half, and this handler brings our palette
            // along. Registered before the first apply so a watcher-driven change during
            // startup is not missed.
            Wpf.Ui.Appearance.ApplicationThemeManager.Changed += OnApplicationThemeChanged;
            ApplyStartupTheme(DesktopThemePreference.LoadMode(rootDir), log: false);

            Pool.SelectionCleared += OnPoolSelectionCleared;
            Pool.PageRefreshed += (_, _) => StatusBar.Text = Pool.SummaryText;
            RefreshPools();
            ApplySidebarCompact(false);
            Closing += OnWindowClosing;
        }

        private void OnWindowClosing(object? sender, System.ComponentModel.CancelEventArgs e)
        {
            // A stray click on the X must not abort a batch: while a tray icon is up,
            // closing hides instead. CloseToTrayPolicy owns the truth table (no tray ->
            // close, because hiding then leaves no way back; quit from the tray menu ->
            // close, that is what quit means). The cancellation below is deliberately
            // *after* this branch: it aborts in-flight work, which a hide must not do.
            if (CloseToTrayPolicy.ShouldHideToTray(trayIcon.IsAvailable, trayIcon.ExitRequested))
            {
                e.Cancel = true;
                Hide();
                trayIcon.NotifyHiddenToTray();
                return;
            }

            // Signal in-flight async operations (refresh / export / detail / payment …)
            // to abort instead of touching the UI after the window is gone. We do not
            // cancel the close itself — the window still shuts down.
            _lifetimeCts.Cancel();
            // Static event: a closed window must not stay reachable through it (and
            // must not answer an OS theme change by repainting a dead visual tree).
            Wpf.Ui.Appearance.ApplicationThemeManager.Changed -= OnApplicationThemeChanged;
            metricsTimer.Stop();
            globalHotkey.Dispose();
            trayIcon.Dispose();
        }

        /// <summary>
        /// Bring the shell back from the tray. Also the hotkey's target, so the two
        /// entry points cannot drift apart.
        /// </summary>
        public void ShowFromTray()
        {
            Show();
            if (WindowState == WindowState.Minimized) WindowState = WindowState.Normal;
            Activate();
            // Activate() alone does not pull focus from another application; the
            // Topmost flip is the documented WPF workaround and is reverted at once.
            Topmost = true;
            Topmost = false;
        }

        /// <summary>Show when hidden or minimised, hide when it is the visible window.</summary>
        public void ToggleFromHotkey()
        {
            if (IsVisible && WindowState != WindowState.Minimized)
            {
                Hide();
                return;
            }

            ShowFromTray();
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
        // Provenance from the backend's `identity` block: the exit this account was
        // registered through and the fingerprint it was bound to. Both are already
        // persisted per account (`identity_context` in the session JSON); before
        // this the detail panel had no way to show them, so the one property that
        // distinguishes two accounts was invisible in the UI that manages them.
        public string FingerprintProfile { get; set; } = "";
        public string EgressEndpoint { get; set; } = "";
        public string EgressCountry { get; set; } = "";
        public bool IdentityBound { get; set; }
        public string RegistrationStatus { get; set; } = "";
        public string Status { get; set; } = "";
        public string PayPalStatus { get; set; } = "";
        public string PayPalAmount { get; set; } = "";
        public string PromotionStatus { get; set; } = "";
        // Machine promotion state from the desktop read payload
        // (sms_tool/promotion_states.py); the label above is display copy.
        public string PromotionState { get; set; } = "";
        // 支付资格 (sms_tool/payment/qualification_states.py): the label is display
        // copy, the state is what filters/sorts key off.
        public string PaymentQualification { get; set; } = "";
        public string PaymentQualificationState { get; set; } = "";
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
        // Matches the dialog default (the "关闭 2FA" box starts unchecked). The
        // previous `= true` made every `new RegisterOptions()` silently disable
        // TOTP enrollment while the UI said otherwise.
        public bool Disable2fa { get; set; }
        public bool CheckPromotion { get; set; }
    }

    public sealed class ScanOptions
    {
        public int Workers { get; set; } = 4;
        public bool AutoRelogin { get; set; }
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
            if (s.Equals("oauth_present", StringComparison.OrdinalIgnoreCase)) return Loc.T("已获取");
            if (s.Equals("legacy_present", StringComparison.OrdinalIgnoreCase)) return Loc.T("旧Token");
            if (s.Equals("no_rt", StringComparison.OrdinalIgnoreCase)) return Loc.T("无RT");
            if (s.Equals("missing", StringComparison.OrdinalIgnoreCase)) return Loc.T("缺失");
            return s.Length > 0 ? s : "—";
        }

        public object ConvertBack(object value, Type targetType, object parameter, CultureInfo culture)
        {
            throw new NotSupportedException();
        }
    }

}
