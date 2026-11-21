using SmsWorkbench.Localization;

namespace SmsWorkbench.Views
{
    [System.Diagnostics.CodeAnalysis.SuppressMessage(
        "Design",
        "CA1001:Types that own disposable fields should be disposable",
        Justification = "WPF Window lifetime is bound to Closing/Closed events.")]
    public partial class MainWindow : FluentWindow, INotifyPropertyChanged
    {
        private Wpf.Ui.Appearance.ApplicationTheme _currentTheme = Wpf.Ui.Appearance.ApplicationTheme.Light;
        private DesktopThemeMode _themeMode = DesktopThemeMode.System;
        private bool systemThemeWatcherArmed;
        private readonly HttpClient httpClient;
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
        private readonly ITrayIconService trayIcon;
        private readonly IWorkspaceBackupService workspaceBackup;
        private readonly IGlobalHotkeyService globalHotkey;
        private readonly string rootDir;
        private readonly string configDir;
        private readonly CancellationTokenSource _lifetimeCts = new();
        private readonly AccountGridView accountGridControl;
        private readonly WorkbenchConsole consoleControl;
        private Geometry themeIconGeometry = Geometry.Empty;
        private double sidebarAnimTarget;
        private double sidebarAnimStart;
        private EventHandler? sidebarRenderingHandler;
        private Stopwatch? sidebarAnimStopwatch;
        private readonly DispatcherTimer metricsTimer;
        private Geometry sunIcon = Geometry.Empty;
        private Geometry moonIcon = Geometry.Empty;
        private Geometry systemIcon = Geometry.Empty;

        public ShellViewModel Shell { get; }

        public LogViewModel LogPanel => Shell.LogPanel;

        public TaskListViewModel TaskList => Shell.TaskList;

        public PoolViewModel Pool => Shell.Pool;

        public ExportViewModel Export => Shell.Export;

        public RegisterViewModel Register => Shell.Register;

        public MetricsViewModel Metrics => Shell.Metrics;

        public StatusBarViewModel StatusBar => Shell.StatusBar;

