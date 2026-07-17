namespace SmsWorkbench
{
    public partial class MainWindow : FluentWindow, INotifyPropertyChanged
    {
        private Wpf.Ui.Appearance.ApplicationTheme _currentTheme = Wpf.Ui.Appearance.ApplicationTheme.Light;
        private static readonly HttpClient httpClient = new HttpClient();
        private static readonly ConfigComboOption[] BillingRegionOptions = new[]
        {
            new ConfigComboOption("JP", "日本 / Japan (JPY)", "Japan", "JPY"),
            new ConfigComboOption("US", "美国 / United States (USD)", "United States", "USD"),
            new ConfigComboOption("AU", "澳大利亚 / Australia (AUD)", "Australia", "AUD"),
            new ConfigComboOption("DE", "德国 / Germany (EUR)", "Germany", "EUR"),
            new ConfigComboOption("FR", "法国 / France (EUR)", "France", "EUR"),
            new ConfigComboOption("GB", "英国 / United Kingdom (GBP)", "United Kingdom", "GBP"),
            new ConfigComboOption("IN", "印度 / India (INR)", "India", "INR"),
            new ConfigComboOption("BR", "巴西 / Brazil (BRL)", "Brazil", "BRL"),
        };
        private static readonly ConfigComboOption[] LinkGenerationTypeOptions = new[]
        {
            new ConfigComboOption("hosted_long_url", "托管长链接 / Hosted Long URL", "hosted_long_url", "hosted_long_url"),
            new ConfigComboOption("paypal_direct", "PayPal 直链 / PayPal Direct", "paypal_direct", "paypal_direct"),
            new ConfigComboOption("paypal_direct_zero_due", "PayPal 直链零金额 / PayPal Direct Zero Due", "paypal_direct_zero_due", "paypal_direct_zero_due"),
        };
        private readonly string rootDir;
        private readonly ObservableCollection<PoolRow> allRows = new ObservableCollection<PoolRow>();
        private Process runningProcess;
        private int taskSeq = 1;
        private string searchText = "";
        private string countText = "1";
        private string pageSizeText = "25";
        private string proxyText = "";
        private object scopeFilter = "全部";
        private bool skipPaypalLink;
        private string logText = "";
        private string statusText = "就绪";
        private string pageStatusText = "第 0/0 页";
        private string totalCountText = "0";
        private string mailboxCountText = "0";
        private string registeredCountText = "0";
        private string paypalCountText = "0";
        private string attentionCountText = "0";
        private int currentPage = 1;
        private int filteredCount;
        private bool sidebarCollapsed;
        private string sidebarToggleGlyph = "‹";
        private Geometry sidebarToggleGeometry = Geometry.Parse("M15 18l-6-6 6-6");
        private Geometry themeIconGeometry;
        private DispatcherTimer sidebarAnimTimer;
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

        public ObservableCollection<PoolRow> PagedRows { get; } = new ObservableCollection<PoolRow>();

        public PoolRow SelectedRow { get; set; }

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
            set { pageSizeText = value ?? "25"; OnPropertyChanged(nameof(PageSizeText)); currentPage = 1; RefreshPagedRows(); }
        }

        public string ProxyText
        {
            get => proxyText;
            set { proxyText = value ?? ""; OnPropertyChanged(nameof(ProxyText)); }
        }

        public object ScopeFilter
        {
            get => scopeFilter;
            set { scopeFilter = value; OnPropertyChanged(nameof(ScopeFilter)); currentPage = 1; RefreshPagedRows(); }
        }

        public bool SkipPaypalLink
        {
            get => skipPaypalLink;
            set { skipPaypalLink = value; OnPropertyChanged(nameof(SkipPaypalLink)); }
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

        public string MailboxCountText
        {
            get => mailboxCountText;
            set { mailboxCountText = value ?? "0"; OnPropertyChanged(nameof(MailboxCountText)); }
        }

        public string RegisteredCountText
        {
            get => registeredCountText;
            set { registeredCountText = value ?? "0"; OnPropertyChanged(nameof(RegisteredCountText)); }
        }

        public string PaypalCountText
        {
            get => paypalCountText;
            set { paypalCountText = value ?? "0"; OnPropertyChanged(nameof(PaypalCountText)); }
        }

        public string AttentionCountText
        {
            get => attentionCountText;
            set { attentionCountText = value ?? "0"; OnPropertyChanged(nameof(AttentionCountText)); }
        }

        public MainWindow()
        {
            InitializeComponent();
            DataContext = this;

            // Initialize theme colors on startup
            _currentTheme = Wpf.Ui.Appearance.ApplicationThemeManager.GetAppTheme();
            ApplyCustomThemeColors(_currentTheme);
            SyncMaterialDesignTheme(_currentTheme);
            ThemeIconGeometry = _currentTheme == Wpf.Ui.Appearance.ApplicationTheme.Dark ? MoonIcon : SunIcon;

            rootDir = Directory.GetParent(AppDomain.CurrentDomain.BaseDirectory)?.FullName ?? AppDomain.CurrentDomain.BaseDirectory;
            if (Path.GetFileName(rootDir).Equals("net10", StringComparison.OrdinalIgnoreCase))
            {
                rootDir = Directory.GetParent(Directory.GetParent(rootDir)?.FullName ?? rootDir)?.FullName ?? rootDir;
            }
            if (Path.GetFileName(rootDir).Equals("dist", StringComparison.OrdinalIgnoreCase))
            {
                rootDir = Directory.GetParent(rootDir)?.FullName ?? rootDir;
            }

            ScopeFilter = "全部";
            ProxyText = ConfigString("proxy", "default");
            RefreshPools();
            ApplySidebarCompact(false);
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
        public string QuotaStatus { get; set; } = "";
        public string Quota5hUsed { get; set; } = "";
        public string Quota5hLimit { get; set; } = "";
        public string Quota5hRemaining { get; set; } = "";
        public string Quota5hPercent { get; set; } = "";
        public string Quota7dUsed { get; set; } = "";
        public string Quota7dLimit { get; set; } = "";
        public string Quota7dRemaining { get; set; } = "";
        public string Quota7dPercent { get; set; } = "";
        public string Status { get; set; } = "";
        public string PayPalStatus { get; set; } = "";
        public string PayPalAmount { get; set; } = "";
        public string RefreshTokenStatus { get; set; } = "";
        public string Phone { get; set; } = "";
        public bool HasAccessToken { get; set; }
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
    }

    public sealed class RegisterOptions
    {
        public string Source { get; set; } = "pool";
        public int Count { get; set; } = 1;
        public int Workers { get; set; } = 4;
        public string PaymentMethod { get; set; } = "paypal";
        public bool SkipPaymentLink { get; set; } = false;
    }

    public sealed class ScanOptions
    {
        public int Workers { get; set; } = 4;
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

    internal static class SqliteNative
    {
        private const int SQLITE_OK = 0;
        private const int SQLITE_ROW = 100;
        private const int SQLITE_DONE = 101;
        private const int SQLITE_OPEN_READONLY = 0x00000001;
        private const int SQLITE_OPEN_READWRITE = 0x00000002;

        [DllImport("winsqlite3", CallingConvention = CallingConvention.Cdecl)]
        private static extern int sqlite3_open_v2(byte[] filename, out IntPtr db, int flags, IntPtr vfs);

        [DllImport("winsqlite3", CallingConvention = CallingConvention.Cdecl)]
        private static extern int sqlite3_close(IntPtr db);

        [DllImport("winsqlite3", CallingConvention = CallingConvention.Cdecl)]
        private static extern int sqlite3_prepare_v2(IntPtr db, byte[] sql, int numBytes, out IntPtr stmt, IntPtr tail);

        [DllImport("winsqlite3", CallingConvention = CallingConvention.Cdecl)]
        private static extern int sqlite3_step(IntPtr stmt);

        [DllImport("winsqlite3", CallingConvention = CallingConvention.Cdecl)]
        private static extern int sqlite3_finalize(IntPtr stmt);

        [DllImport("winsqlite3", CallingConvention = CallingConvention.Cdecl)]
        private static extern int sqlite3_column_count(IntPtr stmt);

        [DllImport("winsqlite3", CallingConvention = CallingConvention.Cdecl)]
        private static extern IntPtr sqlite3_column_name(IntPtr stmt, int index);

        [DllImport("winsqlite3", CallingConvention = CallingConvention.Cdecl)]
        private static extern IntPtr sqlite3_column_text(IntPtr stmt, int index);

        [DllImport("winsqlite3", CallingConvention = CallingConvention.Cdecl)]
        private static extern int sqlite3_column_bytes(IntPtr stmt, int index);

        [DllImport("winsqlite3", CallingConvention = CallingConvention.Cdecl)]
        private static extern IntPtr sqlite3_errmsg(IntPtr db);

        public static List<Dictionary<string, string>> Query(string path, string sql)
        {
            IntPtr db = Open(path, SQLITE_OPEN_READONLY);
            try
            {
                IntPtr stmt = Prepare(db, sql);
                try
                {
                    var rows = new List<Dictionary<string, string>>();
                    int columnCount = sqlite3_column_count(stmt);
                    while (sqlite3_step(stmt) == SQLITE_ROW)
                    {
                        var row = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
                        for (int i = 0; i < columnCount; i++)
                        {
                            row[PtrToString(sqlite3_column_name(stmt, i), -1)] = ColumnText(stmt, i);
                        }
                        rows.Add(row);
                    }
                    return rows;
                }
                finally
                {
                    sqlite3_finalize(stmt);
                }
            }
            finally
            {
                sqlite3_close(db);
            }
        }

        public static void Execute(string path, string sql)
        {
            IntPtr db = Open(path, SQLITE_OPEN_READWRITE);
            try
            {
                IntPtr stmt = Prepare(db, sql);
                try
                {
                    int code = sqlite3_step(stmt);
                    if (code != SQLITE_DONE && code != SQLITE_ROW) throw new InvalidOperationException(Error(db));
                }
                finally
                {
                    sqlite3_finalize(stmt);
                }
            }
            finally
            {
                sqlite3_close(db);
            }
        }

        private static IntPtr Open(string path, int flags)
        {
            int code = sqlite3_open_v2(NullTerminatedUtf8(path), out IntPtr db, flags, IntPtr.Zero);
            if (code != SQLITE_OK) throw new InvalidOperationException(Error(db));
            return db;
        }

        private static IntPtr Prepare(IntPtr db, string sql)
        {
            int code = sqlite3_prepare_v2(db, NullTerminatedUtf8(sql), -1, out IntPtr stmt, IntPtr.Zero);
            if (code != SQLITE_OK) throw new InvalidOperationException(Error(db));
            return stmt;
        }

        private static string Error(IntPtr db) => PtrToString(sqlite3_errmsg(db), -1);

        private static string ColumnText(IntPtr stmt, int index)
        {
            int bytes = sqlite3_column_bytes(stmt, index);
            return PtrToString(sqlite3_column_text(stmt, index), bytes);
        }

        private static string PtrToString(IntPtr ptr, int bytes)
        {
            if (ptr == IntPtr.Zero) return "";
            if (bytes < 0)
            {
                int len = 0;
                while (Marshal.ReadByte(ptr, len) != 0) len++;
                bytes = len;
            }
            byte[] buffer = new byte[bytes];
            Marshal.Copy(ptr, buffer, 0, bytes);
            return Encoding.UTF8.GetString(buffer);
        }

        private static byte[] NullTerminatedUtf8(string value)
        {
            byte[] body = Encoding.UTF8.GetBytes(value ?? "");
            byte[] output = new byte[body.Length + 1];
            Buffer.BlockCopy(body, 0, output, 0, body.Length);
            return output;
        }
    }
    public sealed class CollapsedLabelConverter : IValueConverter
    {
        public object Convert(object value, Type targetType, object parameter, CultureInfo culture)
        {
            var label = parameter?.ToString() ?? string.Empty;
            return value is bool collapsed && collapsed ? string.Empty : label;
        }

        public object ConvertBack(object value, Type targetType, object parameter, CultureInfo culture)
        {
            throw new NotSupportedException();
        }
    }

    /// <summary>
    /// Converts a status string (e.g. "支付完成✅", "AT失效", "待处理") into a
    /// severity keyword ("success", "warn", "danger", "info", "neutral") that
    /// the StatusBadge style uses to pick the correct colour palette.
    /// </summary>
    public sealed class StatusSeverityConverter : IValueConverter
    {
        public object Convert(object value, Type targetType, object parameter, CultureInfo culture)
        {
            string s = (value as string ?? "").Trim();
            if (s.Length == 0) return "neutral";

            // Success states (green)
            if (s.Contains("✅") || s.Contains("完成") || s.Contains("已注册")
                || s.Contains("已获取") || s.Contains("已导入") || s.Contains("K12已进入")
                || s.Contains("PM已创建"))
                return "success";

            // Danger states (red)
            if (s.Contains("失败") || s.Contains("失效") || s.Contains("掉号")
                || s.Contains("异常") || s.Contains("无RT") || s.Contains("缺失")
                || s.Contains("未获取") || s.Contains("K12未切换") || s.Contains("K12已退出"))
                return "danger";

            // Warning states (amber)
            if (s.Contains("待") || s.Contains("缺") || s.Contains("OTP")
                || s.Contains("K12已申请") || s.Contains("旧token"))
                return "warn";

            // Info states (grey-blue)
            if (s.Contains("已保存") || s.Contains("待刷新") || s.Contains("未知"))
                return "info";

            return "neutral";
        }

        public object ConvertBack(object value, Type targetType, object parameter, CultureInfo culture)
        {
            throw new NotSupportedException();
        }
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