        public bool SidebarCollapsed
        {
            get => Shell.SidebarCollapsed;
            set => Shell.SidebarCollapsed = value;
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

        public event PropertyChangedEventHandler? PropertyChanged;

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
            IRegisterDialogService registerDialogs,
            ISettingsService settingsService,
            IDesktopEgress egress,
            Serilog.ILogger logger,
            IFileLauncher fileLauncher,
            IUpdateChecker updateChecker,
            IWorkspaceBackupService workspaceBackupService,
            ITrayIconService trayIconService,
            IGlobalHotkeyService globalHotkeyService,
            ShellViewModel? shell = null,
            PoolRefreshCoordinator? poolRefreshCoordinator = null,
            ThemeCoordinator? themeCoordinator = null,
            BackendBatchCoordinator? backendBatchCoordinator = null,
            RegistrationWorkflowCoordinator? registrationWorkflowCoordinator = null,
            AccountExportCoordinator? accountExportCoordinator = null)
        {
            Shell = shell ?? new ShellViewModel();
            poolRefresh = poolRefreshCoordinator ?? new PoolRefreshCoordinator();
            this.themeCoordinator = themeCoordinator ?? new ThemeCoordinator();
            backendBatch = backendBatchCoordinator ?? new BackendBatchCoordinator();
            registrationWorkflow = registrationWorkflowCoordinator ?? new RegistrationWorkflowCoordinator();
            this.accountExportCoordinator = accountExportCoordinator ?? new AccountExportCoordinator();

            this.backendClient = backendClient;
            this.backendTasks = backendTasks;
            this.desktopRead = desktopRead;
            this.paymentBatchDialogs = paymentBatchDialogs;
            this.proxyPoolDialogs = proxyPoolDialogs;
            this.paymentBatchService = paymentBatchService;
            this.snackbarService = snackbarService;
            this.settingsDialogs = settingsDialogs;
            this.settingsService = settingsService;
            httpClient = egress.CreateClient();
            this.logger = logger;
            this.fileLauncher = fileLauncher;
            this.updateChecker = updateChecker;
            workspaceBackup = workspaceBackupService;
            trayIcon = trayIconService;
            globalHotkey = globalHotkeyService;
            rootDir = paths.RootDirectory;
            configDir = paths.ConfigDirectory;

            this.registerDialogs = registerDialogs;
            registrationRequests = new RegistrationRequestBuilder(FindMailboxLineForRowAsync, IsCfWorkerRow);
            exportDialogs = new ExportDialogService(OpenPath, () => Path.Combine(rootDir, "runtime"));
            accountExport = new AccountExportAssembler(
                desktopRead,
                row => OnlyDigits(row.RawLine),
                row => FindMailboxLineForRowAsync(row),
                DefaultMailboxClientId,
                Log);
            smsDialogs = new SmsProviderDialogService(settingsService, desktopRead, logger, ShowThemedInfoDialog);
            accountDelete = new AccountDeleteService(backendTasks, TryDeleteFile, Log);
            accountDetail = new AccountDetailAssembler(
                desktopRead,
                row => OnlyDigits(row.RawLine),
                () => Path.Combine(rootDir, "runtime"),
                Mask,
                Log);

            Shell.SidebarCollapsedChanged += (_, collapsed) =>
            {
                OnPropertyChanged(nameof(SidebarCollapsed));
                ApplySidebarCompact(collapsed);
            };

            InitializeComponent();
            accountGridControl = (AccountGridView)(
                FindName("AccountGridControl")
                ?? throw new InvalidOperationException("MainWindow.xaml is missing x:Name=\"AccountGridControl\""));
            consoleControl = (WorkbenchConsole)(
                FindName("ConsoleControl")
                ?? throw new InvalidOperationException("MainWindow.xaml is missing x:Name=\"ConsoleControl\""));
            snackbarService.SetSnackbarPresenter(SnackbarPresenter);
            InitializeUpdateCheck();
            DataContext = this;
            NavCommand = new RelayCommand<string>(OnNavigate);

            metricsTimer = new DispatcherTimer(
                TimeSpan.FromSeconds(2),
                DispatcherPriority.Background,
                (_, _) => Metrics.Sample(),
                Dispatcher);
            metricsTimer.Start();

            sunIcon = (Geometry)FindResource("Glyph.Sun");
            moonIcon = (Geometry)FindResource("Glyph.Moon");
            systemIcon = (Geometry)FindResource("Glyph.ThemeSystem");
            Wpf.Ui.Appearance.ApplicationThemeManager.Changed += OnApplicationThemeChanged;
            ApplyStartupTheme(DesktopThemePreference.LoadMode(rootDir), log: false);

            Pool.SelectionCleared += OnPoolSelectionCleared;
            RefreshPools();
            ApplySidebarCompact(false);
            Closing += OnWindowClosing;
        }

        private void OnWindowClosing(object? sender, System.ComponentModel.CancelEventArgs e)
        {
            if (CloseToTrayPolicy.ShouldHideToTray(trayIcon.IsAvailable, trayIcon.ExitRequested))
            {
                e.Cancel = true;
                Hide();
                trayIcon.NotifyHiddenToTray();
                return;
            }

            _lifetimeCts.Cancel();
            Wpf.Ui.Appearance.ApplicationThemeManager.Changed -= OnApplicationThemeChanged;
            metricsTimer.Stop();
            globalHotkey.Dispose();
            trayIcon.Dispose();
        }

        public void ShowFromTray()
        {
            Show();
            if (WindowState == WindowState.Minimized) WindowState = WindowState.Normal;
            Activate();
            Topmost = true;
            Topmost = false;
        }

        public void ToggleFromHotkey()
        {
            if (IsVisible && WindowState != WindowState.Minimized)
            {
                Hide();
                return;
            }

            ShowFromTray();
        }
    }

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
