using System;
using System.Collections.Generic;
using System.Collections.ObjectModel;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Net.Http;
using System.Runtime.InteropServices;
using System.Globalization;
using System.Windows.Data;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Threading;
using FluentWindow = Wpf.Ui.Controls.FluentWindow;

namespace SmsWorkbench
{
    public partial class MainWindow : FluentWindow, INotifyPropertyChanged
    {
        private Wpf.Ui.Appearance.ApplicationTheme _currentTheme = Wpf.Ui.Appearance.ApplicationTheme.Light;
        private static readonly HttpClient httpClient = new HttpClient();
        private static readonly ConfigComboOption[] SmsBowerCountryOptions = new[]
        {
            new ConfigComboOption("38", "加纳 / Ghana (+233) - 38", "Ghana", "+233"),
            new ConfigComboOption("19", "尼日利亚 / Nigeria (+234) - 19", "Nigeria", "+234"),
            new ConfigComboOption("151", "智利 / Chile (+56) - 151", "Chile", "+56"),
            new ConfigComboOption("16", "英国 / United Kingdom (+44) - 16", "United Kingdom", "+44"),
            new ConfigComboOption("6", "印度尼西亚 / Indonesia (+62) - 6", "Indonesia", "+62")
        };
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
            set { searchText = value ?? ""; OnPropertyChanged(nameof(SearchText)); currentPage = 1; RefreshPagedRows(); }
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

        private bool FilterRow(object item)
        {
            return item is PoolRow row && FilterRow(row);
        }

        private bool FilterRow(PoolRow row)
        {
            if (row == null) return false;
            string scope = DisplayText(ScopeFilter);
            string term = (SearchText ?? "").Trim().ToLowerInvariant();

            if (scope == "邮箱池" && !row.AccountType.Contains("邮箱池") && !row.AccountType.Contains("Chatai")) return false;
            if (scope == "已注册" && !row.AccountType.Contains("Session") && !row.AccountType.Contains("SQLite")) return false;
            if (scope == "待处理" && !row.Status.Contains("待") && !row.Status.Contains("缺") && !row.Status.Contains("失败")) return false;
            if (term.Length == 0) return true;

            string text = (row.Identifier + " " + row.AccountType + " " + row.Status + " " + row.Notes).ToLowerInvariant();
            return text.Contains(term);
        }

        private void RefreshPools()
        {
            allRows.Clear();
            LoadMailboxPool();
            LoadSessionPool();
            DeduplicateRows();
            currentPage = 1;
            UpdateOverview();
            RefreshPagedRows();
            StatusText = $"共 {allRows.Count} 条；当前筛选 {filteredCount} 条";
            Log("邮箱池和 session 状态已刷新。");
        }

        private void RefreshPagedRows()
        {
            if (PagedRows == null) return;
            var filtered = allRows.Where(FilterRow).ToList();
            filteredCount = filtered.Count;
            int pageSize = PageSizeValue();
            int pageCount = Math.Max(1, (int)Math.Ceiling(filteredCount / (double)pageSize));
            if (currentPage < 1) currentPage = 1;
            if (currentPage > pageCount) currentPage = pageCount;

            PagedRows.Clear();
            foreach (PoolRow row in filtered.Skip((currentPage - 1) * pageSize).Take(pageSize))
            {
                PagedRows.Add(row);
            }

            int start = filteredCount == 0 ? 0 : (currentPage - 1) * pageSize + 1;
            int end = filteredCount == 0 ? 0 : Math.Min(filteredCount, currentPage * pageSize);
            PageStatusText = $"第 {currentPage}/{pageCount} 页，显示 {start}-{end} / {filteredCount}";
            StatusText = $"共 {allRows.Count} 条；当前筛选 {filteredCount} 条";
        }

        private void UpdateOverview()
        {
            int phoneVerified = allRows.Count(IsPhoneVerifiedRow);
            int registered = allRows.Count(IsRegisteredRow);
            int paypal = allRows.Count(IsPayPalCompletedRow);
            int attention = allRows.Count(r => r.Status.Contains("待") || r.Status.Contains("缺") || r.Status.Contains("失败"));
            TotalCountText = allRows.Count.ToString();
            MailboxCountText = phoneVerified.ToString();
            RegisteredCountText = registered.ToString();
            PaypalCountText = paypal.ToString();
            AttentionCountText = attention.ToString();
        }

        private bool IsPhoneVerifiedRow(PoolRow row)
        {
            return !string.IsNullOrWhiteSpace(row.Phone);
        }

        private bool IsRegisteredRow(PoolRow row)
        {
            return row.AccountType.Contains("Session")
                || row.AccountType.Contains("SQLite")
                || row.Status.Contains("已注册")
                || row.Status.Contains("PayPal");
        }

        private bool IsPayPalCompletedRow(PoolRow row)
        {
            string status = (row.Status + " " + row.PayPalStatus).Trim();
            return status.Contains("支付完成")
                || status.Contains("Payment completed")
                || row.PayPalStatus.Equals("completed", StringComparison.OrdinalIgnoreCase);
        }

        private bool IsImportableAccountRow(PoolRow row)
        {
            if (row == null) return false;
            if (string.IsNullOrWhiteSpace(row.Identifier)) return false;
            if (row.HasAccessToken) return true;
            string status = (row.Status + " " + row.PayPalStatus).Trim();
            return status.Contains("已注册")
                || status.Contains("待支付")
                || status.Contains("支付完成")
                || status.Contains("PM已创建")
                || status.Contains("已导入")
                || status.Contains("Registered")
                || status.Contains("Payment completed");
        }

        private void DeduplicateRows()
        {
            var best = new Dictionary<string, PoolRow>(StringComparer.OrdinalIgnoreCase);
            foreach (PoolRow row in allRows.ToList())
            {
                string key = NormalizeEmailKey(row.Identifier);
                if (key.Length == 0) continue;
                if (!best.TryGetValue(key, out PoolRow existing) || RowPriority(row) > RowPriority(existing))
                {
                    best[key] = row;
                }
            }

            if (best.Count == 0) return;
            var deduped = allRows.Where(row =>
            {
                string key = NormalizeEmailKey(row.Identifier);
                return key.Length == 0 || ReferenceEquals(best[key], row);
            }).ToList();
            if (deduped.Count == allRows.Count) return;
            allRows.Clear();
            foreach (PoolRow row in deduped) allRows.Add(row);
        }

        private int RowPriority(PoolRow row)
        {
            if (row.AccountType.Contains("SQLite")) return 30;
            if (row.AccountType.Contains("Session")) return 20;
            if (row.PayPalUrl.Length > 0 || row.Status.Contains("PayPal")) return 15;
            return 10;
        }

        private string NormalizeEmailKey(string email)
        {
            string value = (email ?? "").Trim().TrimStart('\ufeff').ToLowerInvariant();
            if (value.Contains("@+"))
            {
                string[] parts = value.Split(new[] { "@+" }, StringSplitOptions.None);
                if (parts.Length == 2)
                {
                    string[] domains = { "hotmail.com", "outlook.com", "live.com", "msn.com", "gmail.com" };
                    foreach (string domain in domains)
                    {
                        if (parts[1].EndsWith(domain, StringComparison.OrdinalIgnoreCase) && parts[1].Length > domain.Length)
                        {
                            string alias = parts[1].Substring(0, parts[1].Length - domain.Length);
                            return parts[0] + "+" + alias + "@" + domain;
                        }
                    }
                }
            }
            return value;
        }

        private void LoadMailboxPool()
        {
            string tokenFile = GetMailboxTokenFile();
            LoadMailboxTokenFile(tokenFile);
            LoadChataiMailboxFile();
        }

        private void LoadChataiMailboxFile()
        {
            string path = GetChataiMailboxFilePath();
            if (string.IsNullOrWhiteSpace(path) || !File.Exists(path)) return;
            if (string.Equals(path, GetMailboxTokenFile(), StringComparison.OrdinalIgnoreCase)) return;
            LoadMailboxTokenFile(path);
        }

        private string GetChataiMailboxFilePath()
        {
            if (!string.IsNullOrWhiteSpace(chataiMailboxFilePath) && File.Exists(chataiMailboxFilePath))
                return chataiMailboxFilePath;

            string[] candidates = { "hotmail.txt", "chatai_mailbox.txt", "chatai.txt" };
            foreach (string name in candidates)
            {
                string path = Path.Combine(rootDir, name);
                if (File.Exists(path)) return path;
            }

            foreach (string path in Directory.GetFiles(rootDir, "*chatai*.txt", SearchOption.TopDirectoryOnly))
            {
                return path;
            }

            return "";
        }

        private void LoadMailboxTokenFile(string path)
        {
            if (!File.Exists(path)) return;
            string[] lines = File.ReadAllLines(path, Encoding.UTF8);
            for (int i = 0; i < lines.Length; i++)
            {
                string line = lines[i].Trim();
                if (line.Length == 0 || line.StartsWith("#")) continue;

                if (line.StartsWith("cfworker://", StringComparison.OrdinalIgnoreCase)
                    || line.EndsWith("@edu.liziai.cloud", StringComparison.OrdinalIgnoreCase))
                {
                    string email = line.StartsWith("cfworker://", StringComparison.OrdinalIgnoreCase)
                        ? line.Substring("cfworker://".Length).Trim()
                        : line;
                    allRows.Add(new PoolRow
                    {
                        Id = "M" + (i + 1),
                        CreatedAt = SafeTime(File.GetLastWriteTime(path)),
                        CompletedAt = SafeTime(File.GetLastWriteTime(path)),
                        Identifier = email,
                        AccountType = "CFWorker邮箱池",
                        Status = "可收信",
                        RefreshToken = "CFWorker",
                        Notes = path,
                        SourcePath = path,
                        RawLine = "cfworker://" + email,
                        MailboxLine = "cfworker://" + email,
                        MailboxProvider = "cfworker"
                    });
                    continue;
                }

                if (line.Contains("----"))
                {
                    string[] parts = line.Split(new[] { "----" }, 4, StringSplitOptions.None);
                    if (parts.Length < 4) continue;
                    string p2 = parts[2].Trim();
                    string p3 = parts[3].Trim();
                    string clientId = LooksMicrosoftClientId(p2) || !LooksMicrosoftClientId(p3) ? p2 : p3;
                    string refreshToken = LooksMicrosoftClientId(p2) || !LooksMicrosoftClientId(p3) ? p3 : p2;
                    allRows.Add(new PoolRow
                    {
                        Id = "M" + (i + 1),
                        CreatedAt = SafeTime(File.GetLastWriteTime(path)),
                        CompletedAt = SafeTime(File.GetLastWriteTime(path)),
                        Identifier = parts[0].Trim(),
                        AccountType = "Chatai邮箱池",
                        Status = "已授权",
                        RefreshToken = Mask(refreshToken),
                        Notes = path,
                        SourcePath = path,
                        RawLine = line,
                        ClientId = clientId,
                        RawRefreshToken = refreshToken,
                        MailboxProvider = "chatai"
                    });
                    continue;
                }

                string[] stdParts = line.Split(new[] { "---" }, StringSplitOptions.None);
                if (stdParts.Length < 3) continue;
                allRows.Add(new PoolRow
                {
                    Id = "M" + (i + 1),
                    CreatedAt = SafeTime(File.GetLastWriteTime(path)),
                    CompletedAt = SafeTime(File.GetLastWriteTime(path)),
                    Identifier = stdParts[0].Trim(),
                    AccountType = "邮箱池",
                    Status = "已授权",
                    RefreshToken = Mask(stdParts[2]),
                    Notes = path,
                    SourcePath = path,
                    RawLine = line,
                    MailboxProvider = "graph"
                });
            }
        }

        private void LoadSessionPool()
        {
            if (LoadSessionDatabase())
            {
                return;
            }
            LoadSessionJsonPool();
        }

        private bool LoadSessionDatabase()
        {
            string dbPath = GetDatabasePath();
            if (!File.Exists(dbPath)) return false;
            try
            {
                EnsureAccountExtraColumns(dbPath);
                string sql = "SELECT id,email,access_token,status,error,paypal_ok,payment_method,paypal_url,paypal_status,refresh_token_status,json_path,raw_json,pipeline_total_seconds,timing_total_seconds,created_at,updated_at FROM accounts ORDER BY updated_at DESC";
                var rows = SqliteNative.Query(dbPath, sql);
                if (rows.Count == 0) return false;
                foreach (Dictionary<string, string> data in rows)
                {
                    string status = data.TryGetValue("status", out string rawStatus) ? rawStatus : "";
                    string error = data.TryGetValue("error", out string rawError) ? rawError : "";
                    string paypalOk = data.TryGetValue("paypal_ok", out string rawPaypalOk) ? rawPaypalOk : "";
                    string paymentMethod = data.TryGetValue("payment_method", out string rawPaymentMethod) ? rawPaymentMethod : "";
                    string paypalUrl = data.TryGetValue("paypal_url", out string rawPaypalUrl) ? rawPaypalUrl : "";
                    string paypalStatus = data.TryGetValue("paypal_status", out string rawPaypalStatus) ? rawPaypalStatus : "";
                    string refreshTokenStatus = data.TryGetValue("refresh_token_status", out string rawRefreshTokenStatus) ? rawRefreshTokenStatus : "";
                    string access = data.TryGetValue("access_token", out string rawAccess) ? rawAccess : "";
                    string jsonPath = data.TryGetValue("json_path", out string rawJsonPath) ? rawJsonPath : "";
                    string rawJson = data.TryGetValue("raw_json", out string rawRawJson) ? rawRawJson : "";
                    string paypalAmount = GetPaypalAmount(rawJson);
                    string importedStatus = GetImportedStatus(rawJson);
                    string verifiedPhone = GetVerifiedPhone(rawJson);
                    if (IsPaymentLinkMethodMismatch(rawJson, paymentMethod))
                    {
                        paypalStatus = "failed";
                        paypalOk = "0";
                        paypalUrl = "";
                        paypalAmount = "";
                    }
                    TryReadMailboxFromRawJson(rawJson, out string mailboxProvider, out string mailboxClientId, out string mailboxRefreshToken, out string mailboxLine);
                    bool isCfWorkerMailbox = mailboxProvider.Equals("cfworker", StringComparison.OrdinalIgnoreCase);
                    bool isChataiMailbox = mailboxProvider.Equals("chatai", StringComparison.OrdinalIgnoreCase) || (mailboxClientId.Length > 0 && !isCfWorkerMailbox);
                    allRows.Add(new PoolRow
                    {
                        Id = "DB" + data["id"],
                        CreatedAt = UnixTimeText(data.TryGetValue("created_at", out string created) ? created : ""),
                        CompletedAt = UnixTimeText(data.TryGetValue("updated_at", out string updated) ? updated : ""),
                        Identifier = data.TryGetValue("email", out string email) ? email : "",
                        AccountType = isCfWorkerMailbox ? "SQLite/CFWorker" : isChataiMailbox ? "SQLite/Chatai" : "SQLite",
                        Status = DisplayAccountStatus(status, paypalOk, access, error, paypalStatus, refreshTokenStatus, importedStatus),
                        PayPalStatus = DisplayPayPalStatus(paypalStatus, paypalOk, paypalUrl, paymentMethod),
                        PayPalAmount = paypalAmount,
                        RefreshTokenStatus = DisplayRtStatus(refreshTokenStatus),
                        Phone = verifiedPhone,
                        HasAccessToken = !string.IsNullOrWhiteSpace(access),
                        PayPalUrl = paypalUrl,
                        RefreshToken = isCfWorkerMailbox ? "CFWorker" : Mask(isChataiMailbox ? mailboxRefreshToken : access),
                        Proxy = DbTimingText(data),
                        Notes = string.IsNullOrWhiteSpace(jsonPath) ? dbPath : jsonPath,
                        SourcePath = dbPath,
                        RawLine = data["id"],
                        ClientId = mailboxClientId,
                        RawRefreshToken = mailboxRefreshToken,
                        MailboxLine = mailboxLine,
                        MailboxProvider = mailboxProvider
                    });
                }
                Log("已从 SQLite 加载账号索引：" + dbPath);
                return true;
            }
            catch (Exception ex)
            {
                Log("读取 SQLite 失败，回退读取 JSON：" + ex.Message);
                return false;
            }
        }

        private void LoadSessionJsonPool()
        {
            var dirs = new List<string>();
            string sessionsDir = GetSessionsDir();
            if (Directory.Exists(sessionsDir)) dirs.Add(sessionsDir);
            dirs.Add(rootDir);

            foreach (string dir in dirs.Distinct(StringComparer.OrdinalIgnoreCase))
            {
                foreach (string path in Directory.GetFiles(dir, "session_*.json", SearchOption.TopDirectoryOnly))
                {
                    try
                    {
                        Dictionary<string, object> data = ReadJsonObject(path);
                        string email = GetString(data, "email");
                        string access = GetString(data, "access_token");
                        string paypalStatus = GetPaypalStatus(data);
                        string paypalUrl = GetPaypalUrl(data);
                        string paypalAmount = GetPaypalAmount(data);
                        string refreshTokenStatus = GetString(data, "refresh_token_status");
                        string importedStatus = GetImportedStatus(data);
                        string verifiedPhone = GetVerifiedPhone(data);
                        TryReadMailboxFromRawJson(JsonSerializer.Serialize(data), out string mailboxProvider, out string mailboxClientId, out string mailboxRefreshToken, out string mailboxLine);
                        string timing = GetTimingText(data);
                        allRows.Add(new PoolRow
                        {
                            Id = "S" + (allRows.Count + 1),
                            CreatedAt = SafeTime(File.GetCreationTime(path)),
                            CompletedAt = SafeTime(File.GetLastWriteTime(path)),
                            Identifier = email,
                            AccountType = mailboxProvider.Equals("cfworker", StringComparison.OrdinalIgnoreCase) ? "Session/CFWorker" : "Session",
                            Status = importedStatus.Length > 0
                                ? importedStatus
                                : DisplayAccountStatus(GetString(data, "status"), "", access, GetString(data, "error"), paypalStatus, refreshTokenStatus, importedStatus),
                            PayPalStatus = paypalStatus,
                            PayPalAmount = paypalAmount,
                            RefreshTokenStatus = DisplayRtStatus(refreshTokenStatus),
                            Phone = verifiedPhone,
                            HasAccessToken = !string.IsNullOrWhiteSpace(access),
                            PayPalUrl = paypalUrl,
                            RefreshToken = mailboxProvider.Equals("cfworker", StringComparison.OrdinalIgnoreCase) ? "CFWorker" : Mask(access),
                            Proxy = timing,
                            Notes = path,
                            SourcePath = path,
                            ClientId = mailboxClientId,
                            RawRefreshToken = mailboxRefreshToken,
                            MailboxLine = mailboxLine,
                            MailboxProvider = mailboxProvider
                        });
                    }
                    catch (Exception ex)
                    {
                        Log("读取 session 失败：" + path + " " + ex.Message);
                    }
                }
            }
        }

        private void EnsureAccountExtraColumns(string dbPath)
        {
            string[] migrations =
            {
                "ALTER TABLE accounts ADD COLUMN payment_method TEXT DEFAULT 'paypal'",
                "ALTER TABLE accounts ADD COLUMN paypal_status TEXT DEFAULT ''",
                "ALTER TABLE accounts ADD COLUMN paypal_updated_at INTEGER DEFAULT 0",
                "ALTER TABLE accounts ADD COLUMN refresh_token_status TEXT DEFAULT ''",
                "ALTER TABLE accounts ADD COLUMN refresh_token_updated_at INTEGER DEFAULT 0",
                "ALTER TABLE accounts ADD COLUMN oauth_refresh_token TEXT DEFAULT ''"
            };
            foreach (string sql in migrations)
            {
                try { SqliteNative.Execute(dbPath, sql); }
                catch { }
            }
            try
            {
                SqliteNative.Execute(dbPath, "UPDATE accounts SET paypal_status='link_ready' WHERE (paypal_status IS NULL OR paypal_status='') AND paypal_url IS NOT NULL AND paypal_url<>''");
                SqliteNative.Execute(dbPath, "UPDATE accounts SET refresh_token_status='no_rt' WHERE refresh_token_status IS NULL OR refresh_token_status=''");
            }
            catch { }
        }

        private void RegisterFromPool_Click(object sender, RoutedEventArgs e)
        {
            var args = new List<string> { "--count", CountValue().ToString(), "--workers", "4" };
            AddProxy(args);
            AddPaypalOption(args);
            RunBackend("邮箱池注册", args);
        }

        private void ImportChataiMailbox_Click(object sender, RoutedEventArgs e)
        {
            var dialog = new Microsoft.Win32.OpenFileDialog
            {
                Filter = "文本文件 (*.txt)|*.txt|所有文件 (*.*)|*.*",
                Title = "选择 Chatai 邮箱文件"
            };
            if (dialog.ShowDialog() != true) return;

            string path = dialog.FileName;
            string[] lines;
            try
            {
                lines = File.ReadAllLines(path, Encoding.UTF8);
            }
            catch (Exception ex)
            {
                MessageBox.Show("读取文件失败：" + ex.Message, "错误", MessageBoxButton.OK, MessageBoxImage.Error);
                return;
            }

            int imported = 0, skipped = 0;
            var targetFile = Path.Combine(rootDir, "hotmail.txt");
            var existingLines = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            if (File.Exists(targetFile))
            {
                foreach (string existing in File.ReadAllLines(targetFile, Encoding.UTF8))
                {
                    string trimmed = existing.Trim();
                    if (trimmed.Length > 0) existingLines.Add(trimmed);
                }
            }

            var newLines = new List<string>();
            foreach (string raw in lines)
            {
                string line = raw.Trim();
                if (line.Length == 0 || line.StartsWith("#")) continue;
                if (!line.Contains("----")) { skipped++; continue; }
                string[] parts = line.Split(new[] { "----" }, StringSplitOptions.None);
                if (parts.Length < 4) { skipped++; continue; }
                if (existingLines.Contains(line)) { skipped++; continue; }
                newLines.Add(line);
                imported++;
            }

            if (newLines.Count > 0)
            {
                File.AppendAllLines(targetFile, newLines, Encoding.UTF8);
            }

            ChataiMailboxFilePath = targetFile;
            RefreshPools();
            MessageBox.Show($"导入完成：成功 {imported} 条，跳过 {skipped} 条。", "导入结果", MessageBoxButton.OK, MessageBoxImage.Information);
        }

        private void ViewInbox_Click(object sender, RoutedEventArgs e)
        {
            PoolRow row = SelectedRow ?? (AccountGrid.SelectedItem as PoolRow);
            if (row == null)
            {
                MessageBox.Show("请先选择一条 Chatai 或 edu.liziai.cloud 邮箱记录。", "未选择邮箱", MessageBoxButton.OK, MessageBoxImage.Information);
                return;
            }
            string mailboxLine = FindMailboxLineForRow(row);
            if (!IsCfWorkerRow(row) && (string.IsNullOrWhiteSpace(row.RawRefreshToken) || string.IsNullOrWhiteSpace(row.ClientId)))
            {
                MessageBox.Show("选中记录不是 Chatai 或 edu.liziai.cloud 邮箱。", "格式不匹配", MessageBoxButton.OK, MessageBoxImage.Information);
                return;
            }
            ShowInboxDialog(row);
        }

        private void OneClickRegister_Click(object sender, RoutedEventArgs e)
        {
            if (TryCreateSelectedUnregisteredMailboxFile(out string pendingMailboxArg, out string pendingMailboxFile, out int pendingSelectedCount, out int pendingRowCount))
            {
                RegisterOptions selectedOptions = ShowSelectedRegisterOptionsDialog(pendingSelectedCount);
                if (selectedOptions == null) return;
                var pendingArgs = new List<string> { pendingMailboxArg, pendingMailboxFile, "--count", pendingSelectedCount.ToString(), "--workers", selectedOptions.Workers.ToString() };
                AddRegistrationAtOnlyArgs(pendingArgs);
                AddProxy(pendingArgs);
                AddPaypalOption(pendingArgs, selectedOptions.PaymentMethod);
                RunBackend("选中未注册邮箱注册+支付链接", pendingArgs);
                return;
            }
            if (pendingRowCount > 0)
            {
                ShowThemedInfoDialog("邮箱记录不完整", "选中的未注册邮箱缺少可用邮箱原始记录，无法直接注册。");
                return;
            }

            if (TryCreateSelectedMailboxFile(out string selectedArg, out string selectedFile, out int selectedCount))
            {
                RegisterOptions selectedOptions = ShowSelectedRegisterOptionsDialog(selectedCount);
                if (selectedOptions == null) return;
                var selectedArgs = new List<string> { selectedArg, selectedFile, "--count", selectedCount.ToString(), "--workers", selectedOptions.Workers.ToString() };
                AddRegistrationAtOnlyArgs(selectedArgs);
                AddProxy(selectedArgs);
                AddPaypalOption(selectedArgs, selectedOptions.PaymentMethod);
                RunBackend("选中邮箱注册+支付链接", selectedArgs);
                return;
            }

            RegisterOptions options = ShowRegisterOptionsDialog();
            if (options == null) return;

            if (options.Source == "phone")
            {
                var phoneArgs = new List<string>
                {
                    "--phone-register",
                    "--count",
                    options.Count.ToString(),
                };
                if (!string.IsNullOrWhiteSpace(ProxyText)) phoneArgs.AddRange(new[] { "--proxy", ProxyText.Trim() });
                AddPaypalOption(phoneArgs, options.PaymentMethod);
                RunBackend("手机号注册 (SMSBower)", phoneArgs);
                return;
            }

            if (options.Source == "cfworker")
            {
                var cfArgs = new List<string>
                {
                    "--buy-cfworker-mailbox",
                    "--cfworker-domain",
                    "edu.liziai.cloud",
                    "--count",
                    options.Count.ToString(),
                    "--workers",
                    options.Workers.ToString()
                };
                AddRegistrationAtOnlyArgs(cfArgs);
                AddProxy(cfArgs);
                AddPaypalOption(cfArgs, options.PaymentMethod);
                RunBackend("CFWorker邮箱注册+支付链接", cfArgs);
                return;
            }

            string mailboxArg = "--chatai-mailbox-file";
            string mailboxFile = GetChataiMailboxFilePath();
            int count = options.Count;
            string taskName = "一键注册+支付链接";
            if (string.IsNullOrWhiteSpace(mailboxFile) || !File.Exists(mailboxFile))
            {
                ShowThemedInfoDialog("缺少邮箱文件", "未选择邮箱，且未找到 Chatai 邮箱文件。请先导入邮箱，或勾选要注册的邮箱记录。");
                return;
            }
            var args = new List<string> { mailboxArg, mailboxFile, "--count", count.ToString(), "--workers", options.Workers.ToString() };
            AddRegistrationAtOnlyArgs(args);
            AddProxy(args);
            AddPaypalOption(args, options.PaymentMethod);
            RunBackend(taskName, args);
        }

        private void AddRegistrationAtOnlyArgs(List<string> args)
        {
            args.Add("--registration-at-only");
            args.Add("--no-phone-reuse");
        }

        private void OneClickSms_Click(object sender, RoutedEventArgs e)
        {
            var rows = SelectedRowsOrCurrent()
                .Where(r => !string.IsNullOrWhiteSpace(r.Identifier))
                .GroupBy(r => r.Identifier.Trim().ToLowerInvariant())
                .Select(g => g.First())
                .ToList();
            if (rows.Count == 0)
            {
                MessageBox.Show("请先勾选或选择要接码的邮箱账号。", "未选择账号", MessageBoxButton.OK, MessageBoxImage.Information);
                return;
            }

            var args = new List<string> { "--one-click-sms", "--workers", "1", "--refresh-timeout", "60" };
            if (rows.Count > 1)
            {
                string emailFile = Path.Combine(Path.GetTempPath(), "oneclick_sms_emails_" + DateTime.Now.ToString("yyyyMMdd_HHmmss") + ".txt");
                File.WriteAllLines(emailFile, rows.Select(r => r.Identifier.Trim()), new UTF8Encoding(false));
                args.AddRange(new[] { "--email-file", emailFile });
            }
            else
            {
                args.AddRange(new[] { "--email", rows[0].Identifier });
                AddSessionFileArg(args, rows[0]);
            }
            AddProxy(args);
            RunBackend("一键接码(" + rows.Count + ")", args);
        }

        private void OneClickScan_Click(object sender, RoutedEventArgs e)
        {
            var rows = SelectedRowsOrCurrent()
                .Where(r => !string.IsNullOrWhiteSpace(r.Identifier))
                .ToList();
            if (rows.Count == 0)
            {
                rows = allRows
                    .Where(FilterRow)
                    .Where(r => !string.IsNullOrWhiteSpace(r.Identifier))
                    .ToList();
            }
            rows = rows
                .GroupBy(r => r.Identifier.Trim().ToLowerInvariant())
                .Select(g => g.First())
                .ToList();
            if (rows.Count == 0)
            {
                ShowThemedInfoDialog("一键扫号", "没有找到可扫描的账号。请先勾选账号，或切换到包含账号的筛选范围。");
                return;
            }

            var args = new List<string> { "--one-click-scan", "--workers", Math.Min(8, Math.Max(1, rows.Count)).ToString(), "--refresh-timeout", "90" };
            if (rows.Count > 1)
            {
                string emailFile = Path.Combine(Path.GetTempPath(), "oneclick_scan_emails_" + DateTime.Now.ToString("yyyyMMdd_HHmmss") + ".txt");
                File.WriteAllLines(emailFile, rows.Select(r => r.Identifier.Trim()), new UTF8Encoding(false));
                args.AddRange(new[] { "--email-file", emailFile });
            }
            else
            {
                args.AddRange(new[] { "--email", rows[0].Identifier });
                AddSessionFileArg(args, rows[0]);
            }
            AddProxy(args);
            RunBackend("一键扫号(" + rows.Count + ")", args);
        }

        private void OneClickPay_Click(object sender, RoutedEventArgs e)
        {
            var rows = SelectedRowsOrCurrent()
                .Where(r => !string.IsNullOrWhiteSpace(r.Identifier))
                .GroupBy(r => r.Identifier.Trim().ToLowerInvariant())
                .Select(g => g.First())
                .ToList();
            if (rows.Count == 0)
            {
                ShowThemedInfoDialog("未选择账号", "请先勾选或选择要支付的账号记录。");
                return;
            }
            string paymentMethod = ShowPaymentMethodDialog("一键支付", "支付方式");
            if (paymentMethod.Length == 0) return;
            var args = new List<string> { "--one-click-pay" };
            if (rows.Count > 1)
            {
                string emailFile = Path.Combine(Path.GetTempPath(), "oneclick_emails_" + DateTime.Now.ToString("yyyyMMdd_HHmmss") + ".txt");
                File.WriteAllLines(emailFile, rows.Select(r => r.Identifier.Trim()), new UTF8Encoding(false));
                args.AddRange(new[] { "--email-file", emailFile });
            }
            else
            {
                args.AddRange(new[] { "--email", rows[0].Identifier });
            }
            args.Add("--payment-method");
            args.Add(paymentMethod);
            AddProxy(args);
            RunBackend("一键支付 (" + rows.Count + ")", args);
        }

        private void ShowThemedInfoDialog(string title, string message)
        {
            var dialog = new Window
            {
                Title = title,
                Owner = this,
                Width = 390,
                MinWidth = 340,
                SizeToContent = SizeToContent.Height,
                WindowStartupLocation = WindowStartupLocation.CenterOwner,
                Background = (System.Windows.Media.Brush)FindResource("AppBg"),
                ResizeMode = ResizeMode.NoResize
            };

            var root = new StackPanel { Margin = new Thickness(16) };
            root.Children.Add(new TextBlock
            {
                Text = title,
                FontSize = 16,
                FontWeight = FontWeights.SemiBold,
                Margin = new Thickness(0, 0, 0, 8),
                Foreground = (System.Windows.Media.Brush)FindResource("TextMain")
            });
            root.Children.Add(new TextBlock
            {
                Text = message,
                TextWrapping = TextWrapping.Wrap,
                LineHeight = 20,
                Margin = new Thickness(0, 0, 0, 16),
                Foreground = (System.Windows.Media.Brush)FindResource("TextSub")
            });
            var actions = new StackPanel { Orientation = Orientation.Horizontal, HorizontalAlignment = HorizontalAlignment.Right };
            var ok = new Button { Content = "知道了", Width = 82, Style = (Style)FindResource("PrimaryButton") };
            ok.Click += (_, __) =>
            {
                dialog.DialogResult = true;
                dialog.Close();
            };
            actions.Children.Add(ok);
            root.Children.Add(actions);
            dialog.Content = root;
            dialog.ShowDialog();
        }

        private string ShowPaymentMethodDialog(string title, string labelText = "支付方式")
        {
            var dialog = new Window
            {
                Title = title,
                Owner = this,
                Width = 360,
                Height = 170,
                MinWidth = 320,
                MinHeight = 150,
                WindowStartupLocation = WindowStartupLocation.CenterOwner,
                Background = (System.Windows.Media.Brush)FindResource("AppBg")
            };
            var root = new Grid { Margin = new Thickness(14) };
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(90) });
            root.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            var label = new TextBlock { Text = labelText, VerticalAlignment = VerticalAlignment.Center, Margin = new Thickness(0, 0, 10, 10), Foreground = (System.Windows.Media.Brush)FindResource("TextSub") };
            var box = new ComboBox { Margin = new Thickness(0, 0, 0, 10) };
            box.Items.Add(new ComboBoxItem { Content = "PayPal", Tag = "paypal" });
            box.Items.Add(new ComboBoxItem { Content = "GoPay", Tag = "gopay" });
            box.SelectedIndex = 0;
            Grid.SetRow(label, 0);
            Grid.SetColumn(label, 0);
            Grid.SetRow(box, 0);
            Grid.SetColumn(box, 1);
            root.Children.Add(label);
            root.Children.Add(box);
            var actions = new StackPanel { Orientation = Orientation.Horizontal, HorizontalAlignment = HorizontalAlignment.Right, Margin = new Thickness(0, 10, 0, 0) };
            var ok = new Button { Content = "开始", Width = 72, Style = (Style)FindResource("PrimaryButton") };
            var cancel = new Button { Content = "取消", Width = 72 };
            actions.Children.Add(ok);
            actions.Children.Add(cancel);
            Grid.SetRow(actions, 1);
            Grid.SetColumnSpan(actions, 2);
            root.Children.Add(actions);
            string selected = "";
            ok.Click += (_, __) =>
            {
                selected = NormalizePaymentMethod(((box.SelectedItem as ComboBoxItem)?.Tag as string) ?? "paypal");
                dialog.DialogResult = true;
                dialog.Close();
            };
            cancel.Click += (_, __) => { dialog.DialogResult = false; dialog.Close(); };
            dialog.Content = root;
            return dialog.ShowDialog() == true ? selected : "";
        }

        private RegisterOptions ShowSelectedRegisterOptionsDialog(int selectedCount)
        {
            var dialog = new Window
            {
                Title = "选中邮箱注册+支付链接",
                Owner = this,
                Width = 390,
                Height = 214,
                MinWidth = 350,
                MinHeight = 190,
                WindowStartupLocation = WindowStartupLocation.CenterOwner,
                Background = (System.Windows.Media.Brush)FindResource("AppBg")
            };

            var root = new Grid { Margin = new Thickness(14) };
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(110) });
            root.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });

            var hint = new TextBlock
            {
                Text = "已选择 " + Math.Max(1, selectedCount).ToString() + " 个邮箱",
                Margin = new Thickness(0, 0, 0, 10),
                Foreground = (System.Windows.Media.Brush)FindResource("TextSub")
            };
            Grid.SetRow(hint, 0);
            Grid.SetColumnSpan(hint, 2);
            root.Children.Add(hint);

            var workerLabel = new TextBlock { Text = "并发", VerticalAlignment = VerticalAlignment.Center, Margin = new Thickness(0, 0, 10, 10), Foreground = (System.Windows.Media.Brush)FindResource("TextSub") };
            var workerBox = new TextBox { Text = DefaultWorkerCount().ToString(), Margin = new Thickness(0, 0, 0, 10) };
            Grid.SetRow(workerLabel, 1);
            Grid.SetColumn(workerLabel, 0);
            Grid.SetRow(workerBox, 1);
            Grid.SetColumn(workerBox, 1);
            root.Children.Add(workerLabel);
            root.Children.Add(workerBox);

            var paymentLabel = new TextBlock { Text = "生链方式", VerticalAlignment = VerticalAlignment.Center, Margin = new Thickness(0, 0, 10, 10), Foreground = (System.Windows.Media.Brush)FindResource("TextSub") };
            var paymentBox = new ComboBox { Margin = new Thickness(0, 0, 0, 10) };
            paymentBox.Items.Add(new ComboBoxItem { Content = "PayPal 支付链接", Tag = "paypal" });
            paymentBox.Items.Add(new ComboBoxItem { Content = "GoPay 支付链接", Tag = "gopay" });
            paymentBox.SelectedIndex = 0;
            Grid.SetRow(paymentLabel, 2);
            Grid.SetColumn(paymentLabel, 0);
            Grid.SetRow(paymentBox, 2);
            Grid.SetColumn(paymentBox, 1);
            root.Children.Add(paymentLabel);
            root.Children.Add(paymentBox);

            var actions = new StackPanel { Orientation = Orientation.Horizontal, HorizontalAlignment = HorizontalAlignment.Right, Margin = new Thickness(0, 10, 0, 0) };
            var ok = new Button { Content = "开始", Width = 72, Style = (Style)FindResource("PrimaryButton") };
            var cancel = new Button { Content = "取消", Width = 72 };
            actions.Children.Add(ok);
            actions.Children.Add(cancel);
            Grid.SetRow(actions, 3);
            Grid.SetColumnSpan(actions, 2);
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.Children.Add(actions);

            RegisterOptions selected = null;
            ok.Click += (_, __) =>
            {
                selected = new RegisterOptions
                {
                    Source = "pool",
                    Count = Math.Max(1, selectedCount),
                    Workers = ParsePositiveInt(workerBox.Text, 1, 20, DefaultWorkerCount()),
                    PaymentMethod = NormalizePaymentMethod(((paymentBox.SelectedItem as ComboBoxItem)?.Tag as string) ?? "paypal")
                };
                dialog.DialogResult = true;
                dialog.Close();
            };
            cancel.Click += (_, __) => { dialog.DialogResult = false; dialog.Close(); };
            dialog.Content = root;
            return dialog.ShowDialog() == true ? selected : null;
        }

        private RegisterOptions ShowRegisterOptionsDialog()
        {
            var dialog = new Window
            {
                Title = "一键注册+支付链接",
                Owner = this,
                Width = 420,
                Height = 286,
                MinWidth = 380,
                MinHeight = 260,
                WindowStartupLocation = WindowStartupLocation.CenterOwner,
                Background = (System.Windows.Media.Brush)FindResource("AppBg")
            };

            var root = new Grid { Margin = new Thickness(14) };
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(110) });
            root.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });

            var sourceLabel = new TextBlock { Text = "注册方式", VerticalAlignment = VerticalAlignment.Center, Margin = new Thickness(0, 0, 10, 10), Foreground = (System.Windows.Media.Brush)FindResource("TextSub") };
            var sourceBox = new ComboBox { Margin = new Thickness(0, 0, 0, 10) };
            sourceBox.Items.Add(new ComboBoxItem { Content = "Chatai/邮箱池", Tag = "pool" });
            sourceBox.Items.Add(new ComboBoxItem { Content = "edu.liziai.cloud (CFWorker)", Tag = "cfworker" });
            sourceBox.Items.Add(new ComboBoxItem { Content = "📱 手机号注册 (SMSBower)", Tag = "phone" });
            sourceBox.SelectedIndex = 0;
            Grid.SetRow(sourceLabel, 0);
            Grid.SetColumn(sourceLabel, 0);
            Grid.SetRow(sourceBox, 0);
            Grid.SetColumn(sourceBox, 1);
            root.Children.Add(sourceLabel);
            root.Children.Add(sourceBox);

            var countLabel = new TextBlock { Text = "数量", VerticalAlignment = VerticalAlignment.Center, Margin = new Thickness(0, 0, 10, 10), Foreground = (System.Windows.Media.Brush)FindResource("TextSub") };
            var countBox = new TextBox { Text = CountValue().ToString(), Margin = new Thickness(0, 0, 0, 10) };
            Grid.SetRow(countLabel, 1);
            Grid.SetColumn(countLabel, 0);
            Grid.SetRow(countBox, 1);
            Grid.SetColumn(countBox, 1);
            root.Children.Add(countLabel);
            root.Children.Add(countBox);

            var workerLabel = new TextBlock { Text = "并发", VerticalAlignment = VerticalAlignment.Center, Margin = new Thickness(0, 0, 10, 10), Foreground = (System.Windows.Media.Brush)FindResource("TextSub") };
            var workerBox = new TextBox { Text = DefaultWorkerCount().ToString(), Margin = new Thickness(0, 0, 0, 10) };
            Grid.SetRow(workerLabel, 2);
            Grid.SetColumn(workerLabel, 0);
            Grid.SetRow(workerBox, 2);
            Grid.SetColumn(workerBox, 1);
            root.Children.Add(workerLabel);
            root.Children.Add(workerBox);

            var paymentLabel = new TextBlock { Text = "生链方式", VerticalAlignment = VerticalAlignment.Center, Margin = new Thickness(0, 0, 10, 10), Foreground = (System.Windows.Media.Brush)FindResource("TextSub") };
            var paymentBox = new ComboBox { Margin = new Thickness(0, 0, 0, 10) };
            paymentBox.Items.Add(new ComboBoxItem { Content = "PayPal 支付链接", Tag = "paypal" });
            paymentBox.Items.Add(new ComboBoxItem { Content = "GoPay 支付链接", Tag = "gopay" });
            paymentBox.SelectedIndex = 0;
            Grid.SetRow(paymentLabel, 3);
            Grid.SetColumn(paymentLabel, 0);
            Grid.SetRow(paymentBox, 3);
            Grid.SetColumn(paymentBox, 1);
            root.Children.Add(paymentLabel);
            root.Children.Add(paymentBox);

            var actions = new StackPanel { Orientation = Orientation.Horizontal, HorizontalAlignment = HorizontalAlignment.Right, Margin = new Thickness(0, 10, 0, 0) };
            var ok = new Button { Content = "开始", Width = 72, Style = (Style)FindResource("PrimaryButton") };
            var cancel = new Button { Content = "取消", Width = 72 };
            actions.Children.Add(ok);
            actions.Children.Add(cancel);
            Grid.SetRow(actions, 4);
            Grid.SetColumnSpan(actions, 2);
            root.Children.Add(actions);

            RegisterOptions selected = null;
            ok.Click += (_, __) =>
            {
                int count = ParsePositiveInt(countBox.Text, 1, 200, 1);
                int workers = ParsePositiveInt(workerBox.Text, 1, 20, DefaultWorkerCount());
                selected = new RegisterOptions
                {
                    Source = ((sourceBox.SelectedItem as ComboBoxItem)?.Tag as string) ?? "pool",
                    Count = count,
                    Workers = workers,
                    PaymentMethod = NormalizePaymentMethod(((paymentBox.SelectedItem as ComboBoxItem)?.Tag as string) ?? "paypal")
                };
                CountText = count.ToString();
                dialog.DialogResult = true;
                dialog.Close();
            };
            cancel.Click += (_, __) => { dialog.DialogResult = false; dialog.Close(); };
            dialog.Content = root;
            return dialog.ShowDialog() == true ? selected : null;
        }

        private int ParsePositiveInt(string text, int min, int max, int fallback)
        {
            if (!int.TryParse((text ?? "").Trim(), out int value)) return fallback;
            return Math.Max(min, Math.Min(max, value));
        }

        private int DefaultWorkerCount()
        {
            return Math.Max(1, Math.Min(8, CountValue()));
        }

        private bool TryCreateSelectedMailboxFile(out string mailboxArg, out string mailboxFile, out int selectedCount)
        {
            mailboxArg = "--chatai-mailbox-file";
            mailboxFile = "";
            selectedCount = 0;
            var lines = new List<string>();
            foreach (PoolRow row in SelectedRowsOrCurrent())
            {
                string line = (row.RawLine ?? "").Trim().TrimStart('\ufeff');
                if (MailboxArgForLine(line).Length == 0)
                {
                    line = FindMailboxLineForRow(row);
                }
                if (MailboxArgForLine(line).Length > 0)
                {
                    lines.Add(line.Trim());
                }
            }
            if (lines.Count == 0) return false;

            mailboxFile = Path.Combine(Path.GetTempPath(), "selected_mailbox_" + DateTime.Now.ToString("yyyyMMdd_HHmmss") + ".txt");
            File.WriteAllLines(mailboxFile, lines, new UTF8Encoding(false));
            selectedCount = lines.Count;
            return true;
        }

        private bool TryCreateSelectedUnregisteredMailboxFile(out string mailboxArg, out string mailboxFile, out int selectedCount, out int pendingRowCount)
        {
            mailboxArg = "--chatai-mailbox-file";
            mailboxFile = "";
            selectedCount = 0;
            pendingRowCount = 0;

            var lines = new List<string>();
            foreach (PoolRow row in SelectedRowsOrCurrent().Where(IsUnregisteredMailboxRow))
            {
                pendingRowCount++;
                string line = (row.RawLine ?? "").Trim().TrimStart('\ufeff');
                if (MailboxArgForLine(line).Length == 0)
                {
                    line = FindMailboxLineForRow(row);
                }
                if (MailboxArgForLine(line).Length > 0)
                {
                    lines.Add(line.Trim());
                }
            }
            if (lines.Count == 0) return false;

            mailboxFile = Path.Combine(Path.GetTempPath(), "selected_unregistered_mailbox_" + DateTime.Now.ToString("yyyyMMdd_HHmmss") + ".txt");
            File.WriteAllLines(mailboxFile, lines, new UTF8Encoding(false));
            selectedCount = lines.Count;
            return true;
        }

        private bool IsUnregisteredMailboxRow(PoolRow row)
        {
            if (row == null) return false;
            if (HasRegisteredAccountState(row)) return false;
            if (IsCfWorkerRow(row)) return true;
            if (!string.IsNullOrWhiteSpace(row.MailboxLine)) return true;
            if (!string.IsNullOrWhiteSpace(row.RawRefreshToken)) return true;
            if (!string.IsNullOrWhiteSpace(row.RawLine) && MailboxArgForLine(row.RawLine).Length > 0) return true;
            return !string.IsNullOrWhiteSpace(FindMailboxLineForRow(row));
        }

        private bool HasRegisteredAccountState(PoolRow row)
        {
            string status = row.Status ?? "";
            if (IsPayPalCompletedRow(row)) return true;
            return status.Contains("已注册")
                || status.Contains("PayPal")
                || status.Contains("支付完成")
                || status.Contains("已导入")
                || status.Contains("宸叉敞鍐")
                || status.Contains("鏀粯瀹屾垚")
                || status.Contains("宸插鍏");
        }

        private string MailboxArgForLine(string line)
        {
            string value = (line ?? "").Trim().TrimStart('\ufeff');
            if (value.Length == 0 || value.StartsWith("#")) return "";
            if (value.StartsWith("cfworker://", StringComparison.OrdinalIgnoreCase)
                || value.EndsWith("@edu.liziai.cloud", StringComparison.OrdinalIgnoreCase)) return "--mailbox-file";
            if (value.Contains("----") && value.Split(new[] { "----" }, StringSplitOptions.None).Length >= 4) return "--chatai-mailbox-file";
            if (value.Contains("---") && value.Split(new[] { "---" }, StringSplitOptions.None).Length >= 3) return "--mailbox-file";
            return "";
        }

        private string FindMailboxLineForRow(PoolRow row)
        {
            if (!string.IsNullOrWhiteSpace(row?.MailboxLine)) return row.MailboxLine.Trim();

            string fromDb = FindMailboxLineFromSqlite(row);
            if (fromDb.Length > 0) return fromDb;

            string email = (row.Identifier ?? "").Trim();
            if (email.Length == 0) return "";

            var paths = new List<string> { row.SourcePath, GetChataiMailboxFilePath(), GetMailboxTokenFile() };
            foreach (string path in paths.Where(p => !string.IsNullOrWhiteSpace(p)).Distinct(StringComparer.OrdinalIgnoreCase))
            {
                if (!File.Exists(path) || !path.EndsWith(".txt", StringComparison.OrdinalIgnoreCase)) continue;
                foreach (string raw in File.ReadAllLines(path, Encoding.UTF8))
                {
                    string value = raw.Trim().TrimStart('\ufeff');
                    if ((value.StartsWith(email + "----", StringComparison.OrdinalIgnoreCase)
                        || value.StartsWith(email + "---", StringComparison.OrdinalIgnoreCase))
                        && MailboxArgForLine(value).Length > 0)
                    {
                        return value;
                    }
                }
            }
            return "";
        }

        private string FindMailboxLineFromSqlite(PoolRow row)
        {
            if (row == null || string.IsNullOrWhiteSpace(row.SourcePath) || !row.SourcePath.EndsWith(".sqlite3", StringComparison.OrdinalIgnoreCase)) return "";
            try
            {
                string sql = "SELECT raw_json FROM accounts WHERE id=" + OnlyDigits(row.RawLine);
                var rows = SqliteNative.Query(row.SourcePath, sql);
                if (rows.Count == 0 || !rows[0].TryGetValue("raw_json", out string rawJson) || string.IsNullOrWhiteSpace(rawJson)) return "";

                using JsonDocument document = JsonDocument.Parse(rawJson);
                if (!document.RootElement.TryGetProperty("mailbox", out JsonElement mailbox) || mailbox.ValueKind != JsonValueKind.Object) return "";

                string email = JsonString(mailbox, "email");
                string password = JsonString(mailbox, "password");
                string refreshToken = JsonString(mailbox, "refresh_token");
                string accessToken = JsonString(mailbox, "access_token");
                string clientId = JsonStringAny(mailbox, "client_id", "clientId", "token");
                string provider = JsonString(mailbox, "provider");
                if (email.Length == 0) return "";
                if (provider.Equals("cfworker", StringComparison.OrdinalIgnoreCase))
                {
                    return "cfworker://" + email;
                }
                if (provider.Equals("chatai", StringComparison.OrdinalIgnoreCase) || clientId.Length > 0)
                {
                    if (clientId.Length == 0 || refreshToken.Length == 0) return "";
                    return email + "----" + password + "----" + clientId + "----" + refreshToken;
                }
                if (refreshToken.Length == 0) return "";
                return email + "---" + password + "---" + refreshToken + "---" + accessToken + "---0";
            }
            catch
            {
                return "";
            }
        }

        private bool TryReadMailboxFromRawJson(string rawJson, out string provider, out string clientId, out string refreshToken, out string mailboxLine)
        {
            provider = "";
            clientId = "";
            refreshToken = "";
            mailboxLine = "";
            if (string.IsNullOrWhiteSpace(rawJson)) return false;
            try
            {
                using JsonDocument document = JsonDocument.Parse(rawJson);
                if (!document.RootElement.TryGetProperty("mailbox", out JsonElement mailbox) || mailbox.ValueKind != JsonValueKind.Object) return false;

                string email = JsonString(mailbox, "email");
                string password = JsonString(mailbox, "password");
                refreshToken = JsonString(mailbox, "refresh_token");
                string accessToken = JsonString(mailbox, "access_token");
                clientId = JsonStringAny(mailbox, "client_id", "clientId", "token");
                provider = JsonString(mailbox, "provider");
                if (email.Length == 0) return false;

                if (provider.Equals("cfworker", StringComparison.OrdinalIgnoreCase))
                {
                    mailboxLine = "cfworker://" + email;
                    return true;
                }

                if (provider.Equals("chatai", StringComparison.OrdinalIgnoreCase) || clientId.Length > 0)
                {
                    if (clientId.Length == 0 || refreshToken.Length == 0) return false;
                    mailboxLine = email + "----" + password + "----" + clientId + "----" + refreshToken;
                }
                else
                {
                    if (refreshToken.Length == 0) return false;
                    mailboxLine = email + "---" + password + "---" + refreshToken + "---" + accessToken + "---0";
                }
                return true;
            }
            catch
            {
                return false;
            }
        }

        private string JsonString(JsonElement obj, string property)
        {
            return obj.TryGetProperty(property, out JsonElement value) && value.ValueKind == JsonValueKind.String
                ? value.GetString() ?? ""
                : "";
        }

        private void RerunFailed_Click(object sender, RoutedEventArgs e)
        {
            var failedRows = allRows.Where(r =>
                (r.Status.Contains("失败") || r.Status.Contains("待处理") || r.Status.Contains("缺"))
                && (r.AccountType.Contains("Chatai") || r.AccountType.Contains("邮箱池"))
                && !string.IsNullOrWhiteSpace(r.RawLine)).ToList();

            if (failedRows.Count == 0)
            {
                MessageBox.Show("没有找到需要重注册的失败账号。", "提示", MessageBoxButton.OK, MessageBoxImage.Information);
                return;
            }

            if (MessageBox.Show($"找到 {failedRows.Count} 条失败/待处理账号，确定重新注册？\n\n流程：注册→获取access token→生成支付链接→存session入库",
                "确认重注册", MessageBoxButton.YesNo, MessageBoxImage.Question) != MessageBoxResult.Yes) return;

            string tempFile = Path.Combine(Path.GetTempPath(), "rerun_failed_" + DateTime.Now.ToString("yyyyMMdd_HHmmss") + ".txt");
            var lines = new List<string>();
            foreach (PoolRow row in failedRows)
            {
                string line = row.RawLine.Trim();
                if (line.Length > 0) lines.Add(line);
            }
            File.WriteAllLines(tempFile, lines, new UTF8Encoding(false));

            var args = new List<string> { "--chatai-mailbox-file", tempFile, "--count", lines.Count.ToString(), "--workers", "4" };
            AddProxy(args);
            AddPaypalOption(args);
            RunBackend("重新注册失败账号 (" + lines.Count + ")", args);
        }

        private void RebuildSqlite_Click(object sender, RoutedEventArgs e)
        {
            var args = new List<string> { "--rebuild-sqlite" };
            RunBackend("重建SQLite索引", args);
        }

        private void AccountGrid_SelectionChanged(object sender, SelectionChangedEventArgs e)
        {
            foreach (object item in e.AddedItems)
            {
                if (item is PoolRow row) row.IsChecked = true;
            }
        }

        private void AccountDetail_Click(object sender, RoutedEventArgs e)
        {
            if (sender is FrameworkElement element && element.DataContext is PoolRow row)
            {
                ShowAccountDetail(row);
            }
        }

        private void RunBackend(string taskName, List<string> args)
        {
            if (runningProcess != null && !runningProcess.HasExited)
            {
                MessageBox.Show("已有批次正在运行，请先取消或等待完成。", "运行中", MessageBoxButton.OK, MessageBoxImage.Information);
                return;
            }
            string script = Path.Combine(rootDir, "chatgpt_phone_reg.py");
            if (!File.Exists(script))
            {
                MessageBox.Show("找不到后端脚本：" + script, "错误", MessageBoxButton.OK, MessageBoxImage.Error);
                return;
            }

            var task = new TaskRow { Name = "批次 " + taskSeq++, Task = taskName, Status = "运行中", Info = string.Join(" ", args) };
            Tasks.Add(task);
            ScrollTaskGridToBottom();
            DateTime started = DateTime.Now;

            var psi = new ProcessStartInfo
            {
                FileName = "python",
                Arguments = Quote(script) + " " + JoinArgs(args),
                WorkingDirectory = rootDir,
                UseShellExecute = false,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                CreateNoWindow = true,
                StandardOutputEncoding = Encoding.UTF8,
                StandardErrorEncoding = Encoding.UTF8
            };

            var backendOutput = new StringBuilder();
            object backendOutputLock = new object();
            void CaptureBackendLine(string line)
            {
                lock (backendOutputLock)
                {
                    backendOutput.AppendLine(line);
                }
            }

            var process = new Process { StartInfo = psi, EnableRaisingEvents = true };
            runningProcess = process;
            runningProcess.OutputDataReceived += (_, ev) =>
            {
                if (ev.Data == null) return;
                CaptureBackendLine(ev.Data);
                UiLog(ev.Data);
            };
            runningProcess.ErrorDataReceived += (_, ev) =>
            {
                if (ev.Data == null) return;
                CaptureBackendLine(ev.Data);
                UiLog(ev.Data);
            };
            runningProcess.Exited += (_, __) =>
            {
                Dispatcher.BeginInvoke(new Action(() =>
                {
                    task.Status = process.ExitCode == 0 ? "完成" : "失败";
                    task.Cost = ((int)(DateTime.Now - started).TotalSeconds).ToString();
                    task.DoneAt = SafeTime(DateTime.Now);
                    StatusText = taskName + " 已结束";
                    RefreshPools();
                    ScrollTaskGridToBottom();
                    if (taskName.StartsWith("一键扫号", StringComparison.OrdinalIgnoreCase))
                    {
                        string output;
                        lock (backendOutputLock)
                        {
                            output = backendOutput.ToString();
                        }
                        ShowAccountScanResultDialog(output);
                    }
                }), DispatcherPriority.Background);
            };

            try
            {
                Log("启动：" + psi.FileName + " " + psi.Arguments);
                runningProcess.Start();
                runningProcess.BeginOutputReadLine();
                runningProcess.BeginErrorReadLine();
                StatusText = taskName + " 运行中";
            }
            catch (Exception ex)
            {
                task.Status = "启动失败";
                Log("启动失败：" + ex.Message);
            }
        }

        private string RunBackendWithResult(string taskName, List<string> args)
        {
            string script = Path.Combine(rootDir, "chatgpt_phone_reg.py");
            if (!File.Exists(script))
                throw new FileNotFoundException("找不到后端脚本：" + script);

            var psi = new ProcessStartInfo
            {
                FileName = "python",
                Arguments = Quote(script) + " " + JoinArgs(args),
                WorkingDirectory = rootDir,
                UseShellExecute = false,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                CreateNoWindow = true,
                StandardOutputEncoding = Encoding.UTF8,
                StandardErrorEncoding = Encoding.UTF8,
            };

            var output = new StringBuilder();
            var error = new StringBuilder();

            using (var process = new Process { StartInfo = psi })
            {
                process.OutputDataReceived += (_, ev) => { if (ev.Data != null) { lock (output) output.AppendLine(ev.Data); } };
                process.ErrorDataReceived += (_, ev) => { if (ev.Data != null) { lock (error) error.AppendLine(ev.Data); } };

                Log("启动：" + psi.FileName + " " + psi.Arguments);
                process.Start();
                process.BeginOutputReadLine();
                process.BeginErrorReadLine();
                process.WaitForExit(120000); // 2 分钟超时

                if (!process.HasExited)
                {
                    try { process.Kill(); } catch { }
                    throw new TimeoutException("后端执行超时 (120s)");
                }

                string stdout;
                string stderr;
                lock (output) stdout = output.ToString().Trim();
                lock (error) stderr = error.ToString().Trim();

                // 从 stdout 中提取最后一个 JSON 块
                if (!string.IsNullOrEmpty(stdout))
                {
                    // 尝试找到最后一个 { 开始的 JSON
                    int lastBrace = stdout.LastIndexOf('{');
                    if (lastBrace >= 0)
                    {
                        string jsonPart = stdout.Substring(lastBrace);
                        if (jsonPart.Contains("}"))
                            return jsonPart;
                    }
                    return stdout;
                }

                if (!string.IsNullOrEmpty(stderr))
                    throw new Exception(stderr);

                return stdout;
            }
        }

        private void TaskGrid_Loaded(object sender, RoutedEventArgs e) => ScrollTaskGridToBottom();

        private void ScrollTaskGridToBottom()
        {
            if (TaskGrid == null || Tasks.Count == 0) return;
            Dispatcher.BeginInvoke(new Action(() =>
            {
                object last = Tasks[Tasks.Count - 1];
                TaskGrid.SelectedItem = last;
                TaskGrid.ScrollIntoView(last);
            }), DispatcherPriority.Background);
        }

        private void DeleteSelected_Click(object sender, RoutedEventArgs e)
        {
            var selected = allRows.Where(r => r.IsChecked).ToList();
            if (selected.Count == 0 && SelectedRow != null) selected.Add(SelectedRow);
            if (selected.Count == 0)
            {
                ShowThemeNoticeDialog("未选择记录", "请先勾选或选择要删除的记录。");
                return;
            }
            if (!ShowDeleteConfirmDialog(selected.Count)) return;
            foreach (PoolRow row in selected) DeleteRow(row);
            RefreshPools();
        }

        private void ShowThemeNoticeDialog(string title, string message)
        {
            var dialog = new Window
            {
                Title = title,
                Owner = this,
                Width = 420,
                Height = 190,
                MinWidth = 380,
                MinHeight = 170,
                ResizeMode = ResizeMode.NoResize,
                WindowStartupLocation = WindowStartupLocation.CenterOwner,
                Background = (Brush)FindResource("AppBg")
            };

            var root = new Grid { Margin = new Thickness(18) };
            root.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });

            var body = new StackPanel { VerticalAlignment = VerticalAlignment.Center };
            body.Children.Add(new TextBlock
            {
                Text = title,
                FontSize = 18,
                FontWeight = FontWeights.SemiBold,
                Foreground = (Brush)FindResource("TextMain"),
                Margin = new Thickness(0, 0, 0, 8)
            });
            body.Children.Add(new TextBlock
            {
                Text = message,
                TextWrapping = TextWrapping.Wrap,
                Foreground = (Brush)FindResource("TextSub")
            });
            root.Children.Add(body);

            var okButton = new Button
            {
                Content = "知道了",
                Width = 88,
                Style = (Style)FindResource("PrimaryButton"),
                HorizontalAlignment = HorizontalAlignment.Right
            };
            okButton.Click += (_, __) => dialog.Close();
            Grid.SetRow(okButton, 1);
            root.Children.Add(okButton);

            dialog.Content = root;
            dialog.ShowDialog();
        }

        private bool ShowDeleteConfirmDialog(int count)
        {
            bool confirmed = false;
            var dialog = new Window
            {
                Title = "删除记录",
                Owner = this,
                Width = 460,
                Height = 230,
                MinWidth = 420,
                MinHeight = 210,
                ResizeMode = ResizeMode.NoResize,
                WindowStartupLocation = WindowStartupLocation.CenterOwner,
                Background = (Brush)FindResource("AppBg")
            };

            var root = new Grid { Margin = new Thickness(18) };
            root.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });

            var body = new StackPanel { VerticalAlignment = VerticalAlignment.Center };
            body.Children.Add(new TextBlock
            {
                Text = "删除选中的 " + count + " 条记录？",
                FontSize = 18,
                FontWeight = FontWeights.SemiBold,
                Foreground = (Brush)FindResource("TextMain"),
                Margin = new Thickness(0, 0, 0, 8)
            });
            body.Children.Add(new TextBlock
            {
                Text = "将同步清理邮箱池、SQLite 索引和匹配的 session 文件。此操作不可撤销。",
                TextWrapping = TextWrapping.Wrap,
                Foreground = (Brush)FindResource("TextSub")
            });
            root.Children.Add(body);

            var actions = new StackPanel
            {
                Orientation = Orientation.Horizontal,
                HorizontalAlignment = HorizontalAlignment.Right
            };
            var cancelButton = new Button { Content = "取消", Width = 76 };
            cancelButton.Click += (_, __) => dialog.Close();
            var deleteButton = new Button
            {
                Content = "删除",
                Width = 76,
                Style = (Style)FindResource("DangerButton")
            };
            deleteButton.Click += (_, __) =>
            {
                confirmed = true;
                dialog.Close();
            };
            actions.Children.Add(cancelButton);
            actions.Children.Add(deleteButton);
            Grid.SetRow(actions, 1);
            root.Children.Add(actions);

            dialog.Content = root;
            dialog.ShowDialog();
            return confirmed;
        }

        private void DeleteRow(PoolRow row)
        {
            try
            {
                string emailKey = NormalizeEmailKey(row.Identifier);
                int removedPoolLines = DeleteMailboxLines(row, emailKey);
                int removedSqliteRows = DeleteSqliteAccountRows(row, emailKey);
                int removedSessionFiles = DeleteSessionJsonFiles(row, emailKey);

                if (row.SourcePath.EndsWith(".json", StringComparison.OrdinalIgnoreCase)
                    && File.Exists(row.SourcePath)
                    && IsUnderDirectory(row.SourcePath, GetSessionsDir()))
                {
                    File.Delete(row.SourcePath);
                    removedSessionFiles++;
                }

                Log("删除账号：" + row.Identifier
                    + "，邮箱池 " + removedPoolLines
                    + " 条，SQLite " + removedSqliteRows
                    + " 条，session " + removedSessionFiles + " 个");
            }
            catch (Exception ex)
            {
                Log("删除失败：" + row.Identifier + " " + ex.Message);
            }
        }

        private int DeleteMailboxLines(PoolRow row, string emailKey)
        {
            int removed = 0;
            var paths = new List<string> { row.SourcePath, GetChataiMailboxFilePath(), GetMailboxTokenFile() };
            foreach (string path in paths.Where(p => !string.IsNullOrWhiteSpace(p)).Distinct(StringComparer.OrdinalIgnoreCase))
            {
                if (!File.Exists(path) || !path.EndsWith(".txt", StringComparison.OrdinalIgnoreCase)) continue;
                string rawLine = (row.RawLine ?? "").Trim();
                var lines = File.ReadAllLines(path, Encoding.UTF8).ToList();
                int before = lines.Count;
                lines.RemoveAll(line =>
                {
                    string value = line.Trim().TrimStart('\ufeff');
                    if (rawLine.Length > 0 && value.Equals(rawLine, StringComparison.OrdinalIgnoreCase)) return true;
                    string lineEmail = MailboxEmailForLine(value);
                    return emailKey.Length > 0 && NormalizeEmailKey(lineEmail) == emailKey;
                });
                int delta = before - lines.Count;
                if (delta <= 0) continue;
                File.WriteAllLines(path, lines, new UTF8Encoding(false));
                removed += delta;
            }
            return removed;
        }

        private int DeleteSqliteAccountRows(PoolRow row, string emailKey)
        {
            string dbPath = row.SourcePath.EndsWith(".sqlite3", StringComparison.OrdinalIgnoreCase)
                ? row.SourcePath
                : GetDatabasePath();
            if (!File.Exists(dbPath)) return 0;

            var rows = SqliteNative.Query(dbPath, "SELECT id,email,json_path FROM accounts");
            var deleteIds = new List<string>();
            string explicitId = row.SourcePath.EndsWith(".sqlite3", StringComparison.OrdinalIgnoreCase) ? OnlyDigits(row.RawLine) : "";
            foreach (Dictionary<string, string> data in rows)
            {
                string id = data.TryGetValue("id", out string rawId) ? rawId : "";
                string email = data.TryGetValue("email", out string rawEmail) ? rawEmail : "";
                bool matches = explicitId.Length > 0 && id == explicitId;
                matches = matches || (emailKey.Length > 0 && NormalizeEmailKey(email) == emailKey);
                if (!matches) continue;
                deleteIds.Add(id);

                string jsonPath = data.TryGetValue("json_path", out string rawJsonPath) ? rawJsonPath : "";
                if (File.Exists(jsonPath) && IsUnderDirectory(jsonPath, GetSessionsDir()))
                {
                    TryDeleteFile(jsonPath);
                }
            }

            foreach (string id in deleteIds.Distinct())
            {
                SqliteNative.Execute(dbPath, "DELETE FROM accounts WHERE id=" + OnlyDigits(id));
            }
            return deleteIds.Distinct().Count();
        }

        private int DeleteSessionJsonFiles(PoolRow row, string emailKey)
        {
            int removed = 0;
            var dirs = new List<string> { GetSessionsDir(), rootDir };
            foreach (string dir in dirs.Where(Directory.Exists).Distinct(StringComparer.OrdinalIgnoreCase))
            {
                foreach (string path in Directory.GetFiles(dir, "session_*.json", SearchOption.TopDirectoryOnly))
                {
                    if (!SessionJsonMatchesEmail(path, emailKey)) continue;
                    if (TryDeleteFile(path)) removed++;
                }
            }
            string notes = (row.Notes ?? "").Trim();
            if (File.Exists(notes) && notes.EndsWith(".json", StringComparison.OrdinalIgnoreCase)
                && IsUnderDirectory(notes, GetSessionsDir()) && TryDeleteFile(notes))
            {
                removed++;
            }
            return removed;
        }

        private bool SessionJsonMatchesEmail(string path, string emailKey)
        {
            if (emailKey.Length == 0) return false;
            try
            {
                Dictionary<string, object> data = ReadJsonObject(path);
                return NormalizeEmailKey(GetString(data, "email")) == emailKey;
            }
            catch
            {
                return false;
            }
        }

        private string MailboxEmailForLine(string line)
        {
            string value = (line ?? "").Trim().TrimStart('\ufeff');
            if (value.Contains("----")) return value.Split(new[] { "----" }, StringSplitOptions.None).FirstOrDefault() ?? "";
            if (value.Contains("---")) return value.Split(new[] { "---" }, StringSplitOptions.None).FirstOrDefault() ?? "";
            return "";
        }

        private bool TryDeleteFile(string path)
        {
            try
            {
                if (!File.Exists(path)) return false;
                File.Delete(path);
                return true;
            }
            catch (Exception ex)
            {
                Log("删除文件失败：" + path + " " + ex.Message);
                return false;
            }
        }

        private void CancelBatch_Click(object sender, RoutedEventArgs e)
        {
            if (runningProcess == null || runningProcess.HasExited)
            {
                Log("当前没有运行中的批次。");
                return;
            }
            try
            {
                runningProcess.Kill(true);
                Log("已取消当前批次。");
            }
            catch (Exception ex)
            {
                Log("取消失败：" + ex.Message);
            }
        }

        private void Refresh_Click(object sender, RoutedEventArgs e) => RefreshPools();

        private void Settings_Click(object sender, RoutedEventArgs e) => ShowConfigDialog();

        private void ToggleTheme_Click(object sender, RoutedEventArgs e)
        {
            _currentTheme = _currentTheme == Wpf.Ui.Appearance.ApplicationTheme.Dark
                ? Wpf.Ui.Appearance.ApplicationTheme.Light
                : Wpf.Ui.Appearance.ApplicationTheme.Dark;

            Log($"切换主题被点击。新主题: {_currentTheme}");

            try
            {
                Wpf.Ui.Appearance.ApplicationThemeManager.Apply(_currentTheme, Wpf.Ui.Controls.WindowBackdropType.Mica, true);
                ApplyCustomThemeColors(_currentTheme);
                SyncMaterialDesignTheme(_currentTheme);
                ThemeIconGeometry = _currentTheme == Wpf.Ui.Appearance.ApplicationTheme.Dark ? MoonIcon : SunIcon;
                Log("主题更新应用成功。");
            }
            catch (Exception ex)
            {
                Log($"应用主题异常: {ex.Message}");
            }
        }

        private void SyncMaterialDesignTheme(Wpf.Ui.Appearance.ApplicationTheme theme)
        {
            try
            {
                var mdTheme = Application.Current.Resources.MergedDictionaries
                    .OfType<MaterialDesignThemes.Wpf.BundledTheme>()
                    .FirstOrDefault();
                if (mdTheme != null)
                {
                    mdTheme.BaseTheme = theme == Wpf.Ui.Appearance.ApplicationTheme.Dark
                        ? MaterialDesignThemes.Wpf.BaseTheme.Dark
                        : MaterialDesignThemes.Wpf.BaseTheme.Light;
                }
            }
            catch
            {
                // MaterialDesign theme sync is best-effort
            }
        }

        private void ApplyCustomThemeColors(Wpf.Ui.Appearance.ApplicationTheme theme)
        {
            if (theme == Wpf.Ui.Appearance.ApplicationTheme.Dark)
            {
                // Antigravity-like premium Dark Theme (deep navy/charcoal, neon/slate accent)
                SetBrush("AppBg", "#0F1115");
                SetBrush("PanelBg", "#161920");
                SetBrush("PanelBg2", "#1E222B");
                SetBrush("PanelHover", "#242933");
                SetBrush("Line", "#2C313D");
                SetBrush("LineStrong", "#4C5467");
                SetBrush("Primary", "#E9ECEF");
                SetBrush("PrimarySoft", "#1E222B");
                SetBrush("Danger", "#FA5252");
                SetBrush("DangerSoft", "#2B1D1D");
                SetBrush("DangerBorder", "#8C2A2A");
                SetBrush("Success", "#51CF66");
                SetBrush("SuccessSoft", "#1A2E1F");
                SetBrush("SuccessBorder", "#2B6B3A");
                SetBrush("TextMain", "#F1F3F5");
                SetBrush("TextSub", "#A9B2C3");
                SetBrush("TextMuted", "#6C7A93");
                SetBrush("SidebarBg", "#161920");
                SetBrush("GridAltBg", "#12141A");
                SetBrush("SplitterBg", "#2C313D");
                SetBrush("StatusBg", "#0F1115");
                SetBrush("LogBg", "#0A0B0E");
                SetBrush("LogBorder", "#1E222B");
                SetBrush("LogText", "#D1D6E0");

                ApplyComboBoxThemeKeys(
                    dropBg: "#161920", dropBorder: "#2C313D", glyph: "#6C7A93",
                    focused: "#4C5467", pointerOver: "#242933",
                    disabledBg: "#1E222B", disabledBorder: "#2C313D", disabledFg: "#6C7A93");
            }
            else
            {
                // Warm Premium Light Theme
                SetBrush("AppBg", "#F7F5F0");           // 247,245,240
                SetBrush("PanelBg", "#F0EEE8");          // 240,238,232
                SetBrush("PanelBg2", "#DDDAD4");         // 221,218,212
                SetBrush("PanelHover", "#E3E1DB");       // 227,225,219
                SetBrush("Line", "#DDDAD4");             // 221,218,212
                SetBrush("LineStrong", "#C5C2BA");
                SetBrush("Primary", "#3E3B36");
                SetBrush("PrimarySoft", "#E3E1DB");      // 227,225,219
                SetBrush("Danger", "#985248");           // 152,82,72  待支付/未获取
                SetBrush("DangerSoft", "#ECE2DC");       // 236,226,220
                SetBrush("DangerBorder", "#C49088");
                SetBrush("Success", "#3E846F");          // 62,132,111  支付完成/已获取
                SetBrush("SuccessSoft", "#E0F3E6");      // 224,243,230
                SetBrush("SuccessBorder", "#8DC5A9");
                SetBrush("TextMain", "#3E3B36");
                SetBrush("TextSub", "#6B6860");
                SetBrush("TextMuted", "#9E9B93");
                SetBrush("SidebarBg", "#F0EEE8");        // 240,238,232
                SetBrush("GridAltBg", "#F7F5F0");        // 247,245,240
                SetBrush("SplitterBg", "#DDDAD4");       // 221,218,212
                SetBrush("StatusBg", "#F7F5F0");         // 247,245,240
                SetBrush("LogBg", "#3E3B36");
                SetBrush("LogBorder", "#55524C");
                SetBrush("LogText", "#E3E1DB");

                ApplyComboBoxThemeKeys(
                    dropBg: "#F0EEE8", dropBorder: "#DDDAD4", glyph: "#9E9B93",
                    focused: "#C5C2BA", pointerOver: "#E3E1DB",
                    disabledBg: "#DDDAD4", disabledBorder: "#DDDAD4", disabledFg: "#9E9B93");
            }
        }

        private void ApplyComboBoxThemeKeys(string dropBg, string dropBorder, string glyph,
            string focused, string pointerOver, string disabledBg, string disabledBorder, string disabledFg)
        {
            SetBrush("ComboBoxDropDownBackground", dropBg);
            SetBrush("ComboBoxDropDownBorderBrush", dropBorder);
            SetBrush("ComboBoxDropDownGlyphForeground", glyph);
            SetBrush("ComboBoxBorderBrushFocused", focused);
            SetBrush("ComboBoxBackgroundPointerOver", pointerOver);
            SetBrush("ComboBoxBackgroundDisabled", disabledBg);
            SetBrush("ComboBoxBorderBrushDisabled", disabledBorder);
            SetBrush("ComboBoxForegroundDisabled", disabledFg);
        }

        private void SetBrush(string key, string hexColor)
        {
            var color = (System.Windows.Media.Color)System.Windows.Media.ColorConverter.ConvertFromString(hexColor);
            var brush = new System.Windows.Media.SolidColorBrush(color);
            Application.Current.Resources[key] = brush;
            this.Resources[key] = brush; // Force local window resource update
        }

        private void ToggleSidebar_Click(object sender, RoutedEventArgs e)
        {
            SidebarCollapsed = !SidebarCollapsed;
        }

        // Custom TitleBar button handlers
        private void TitleBar_MouseLeftButtonDown(object sender, System.Windows.Input.MouseButtonEventArgs e)
        {
            if (e.ClickCount == 2)
            {
                // Double-click to toggle maximize/restore
                WindowState = WindowState == WindowState.Maximized
                    ? WindowState.Normal
                    : WindowState.Maximized;
            }
            else
            {
                DragMove();
            }
        }

        private void MinimizeButton_Click(object sender, RoutedEventArgs e)
        {
            WindowState = WindowState.Minimized;
        }

        private void MaximizeButton_Click(object sender, RoutedEventArgs e)
        {
            WindowState = WindowState == WindowState.Maximized
                ? WindowState.Normal
                : WindowState.Maximized;
        }

        private void CloseButton_Click(object sender, RoutedEventArgs e)
        {
            Close();
        }

        private void ApplySidebarCompact(bool compact)
        {
            if (SidebarToggleButton != null)
            {
                SidebarToggleButton.ToolTip = compact ? "展开侧边栏" : "收起侧边栏";
            }

            SidebarToggleGlyph = compact ? "›" : "‹";
            SidebarToggleGeometry = Geometry.Parse(compact
                ? "M9 18l6-6-6-6"
                : "M15 18l-6-6 6-6");

            AnimateSidebar(compact);
        }

        private const double SidebarExpandedWidth = 272;
        private const double SidebarCollapsedWidth = 80;
        private const int SidebarAnimDurationMs = 280;

        private void AnimateSidebar(bool collapse)
        {
            double target = collapse ? SidebarCollapsedWidth : SidebarExpandedWidth;
            double current = SidebarColumn?.Width.Value ?? (collapse ? SidebarExpandedWidth : SidebarCollapsedWidth);

            sidebarAnimStart = current;
            sidebarAnimTarget = target;

            sidebarAnimTimer?.Stop();
            sidebarAnimTimer = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(16) };
            var sw = System.Diagnostics.Stopwatch.StartNew();

            sidebarAnimTimer.Tick += (_, __) =>
            {
                double elapsed = sw.Elapsed.TotalMilliseconds;
                double t = Math.Min(1.0, elapsed / SidebarAnimDurationMs);
                // Ease-out cubic for smooth deceleration
                double eased = 1 - Math.Pow(1 - t, 3);
                double value = sidebarAnimStart + (sidebarAnimTarget - sidebarAnimStart) * eased;

                if (SidebarColumn != null)
                    SidebarColumn.Width = new GridLength(value);

                if (t >= 1.0)
                {
                    sidebarAnimTimer.Stop();
                    sidebarAnimTimer = null;
                    if (SidebarColumn != null)
                        SidebarColumn.Width = new GridLength(sidebarAnimTarget);

                    // Update margin and layout after animation completes
                    if (SidebarHost != null)
                    {
                        SidebarHost.ClearValue(FrameworkElement.WidthProperty);
                        SidebarHost.Margin = collapse ? new Thickness(8, 0, 8, 10) : new Thickness(10, 0, 10, 10);
                        SidebarHost.HorizontalAlignment = HorizontalAlignment.Stretch;
                    }
                }
            };

            // Pre-set margin for target state before animation starts
            if (SidebarHost != null)
            {
                SidebarHost.Margin = collapse ? new Thickness(8, 0, 8, 10) : new Thickness(10, 0, 10, 10);
            }

            sidebarAnimTimer.Start();
        }

        private static IEnumerable<DependencyObject> FindVisualChildren(DependencyObject node)
        {
            if (node == null) yield break;
            int childCount = VisualTreeHelper.GetChildrenCount(node);
            for (int i = 0; i < childCount; i++)
            {
                DependencyObject child = VisualTreeHelper.GetChild(node, i);
                yield return child;
                foreach (DependencyObject grandChild in FindVisualChildren(child))
                {
                    yield return grandChild;
                }
            }
        }

        private void OpenSessions_Click(object sender, RoutedEventArgs e) => OpenPath(GetSessionsDir());

        private void OpenDatabase_Click(object sender, RoutedEventArgs e) => OpenPath(GetDatabasePath());

        private void OpenMailboxPool_Click(object sender, RoutedEventArgs e) => OpenPath(GetMailboxTokenFile());

        private void OpenPayPalLink_Click(object sender, RoutedEventArgs e)
        {
            PoolRow row = SelectedAccountRow();
            if (row == null) return;
            if (string.IsNullOrWhiteSpace(row.PayPalUrl))
            {
                MessageBox.Show("选中账号没有可打开的 PayPal 支付链接。", "无支付链接", MessageBoxButton.OK, MessageBoxImage.Information);
                return;
            }
            OpenPayPalUrl(row.PayPalUrl, row.Identifier);
        }

        private void RegeneratePayPalLink_Click(object sender, RoutedEventArgs e)
        {
            var rows = SelectedRowsOrCurrent()
                .Where(r => !string.IsNullOrWhiteSpace(r.Identifier))
                .GroupBy(r => r.Identifier.Trim().ToLowerInvariant())
                .Select(g => g.First())
                .ToList();
            if (rows.Count == 0)
            {
                ShowThemedInfoDialog("未选择账号", "请先勾选或选择要重新生成链接的账号记录。");
                return;
            }
            string paymentMethod = ShowPaymentMethodDialog("重新生成链接", "生链方式");
            if (paymentMethod.Length == 0) return;

            if (rows.Count == 1)
            {
                PoolRow row = rows[0];
                var singleArgs = new List<string> { "--email", row.Identifier, "--regenerate-paypal-link", "--workers", "4" };
                AddSessionFileArg(singleArgs, row);
                singleArgs.Add("--payment-method");
                singleArgs.Add(paymentMethod);
                RunBackend("重新生成支付链接", singleArgs);
                return;
            }

            string emailFile = Path.Combine(Path.GetTempPath(), "paypal_regen_emails_" + DateTime.Now.ToString("yyyyMMdd_HHmmss") + ".txt");
            File.WriteAllLines(emailFile, rows.Select(r => r.Identifier.Trim()), new UTF8Encoding(false));
            var args = new List<string> { "--regenerate-paypal-link", "--email-file", emailFile, "--workers", "4" };
            args.Add("--payment-method");
            args.Add(paymentMethod);
            RunBackend("批量重新生成支付链接 (" + rows.Count + ")", args);
        }

        private void MarkPayPalComplete_Click(object sender, RoutedEventArgs e)
        {
            MarkPayPalComplete(SelectedRowsOrCurrent());
        }

        private void MarkPayPalComplete(PoolRow row)
        {
            MarkPayPalComplete(row == null ? new List<PoolRow>() : new List<PoolRow> { row });
        }

        private void MarkPayPalComplete(List<PoolRow> rows)
        {
            rows = (rows ?? new List<PoolRow>())
                .Where(r => !string.IsNullOrWhiteSpace(r.Identifier))
                .GroupBy(r => r.Identifier.Trim().ToLowerInvariant())
                .Select(g => g.First())
                .ToList();
            if (rows.Count == 0)
            {
                MessageBox.Show("请先勾选或选择账号记录。", "未选择账号", MessageBoxButton.OK, MessageBoxImage.Information);
                return;
            }

            if (rows.Count == 1)
            {
                PoolRow row = rows[0];
                var singleArgs = new List<string> { "--email", row.Identifier, "--mark-paypal-status", "completed", "--workers", "4" };
                AddSessionFileArg(singleArgs, row);
                RunBackend("标记支付完成", singleArgs);
                return;
            }

            string emailFile = Path.Combine(Path.GetTempPath(), "paypal_completed_emails_" + DateTime.Now.ToString("yyyyMMdd_HHmmss") + ".txt");
            File.WriteAllLines(emailFile, rows.Select(r => r.Identifier.Trim()), new UTF8Encoding(false));
            var args = new List<string> { "--mark-paypal-status", "completed", "--email-file", emailFile, "--workers", "4" };
            RunBackend("批量标记支付完成 (" + rows.Count + ")", args);
        }

        private void AtExtractBaLink_Click(object sender, RoutedEventArgs e)
        {
            ShowAtExtractBaLinkDialog();
        }

        private void ShowAtExtractBaLinkDialog()
        {
            var win = new Window
            {
                Title = "AT 提取 BA 链接",
                Width = 560,
                Height = 620,
                WindowStartupLocation = WindowStartupLocation.CenterOwner,
                Owner = this,
                ResizeMode = ResizeMode.CanResize,
                Background = (System.Windows.Media.Brush)FindResource("AppBg"),
            };

            var scrollViewer = new ScrollViewer
            {
                VerticalScrollBarVisibility = ScrollBarVisibility.Auto,
                HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled,
            };
            var mainPanel = new StackPanel { Margin = new Thickness(24) };

            // 标题
            mainPanel.Children.Add(new TextBlock
            {
                Text = "输入 Access Token 提取 PayPal BA 链接",
                FontSize = 18,
                FontWeight = FontWeights.SemiBold,
                Foreground = (System.Windows.Media.Brush)FindResource("TextMain"),
                Margin = new Thickness(0, 0, 0, 16),
            });

            // AT 输入
            mainPanel.Children.Add(new TextBlock
            {
                Text = "Access Token (JWT)",
                FontSize = 13,
                Foreground = (System.Windows.Media.Brush)FindResource("TextSub"),
                Margin = new Thickness(0, 0, 0, 4),
            });
            var atBox = new TextBox
            {
                Height = 80,
                TextWrapping = TextWrapping.Wrap,
                AcceptsReturn = true,
                VerticalScrollBarVisibility = ScrollBarVisibility.Auto,
                FontFamily = new System.Windows.Media.FontFamily("Consolas"),
                FontSize = 12,
                Background = (System.Windows.Media.Brush)FindResource("PanelBg"),
                Foreground = (System.Windows.Media.Brush)FindResource("TextMain"),
                BorderBrush = (System.Windows.Media.Brush)FindResource("Line"),
                Margin = new Thickness(0, 0, 0, 12),
            };
            mainPanel.Children.Add(atBox);

            // 目标国家
            mainPanel.Children.Add(new TextBlock
            {
                Text = "目标国家",
                FontSize = 13,
                Foreground = (System.Windows.Media.Brush)FindResource("TextSub"),
                Margin = new Thickness(0, 0, 0, 4),
            });
            var countryCombo = new ComboBox
            {
                Height = 32,
                SelectedIndex = 1,
                Background = (System.Windows.Media.Brush)FindResource("PanelBg"),
                Foreground = (System.Windows.Media.Brush)FindResource("TextMain"),
                BorderBrush = (System.Windows.Media.Brush)FindResource("Line"),
                Margin = new Thickness(0, 0, 0, 12),
            };
            foreach (var c in new[] { "DE - Germany", "GB - United Kingdom", "US - United States", "AU - Australia", "JP - Japan", "FR - France", "IN - India", "BR - Brazil" })
                countryCombo.Items.Add(new ComboBoxItem { Content = c });
            mainPanel.Children.Add(countryCombo);

            // 代理配置
            mainPanel.Children.Add(new TextBlock
            {
                Text = "代理配置 (可选，留空使用配置文件)",
                FontSize = 13,
                Foreground = (System.Windows.Media.Brush)FindResource("TextSub"),
                Margin = new Thickness(0, 0, 0, 4),
            });
            var proxyBox = new TextBox
            {
                Height = 28,
                FontFamily = new System.Windows.Media.FontFamily("Consolas"),
                FontSize = 12,
                Background = (System.Windows.Media.Brush)FindResource("PanelBg"),
                Foreground = (System.Windows.Media.Brush)FindResource("TextMain"),
                BorderBrush = (System.Windows.Media.Brush)FindResource("Line"),
                Margin = new Thickness(0, 0, 0, 4),
            };
            mainPanel.Children.Add(proxyBox);

            var stageProxyPanel = new StackPanel { Margin = new Thickness(0, 0, 0, 12) };
            stageProxyPanel.Children.Add(new TextBlock
            {
                Text = "分段代理 (格式: checkout=... provider=... approve=...)",
                FontSize = 11,
                Foreground = (System.Windows.Media.Brush)FindResource("TextSub"),
                Margin = new Thickness(0, 0, 0, 2),
            });
            var stageProxyBox = new TextBox
            {
                Height = 28,
                FontFamily = new System.Windows.Media.FontFamily("Consolas"),
                FontSize = 11,
                Background = (System.Windows.Media.Brush)FindResource("PanelBg"),
                Foreground = (System.Windows.Media.Brush)FindResource("TextMain"),
                BorderBrush = (System.Windows.Media.Brush)FindResource("Line"),
            };
            stageProxyPanel.Children.Add(stageProxyBox);
            mainPanel.Children.Add(stageProxyPanel);

            // 选项
            var optionPanel = new StackPanel { Orientation = Orientation.Vertical, Margin = new Thickness(0, 0, 0, 16) };
            var zeroCheck = new CheckBox
            {
                Content = "严格要求 0 元金额 / Strict zero due",
                IsChecked = true,
                Foreground = (System.Windows.Media.Brush)FindResource("TextMain"),
                Margin = new Thickness(0, 0, 0, 6),
            };
            var requireBaCheck = new CheckBox
            {
                Content = "必须返回 PayPal BA approve URL / Require BA approve URL",
                IsChecked = true,
                Foreground = (System.Windows.Media.Brush)FindResource("TextMain"),
                Margin = new Thickness(0, 0, 0, 0),
            };
            optionPanel.Children.Add(zeroCheck);
            optionPanel.Children.Add(requireBaCheck);
            mainPanel.Children.Add(optionPanel);

            // 结果区域
            var resultBox = new TextBox
            {
                Height = 100,
                TextWrapping = TextWrapping.Wrap,
                IsReadOnly = true,
                VerticalScrollBarVisibility = ScrollBarVisibility.Auto,
                FontFamily = new System.Windows.Media.FontFamily("Consolas"),
                FontSize = 12,
                Background = (System.Windows.Media.Brush)FindResource("PanelBg"),
                Foreground = (System.Windows.Media.Brush)FindResource("TextMain"),
                BorderBrush = (System.Windows.Media.Brush)FindResource("Line"),
                Margin = new Thickness(0, 0, 0, 12),
            };
            mainPanel.Children.Add(resultBox);

            // 按钮面板
            var btnPanel = new StackPanel { Orientation = Orientation.Horizontal, HorizontalAlignment = HorizontalAlignment.Right };
            var extractBtn = new Button
            {
                Content = "提取 BA 链接",
                Height = 32,
                MinWidth = 100,
                FontWeight = FontWeights.SemiBold,
                Margin = new Thickness(0, 0, 8, 0),
            };
            var copyBtn = new Button
            {
                Content = "复制链接",
                Height = 32,
                MinWidth = 80,
                IsEnabled = false,
                Background = (System.Windows.Media.Brush)FindResource("PanelBg"),
                Foreground = (System.Windows.Media.Brush)FindResource("TextMain"),
                BorderBrush = (System.Windows.Media.Brush)FindResource("Line"),
                Margin = new Thickness(0, 0, 8, 0),
            };
            var closeBtn = new Button
            {
                Content = "关闭",
                Height = 32,
                MinWidth = 60,
                Background = (System.Windows.Media.Brush)FindResource("PanelBg"),
                Foreground = (System.Windows.Media.Brush)FindResource("TextMain"),
                BorderBrush = (System.Windows.Media.Brush)FindResource("Line"),
            };
            btnPanel.Children.Add(extractBtn);
            btnPanel.Children.Add(copyBtn);
            btnPanel.Children.Add(closeBtn);
            mainPanel.Children.Add(btnPanel);

            scrollViewer.Content = mainPanel;
            win.Content = scrollViewer;

            string lastUrl = "";

            extractBtn.Click += async (_, __) =>
            {
                string at = atBox.Text.Trim();
                if (string.IsNullOrEmpty(at))
                {
                    resultBox.Text = "请输入 Access Token";
                    return;
                }

                string country = "GB";
                if (countryCombo.SelectedItem is ComboBoxItem ci && ci.Content.ToString().Length >= 2)
                    country = ci.Content.ToString().Substring(0, 2);

                string proxy = proxyBox.Text.Trim();
                string stageProxies = stageProxyBox.Text.Trim();
                bool requireZero = zeroCheck.IsChecked == true;
                bool requireBaToken = requireBaCheck.IsChecked == true;

                resultBox.Text = "正在提取...";
                extractBtn.IsEnabled = false;
                copyBtn.IsEnabled = false;

                try
                {
                    var args = new List<string>
                    {
                        "--generate-ba-link",
                        "--at", at,
                        "--target-country", country,
                    };

                    if (!string.IsNullOrEmpty(proxy))
                        args.AddRange(new[] { "--proxy", proxy });

                    if (!string.IsNullOrEmpty(stageProxies))
                    {
                        // 解析 checkout=... provider=... approve=...
                        var parts = stageProxies.Split(new[] { ' ', ';', ',' }, StringSplitOptions.RemoveEmptyEntries);
                        foreach (var part in parts)
                        {
                            var kv = part.Split('=', 2);
                            if (kv.Length == 2)
                            {
                                string key = kv[0].Trim().ToLowerInvariant();
                                string val = kv[1].Trim();
                                if (key == "checkout" || key == "checkout-proxy")
                                    args.AddRange(new[] { "--checkout-proxy", val });
                                else if (key == "provider" || key == "provider-proxy")
                                    args.AddRange(new[] { "--provider-proxy", val });
                                else if (key == "approve" || key == "approve-proxy")
                                    args.AddRange(new[] { "--approve-proxy", val });
                            }
                        }
                    }

                    if (!requireZero)
                        args.Add("--no-require-zero");
                    if (requireBaToken)
                        args.Add("--require-ba-token");

                    var result = await Task.Run(() => RunBackendWithResult("AT 提取 BA 链接", args));
                    resultBox.Text = result;

                    // 解析 JSON 结果
                    try
                    {
                        var json = System.Text.Json.JsonDocument.Parse(result);
                        var root = json.RootElement;
                        if (root.TryGetProperty("ok", out var ok) && ok.GetBoolean())
                        {
                            if (root.TryGetProperty("url", out var url))
                            {
                                lastUrl = url.GetString() ?? "";
                                copyBtn.IsEnabled = !string.IsNullOrEmpty(lastUrl);
                                resultBox.Text = $"✅ 提取成功!\n\nURL: {lastUrl}\n\n" +
                                    (root.TryGetProperty("ba_token", out var bt) ? $"BA Token: {bt.GetString()}\n" : "") +
                                    (root.TryGetProperty("amount", out var amt) ? $"金额: {amt}" : "") +
                                    (root.TryGetProperty("currency", out var cur) ? $" {cur.GetString()}\n" : "") +
                                    (root.TryGetProperty("target_country", out var tc) ? $"目标国: {tc.GetString()}" : "");
                            }
                        }
                        else
                        {
                            if (root.TryGetProperty("error", out var err))
                                resultBox.Text = $"❌ 失败: {err.GetString()}";
                        }
                    }
                    catch
                    {
                        // 非 JSON 结果，直接显示
                    }
                }
                catch (Exception ex)
                {
                    resultBox.Text = $"❌ 异常: {ex.Message}";
                }
                finally
                {
                    extractBtn.IsEnabled = true;
                }
            };

            copyBtn.Click += (_, __) =>
            {
                if (!string.IsNullOrEmpty(lastUrl))
                {
                    System.Windows.Clipboard.SetText(lastUrl);
                    copyBtn.Content = "已复制!";
                    Task.Delay(1500).ContinueWith(_ => Dispatcher.Invoke(() => copyBtn.Content = "复制链接"));
                }
            };

            closeBtn.Click += (_, __) => win.Close();

            win.ShowDialog();
        }

        private void ImportPaidCpa_Click(object sender, RoutedEventArgs e)
        {
            string target = ShowImportTargetDialog("一键导入");
            if (target.Length == 0) return;

            var selected = SelectedRowsOrCurrent()
                .Where(IsImportableAccountRow)
                .Where(r => !string.IsNullOrWhiteSpace(r.Identifier))
                .GroupBy(r => r.Identifier.Trim().ToLowerInvariant())
                .Select(g => g.First())
                .ToList();
            var rows = selected.Count > 0
                ? selected
                : allRows.Where(IsImportableAccountRow)
                    .Where(r => !string.IsNullOrWhiteSpace(r.Identifier))
                    .GroupBy(r => r.Identifier.Trim().ToLowerInvariant())
                    .Select(g => g.First())
                    .ToList();

            if (rows.Count == 0)
            {
                MessageBox.Show("没有找到可导入账号。请先注册账号并获得 access_token/session。", "一键导入", MessageBoxButton.OK, MessageBoxImage.Information);
                return;
            }

            string emailFile = Path.Combine(Path.GetTempPath(), "oneclick_import_emails_" + DateTime.Now.ToString("yyyyMMdd_HHmmss") + ".txt");
            File.WriteAllLines(emailFile, rows.Select(r => r.Identifier.Trim()), new UTF8Encoding(false));
            var args = new List<string> { "--import-cpa", "--email-file", emailFile, "--workers", "4", "--refresh-timeout", "60" };
            AddImportTargetArg(args, target);
            RunBackend("一键导入" + ImportTargetLabel(target) + " (" + rows.Count + ")", args);
        }

        private void ExportAccounts_Click(object sender, RoutedEventArgs e)
        {
            string format = ShowExportFormatDialog();
            if (format.Length == 0) return;

            var rows = ExportCandidateRows();
            if (format.Equals("json", StringComparison.OrdinalIgnoreCase))
            {
                ExportAccountsJson(rows);
                return;
            }
            ExportAccountsTxt(rows);
        }

        private List<PoolRow> ExportCandidateRows()
        {
            var rows = SelectedRowsOrCurrent();
            if (rows.Count == 0)
            {
                rows = allRows.Where(FilterRow).ToList();
            }
            if (rows.Count == 0)
            {
                rows = allRows.ToList();
            }
            return rows;
        }

        private void ExportAccountsTxt(List<PoolRow> rows)
        {
            var lines = new List<string>();
            var seen = new HashSet<string>(StringComparer.Ordinal);
            int skipped = 0;
            foreach (PoolRow row in rows)
            {
                if (TryBuildAccountExportLine(row, out string line))
                {
                    if (seen.Add(line))
                    {
                        lines.Add(line);
                    }
                }
                else
                {
                    skipped++;
                }
            }

            if (lines.Count == 0)
            {
                ShowThemedInfoDialog("一键导出", "没有找到可导出的账号记录。仅支持包含邮箱、密码、客户端ID、刷新令牌的邮箱记录；CFWorker 或缺少密码/刷新令牌的记录会被跳过。");
                return;
            }

            string outputDir = Path.Combine(rootDir, "runtime");
            Directory.CreateDirectory(outputDir);
            string outputPath = Path.Combine(outputDir, "account-" + DateTime.Now.ToString("yyyyMMdd_HHmmss") + ".txt");
            File.WriteAllLines(outputPath, lines, new UTF8Encoding(false));
            Log("One-click export wrote " + lines.Count + " account(s), skipped " + skipped + ": " + outputPath);
            ShowExportCompleteDialog(outputPath, lines.Count, skipped, "TXT", "账号----密码----客户端ID----刷新令牌");
        }

        private void ExportAccountsJson(List<PoolRow> rows)
        {
            var items = new List<Dictionary<string, object>>();
            var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            int skipped = 0;
            foreach (PoolRow row in rows)
            {
                if (TryBuildAccountExportJson(row, out Dictionary<string, object> item))
                {
                    string key = JsonExportDedupKey(item, row);
                    if (seen.Add(key))
                    {
                        items.Add(item);
                    }
                }
                else
                {
                    skipped++;
                }
            }

            if (items.Count == 0)
            {
                ShowThemedInfoDialog("一键导出", "没有找到可导出的 JSON 账号记录。需要账号已生成 session/auth_session 或 SQLite 原始记录。");
                return;
            }

            string outputDir = Path.Combine(rootDir, "runtime", "account_json");
            Directory.CreateDirectory(outputDir);
            string outputPath = Path.Combine(outputDir, "account-" + DateTime.Now.ToString("yyyyMMdd_HHmmss") + ".json");
            object payload = items.Count == 1 ? items[0] : items;
            var options = new JsonSerializerOptions { WriteIndented = true };
            File.WriteAllText(outputPath, JsonSerializer.Serialize(payload, options), new UTF8Encoding(false));
            Log("One-click JSON export wrote " + items.Count + " account(s), skipped " + skipped + ": " + outputPath);
            ShowExportCompleteDialog(outputPath, items.Count, skipped, "JSON", "保留 RT 字段；未获取 RT 的账号默认留空");
        }

        private string ShowExportFormatDialog()
        {
            string selected = "";
            var dialog = new Window
            {
                Title = "一键导出",
                Owner = this,
                Width = 430,
                SizeToContent = SizeToContent.Height,
                ResizeMode = ResizeMode.NoResize,
                WindowStartupLocation = WindowStartupLocation.CenterOwner,
                Background = (Brush)FindResource("AppBg")
            };

            var root = new Grid { Margin = new Thickness(18) };
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });

            var header = new StackPanel { Margin = new Thickness(0, 0, 0, 16) };
            header.Children.Add(new TextBlock
            {
                Text = "选择导出格式",
                FontSize = 18,
                FontWeight = FontWeights.SemiBold,
                Foreground = (Brush)FindResource("TextMain")
            });
            header.Children.Add(new TextBlock
            {
                Text = "TXT 保持原格式；JSON 会导出账号 session 信息并保留 RT 字段。",
                TextWrapping = TextWrapping.Wrap,
                Margin = new Thickness(0, 6, 0, 0),
                Foreground = (Brush)FindResource("TextSub")
            });
            Grid.SetRow(header, 0);
            root.Children.Add(header);

            var actions = new StackPanel
            {
                Orientation = Orientation.Horizontal,
                HorizontalAlignment = HorizontalAlignment.Right
            };
            var txtButton = new Button
            {
                Content = "导出 TXT",
                Width = 96,
                Style = (Style)FindResource("PrimaryButton")
            };
            txtButton.Click += (_, __) => { selected = "txt"; dialog.Close(); };
            var jsonButton = new Button
            {
                Content = "导出 JSON",
                Width = 104,
                Margin = new Thickness(8, 0, 0, 0),
                Style = (Style)FindResource("PrimaryButton")
            };
            jsonButton.Click += (_, __) => { selected = "json"; dialog.Close(); };
            var cancelButton = new Button
            {
                Content = "取消",
                Width = 76,
                Margin = new Thickness(8, 0, 0, 0)
            };
            cancelButton.Click += (_, __) => dialog.Close();
            actions.Children.Add(txtButton);
            actions.Children.Add(jsonButton);
            actions.Children.Add(cancelButton);
            Grid.SetRow(actions, 1);
            root.Children.Add(actions);

            dialog.Content = root;
            dialog.ShowDialog();
            return selected;
        }

        private void ShowExportCompleteDialog(string outputPath, int exportedCount, int skippedCount, string formatLabel, string formatDescription)
        {
            var dialog = new Window
            {
                Title = "一键导出",
                Owner = this,
                Width = 520,
                MinWidth = 460,
                SizeToContent = SizeToContent.Height,
                ResizeMode = ResizeMode.NoResize,
                WindowStartupLocation = WindowStartupLocation.CenterOwner,
                Background = (Brush)FindResource("AppBg")
            };

            var root = new Grid { Margin = new Thickness(18) };
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });

            var header = new StackPanel { Margin = new Thickness(0, 0, 0, 14) };
            header.Children.Add(new TextBlock
            {
                Text = "导出完成",
                FontSize = 18,
                FontWeight = FontWeights.SemiBold,
                Foreground = (Brush)FindResource("TextMain")
            });
            header.Children.Add(new TextBlock
            {
                Text = "已生成账号 " + formatLabel + " 文件：" + formatDescription,
                TextWrapping = TextWrapping.Wrap,
                LineHeight = 20,
                Margin = new Thickness(0, 6, 0, 0),
                Foreground = (Brush)FindResource("TextSub")
            });
            Grid.SetRow(header, 0);
            root.Children.Add(header);

            var summary = new Border
            {
                Background = (Brush)FindResource("PanelBg"),
                BorderBrush = (Brush)FindResource("Line"),
                BorderThickness = new Thickness(1),
                CornerRadius = new CornerRadius(10),
                Padding = new Thickness(12),
                Margin = new Thickness(0, 0, 0, 16)
            };
            var summaryStack = new StackPanel();
            summaryStack.Children.Add(new TextBlock
            {
                Text = "数量：" + exportedCount + "    跳过：" + skippedCount,
                FontWeight = FontWeights.SemiBold,
                Foreground = (Brush)FindResource("TextMain")
            });
            summaryStack.Children.Add(new TextBlock
            {
                Text = outputPath,
                TextWrapping = TextWrapping.Wrap,
                Margin = new Thickness(0, 8, 0, 0),
                Foreground = (Brush)FindResource("TextSub")
            });
            summary.Child = summaryStack;
            Grid.SetRow(summary, 1);
            root.Children.Add(summary);

            var actions = new StackPanel
            {
                Orientation = Orientation.Horizontal,
                HorizontalAlignment = HorizontalAlignment.Right
            };
            var openDirButton = new Button
            {
                Content = "打开目录",
                Width = 92,
                Style = (Style)FindResource("PrimaryButton")
            };
            openDirButton.Click += (_, __) =>
            {
                string directory = Path.GetDirectoryName(outputPath) ?? Path.Combine(rootDir, "runtime");
                OpenPath(directory);
                dialog.Close();
            };
            var closeButton = new Button
            {
                Content = "关闭",
                Width = 76,
                Margin = new Thickness(8, 0, 0, 0)
            };
            closeButton.Click += (_, __) => dialog.Close();
            actions.Children.Add(openDirButton);
            actions.Children.Add(closeButton);
            Grid.SetRow(actions, 2);
            root.Children.Add(actions);

            dialog.Content = root;
            dialog.ShowDialog();
        }

        private void ShowAccountScanResultDialog(string backendOutput)
        {
            if (!TryExtractScanSummary(backendOutput, out Dictionary<string, object> summary))
            {
                ShowThemedInfoDialog("一键扫号", "扫号已结束，但未解析到结果汇总。请查看下方日志确认详情。");
                return;
            }

            var results = new List<Dictionary<string, object>>();
            if (summary.TryGetValue("results", out object rawResults) && rawResults is List<object> items)
            {
                foreach (object item in items)
                {
                    if (item is Dictionary<string, object> map)
                    {
                        results.Add(map);
                    }
                }
            }

            var rtRows = results.Where(r => BoolValue(r, "has_rt")).ToList();
            var noRtRows = results.Where(r => !BoolValue(r, "has_rt")).ToList();

            var dialog = new Window
            {
                Title = "一键扫号结果",
                Owner = this,
                Width = 720,
                MinWidth = 620,
                SizeToContent = SizeToContent.Height,
                MaxHeight = 760,
                ResizeMode = ResizeMode.CanResize,
                WindowStartupLocation = WindowStartupLocation.CenterOwner,
                Background = (Brush)FindResource("AppBg")
            };

            var root = new Grid { Margin = new Thickness(18) };
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });

            var header = new StackPanel { Margin = new Thickness(0, 0, 0, 14) };
            header.Children.Add(new TextBlock
            {
                Text = "扫号完成",
                FontSize = 18,
                FontWeight = FontWeights.SemiBold,
                Foreground = (Brush)FindResource("TextMain")
            });
            header.Children.Add(new TextBlock
            {
                Text = "总数：" + GetString(summary, "total")
                    + "    正常：" + GetString(summary, "alive")
                    + "    掉号：" + GetString(summary, "account_deactivated")
                    + "    手机验证：" + GetString(summary, "secondary_phone_verification_required")
                    + "    失败：" + GetString(summary, "failed"),
                Margin = new Thickness(0, 6, 0, 0),
                Foreground = (Brush)FindResource("TextSub")
            });
            Grid.SetRow(header, 0);
            root.Children.Add(header);

            var body = new StackPanel();
            if (noRtRows.Count > 0)
            {
                AddScanResultSection(body, "未接码号结果", noRtRows);
            }
            if (rtRows.Count > 0)
            {
                AddScanResultSection(body, "已接码号结果", rtRows);
            }
            if (body.Children.Count == 0)
            {
                body.Children.Add(new TextBlock
                {
                    Text = "没有可展示的扫描明细。",
                    Foreground = (Brush)FindResource("TextSub")
                });
            }

            var scroll = new ScrollViewer
            {
                Content = body,
                MaxHeight = 520,
                VerticalScrollBarVisibility = ScrollBarVisibility.Auto
            };
            Grid.SetRow(scroll, 1);
            root.Children.Add(scroll);

            var actions = new StackPanel
            {
                Orientation = Orientation.Horizontal,
                HorizontalAlignment = HorizontalAlignment.Right,
                Margin = new Thickness(0, 16, 0, 0)
            };
            var ok = new Button { Content = "关闭", Width = 82, Style = (Style)FindResource("PrimaryButton") };
            ok.Click += (_, __) => dialog.Close();
            actions.Children.Add(ok);
            Grid.SetRow(actions, 2);
            root.Children.Add(actions);

            dialog.Content = root;
            dialog.ShowDialog();
        }

        private void AddScanResultSection(StackPanel parent, string title, List<Dictionary<string, object>> rows)
        {
            parent.Children.Add(new TextBlock
            {
                Text = title + "（" + rows.Count + "）",
                FontSize = 15,
                FontWeight = FontWeights.SemiBold,
                Foreground = (Brush)FindResource("TextMain"),
                Margin = new Thickness(0, parent.Children.Count == 0 ? 0 : 12, 0, 8)
            });

            var card = new Border
            {
                Background = (Brush)FindResource("PanelBg"),
                BorderBrush = (Brush)FindResource("Line"),
                BorderThickness = new Thickness(1),
                CornerRadius = new CornerRadius(10),
                Padding = new Thickness(10),
                Margin = new Thickness(0, 0, 0, 4)
            };
            var stack = new StackPanel();
            foreach (Dictionary<string, object> row in rows)
            {
                string email = GetString(row, "email");
                string status = ScanStatusLabel(GetString(row, "scan_status"));
                string error = ScanResultError(row);
                stack.Children.Add(new TextBlock
                {
                    Text = error.Length > 0 ? email + "  ·  " + status + "  ·  " + error : email + "  ·  " + status,
                    TextWrapping = TextWrapping.Wrap,
                    LineHeight = 20,
                    Margin = new Thickness(0, 0, 0, 6),
                    Foreground = (Brush)FindResource("TextSub")
                });
            }
            card.Child = stack;
            parent.Children.Add(card);
        }

        private string ScanStatusLabel(string status)
        {
            string value = (status ?? "").Trim().ToLowerInvariant();
            return value switch
            {
                "alive" => "正常",
                "alive_probe_inconclusive" => "RT正常 / OAuth深度探测未完成",
                "account_deactivated" => "账号掉号",
                "secondary_phone_verification_required" => "手机验证",
                "phone_verification_required" => "支付完成",
                "scan_failed" => "扫描失败",
                _ => value.Length > 0 ? value : "未知"
            };
        }

        private string ScanResultError(Dictionary<string, object> row)
        {
            foreach (string section in new[] { "oauth", "refresh" })
            {
                if (TryGetMap(row, section, out Dictionary<string, object> map))
                {
                    string error = GetString(map, "error");
                    if (error.Length > 0) return error;
                }
            }
            return "";
        }

        private bool TryExtractScanSummary(string output, out Dictionary<string, object> summary)
        {
            summary = null;
            string text = output ?? "";
            int end = text.LastIndexOf('}');
            if (end < 0) return false;
            for (int start = text.LastIndexOf('{', end); start >= 0; start = start > 0 ? text.LastIndexOf('{', start - 1) : -1)
            {
                string candidate = text.Substring(start, end - start + 1);
                try
                {
                    var parsed = JsonTextToObject(candidate);
                    if (parsed.ContainsKey("results") && parsed.ContainsKey("total"))
                    {
                        summary = parsed;
                        return true;
                    }
                }
                catch
                {
                }
            }
            return false;
        }

        private bool BoolValue(Dictionary<string, object> data, string key)
        {
            if (data == null || !data.TryGetValue(key, out object value) || value == null) return false;
            if (value is bool b) return b;
            string text = Convert.ToString(value)?.Trim() ?? "";
            return text.Equals("true", StringComparison.OrdinalIgnoreCase) || text == "1";
        }

        private bool TryBuildAccountExportJson(PoolRow row, out Dictionary<string, object> item)
        {
            item = null;
            if (row == null) return false;
            if (!TryLoadAccountDataForRow(row, out Dictionary<string, object> data) || data.Count == 0)
            {
                return false;
            }

            Dictionary<string, object> source = data;
            if (TryGetMap(data, "auth_session", out Dictionary<string, object> authSession) && authSession.Count > 0)
            {
                source = authSession;
            }

            if (CloneExportJsonValue(source) is not Dictionary<string, object> clean || clean.Count == 0)
            {
                return false;
            }

            EnsureJsonExportEmail(clean, row);
            EnsureJsonExportRefreshToken(clean, data);
            if (IsPayPalCompletedRow(row))
            {
                SetJsonExportPlanTypePlus(clean);
            }

            item = clean;
            return true;
        }

        private bool TryLoadAccountDataForRow(PoolRow row, out Dictionary<string, object> data)
        {
            data = new Dictionary<string, object>(StringComparer.OrdinalIgnoreCase);
            if (row == null) return false;

            string source = (row.SourcePath ?? "").Trim();
            if (source.EndsWith(".sqlite3", StringComparison.OrdinalIgnoreCase) && File.Exists(source))
            {
                if (TryLoadAccountDataFromSqlite(row, out data)) return true;
            }

            var paths = new List<string> { row.Notes, row.SourcePath };
            foreach (string path in paths.Where(p => !string.IsNullOrWhiteSpace(p)).Distinct(StringComparer.OrdinalIgnoreCase))
            {
                if (!File.Exists(path) || !path.EndsWith(".json", StringComparison.OrdinalIgnoreCase)) continue;
                try
                {
                    data = ReadJsonObject(path);
                    return data.Count > 0;
                }
                catch
                {
                }
            }
            return false;
        }

        private bool TryLoadAccountDataFromSqlite(PoolRow row, out Dictionary<string, object> data)
        {
            data = new Dictionary<string, object>(StringComparer.OrdinalIgnoreCase);
            try
            {
                string id = OnlyDigits(row.RawLine);
                string sql;
                if (id.Length > 0)
                {
                    sql = "SELECT raw_json,json_path FROM accounts WHERE id=" + id + " LIMIT 1";
                }
                else
                {
                    string email = SqlLiteral((row.Identifier ?? "").Trim());
                    if (email.Length == 0) return false;
                    sql = "SELECT raw_json,json_path FROM accounts WHERE lower(email)=lower('" + email + "') ORDER BY updated_at DESC LIMIT 1";
                }

                var rows = SqliteNative.Query(row.SourcePath, sql);
                if (rows.Count == 0) return false;
                string rawJson = rows[0].TryGetValue("raw_json", out string raw) ? raw : "";
                string jsonPath = rows[0].TryGetValue("json_path", out string jp) ? jp : "";

                if (!string.IsNullOrWhiteSpace(jsonPath) && File.Exists(jsonPath) && jsonPath.EndsWith(".json", StringComparison.OrdinalIgnoreCase))
                {
                    try
                    {
                        MergeJsonObject(data, ReadJsonObject(jsonPath));
                    }
                    catch
                    {
                    }
                }
                if (!string.IsNullOrWhiteSpace(rawJson))
                {
                    MergeJsonObject(data, JsonTextToObject(rawJson));
                }
                return data.Count > 0;
            }
            catch
            {
                data = new Dictionary<string, object>(StringComparer.OrdinalIgnoreCase);
                return false;
            }
        }

        private void MergeJsonObject(Dictionary<string, object> target, Dictionary<string, object> source)
        {
            if (target == null || source == null) return;
            foreach (var pair in source)
            {
                target[pair.Key] = pair.Value;
            }
        }

        private string SqlLiteral(string value)
        {
            return (value ?? "").Replace("'", "''");
        }

        private object CloneExportJsonValue(object value)
        {
            if (value is Dictionary<string, object> map)
            {
                var clean = new Dictionary<string, object>(StringComparer.OrdinalIgnoreCase);
                foreach (var pair in map)
                {
                    clean[pair.Key] = CloneExportJsonValue(pair.Value);
                }
                return clean;
            }
            if (value is List<object> list)
            {
                return list.Select(CloneExportJsonValue).ToList();
            }
            return value;
        }

        private void EnsureJsonExportEmail(Dictionary<string, object> item, PoolRow row)
        {
            string email = (row?.Identifier ?? "").Trim();
            if (email.Length == 0) return;
            if (!TryGetMap(item, "user", out Dictionary<string, object> user))
            {
                user = new Dictionary<string, object>(StringComparer.OrdinalIgnoreCase);
                item["user"] = user;
            }
            if (GetString(user, "email").Length == 0)
            {
                user["email"] = email;
            }
        }

        private void EnsureJsonExportRefreshToken(Dictionary<string, object> item, Dictionary<string, object> sourceData)
        {
            string rt = FirstJsonString(
                GetString(sourceData, "oauth_refresh_token"),
                GetString(sourceData, "refresh_token"),
                NestedJsonString(sourceData, "codex_session", "refresh_token"),
                NestedJsonString(sourceData, "token", "refresh_token"),
                NestedJsonString(sourceData, "credentials", "refresh_token")
            );
            item["refresh_token"] = rt;
            if (GetString(item, "oauth_refresh_token").Length == 0 && rt.Length > 0)
            {
                item["oauth_refresh_token"] = rt;
            }
        }

        private string NestedJsonString(Dictionary<string, object> data, string section, string key)
        {
            return TryGetMap(data, section, out Dictionary<string, object> map) ? GetString(map, key) : "";
        }

        private string FirstJsonString(params string[] values)
        {
            foreach (string value in values)
            {
                string text = (value ?? "").Trim();
                if (text.Length > 0) return text;
            }
            return "";
        }

        private void SetJsonExportPlanTypePlus(Dictionary<string, object> item)
        {
            if (item.ContainsKey("planType"))
            {
                item["planType"] = "plus";
            }
            if (!TryGetMap(item, "account", out Dictionary<string, object> account))
            {
                account = new Dictionary<string, object>(StringComparer.OrdinalIgnoreCase);
                item["account"] = account;
            }
            account["planType"] = "plus";
        }

        private string JsonExportDedupKey(Dictionary<string, object> item, PoolRow row)
        {
            if (TryGetMap(item, "user", out Dictionary<string, object> user))
            {
                string userEmail = GetString(user, "email").Trim();
                if (userEmail.Length > 0) return userEmail.ToLowerInvariant();
            }
            string email = GetString(item, "email").Trim();
            if (email.Length > 0) return email.ToLowerInvariant();
            email = (row?.Identifier ?? "").Trim();
            if (email.Length > 0) return email.ToLowerInvariant();
            return JsonSerializer.Serialize(item);
        }

        private bool TryBuildAccountExportLine(PoolRow row, out string line)
        {
            line = "";
            if (row == null) return false;

            string source = FindMailboxLineForRow(row);
            if (source.Length == 0 && !string.IsNullOrWhiteSpace(row.RawLine))
            {
                source = row.RawLine;
            }

            if (!TryParseMailboxExportParts(source, row, out string email, out string password, out string clientId, out string refreshToken))
            {
                return false;
            }

            if (email.Length == 0 || password.Length == 0 || clientId.Length == 0 || refreshToken.Length == 0)
            {
                return false;
            }

            line = email + "----" + password + "----" + clientId + "----" + refreshToken;
            return true;
        }

        private bool TryParseMailboxExportParts(string source, PoolRow row, out string email, out string password, out string clientId, out string refreshToken)
        {
            email = "";
            password = "";
            clientId = "";
            refreshToken = "";

            string value = (source ?? "").Trim().TrimStart('\ufeff');
            if (value.Length == 0 || value.StartsWith("#")) return false;
            if (value.StartsWith("cfworker://", StringComparison.OrdinalIgnoreCase)
                || value.EndsWith("@edu.liziai.cloud", StringComparison.OrdinalIgnoreCase))
            {
                return false;
            }

            if (value.Contains("----"))
            {
                string[] parts = value.Split(new[] { "----" }, StringSplitOptions.None);
                if (parts.Length < 4) return false;
                email = parts[0].Trim();
                password = parts[1].Trim();
                string p2 = parts[2].Trim();
                string p3 = string.Join("----", parts.Skip(3)).Trim();
                clientId = LooksMicrosoftClientId(p2) || !LooksMicrosoftClientId(p3) ? p2 : p3;
                refreshToken = LooksMicrosoftClientId(p2) || !LooksMicrosoftClientId(p3) ? p3 : p2;
                return true;
            }

            if (value.Contains("---"))
            {
                string[] parts = value.Split(new[] { "---" }, StringSplitOptions.None);
                if (parts.Length < 3) return false;
                email = parts[0].Trim();
                password = parts[1].Trim();
                clientId = !string.IsNullOrWhiteSpace(row?.ClientId) ? row.ClientId.Trim() : DefaultMailboxClientId();
                refreshToken = parts[2].Trim();
                return true;
            }

            return false;
        }

        private bool LooksMicrosoftClientId(string value)
        {
            return Regex.IsMatch((value ?? "").Trim(), "^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$");
        }

        private string DefaultMailboxClientId()
        {
            string configured = ConfigString("email_registration", "oauth_client_id").Trim();
            return configured.Length > 0 ? configured : "9e5f94bc-e8a4-4e73-b8be-63364c29d753";
        }

        private string ShowImportTargetDialog(string title)
        {
            string selected = "";
            var dialog = new Window
            {
                Title = title,
                Owner = this,
                Width = 360,
                Height = 190,
                ResizeMode = ResizeMode.NoResize,
                WindowStartupLocation = WindowStartupLocation.CenterOwner,
                Background = (System.Windows.Media.Brush)FindResource("AppBg")
            };

            var root = new Grid { Margin = new Thickness(18) };
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });

            var label = new TextBlock
            {
                Text = "选择导入目标",
                Foreground = (System.Windows.Media.Brush)FindResource("TextMain"),
                FontWeight = FontWeights.SemiBold,
                Margin = new Thickness(0, 0, 0, 10)
            };
            Grid.SetRow(label, 0);
            root.Children.Add(label);

            var combo = new ComboBox { SelectedIndex = 0, Margin = new Thickness(0, 0, 0, 18) };
            combo.Items.Add(new ComboBoxItem { Content = "CPA", Tag = "cpa" });
            Grid.SetRow(combo, 1);
            root.Children.Add(combo);

            var actions = new StackPanel
            {
                Orientation = Orientation.Horizontal,
                HorizontalAlignment = HorizontalAlignment.Right
            };
            var ok = new Button { Content = "确定", Width = 76, Style = (Style)FindResource("PrimaryButton") };
            ok.Click += (_, __) =>
            {
                selected = ((combo.SelectedItem as ComboBoxItem)?.Tag as string) ?? "cpa";
                dialog.Close();
            };
            var cancel = new Button { Content = "取消", Width = 76, Margin = new Thickness(8, 0, 0, 0) };
            cancel.Click += (_, __) =>
            {
                selected = "";
                dialog.Close();
            };
            actions.Children.Add(ok);
            actions.Children.Add(cancel);
            Grid.SetRow(actions, 2);
            root.Children.Add(actions);

            dialog.Content = root;
            dialog.ShowDialog();
            return selected;
        }

        private void AddImportTargetArg(List<string> args, string target)
        {
            args.Add("--import-target");
            string value = (target ?? "").Trim().ToLowerInvariant();
            if (value == "sub2api")
            {
                args.Add("sub2api");
            }
            else if (value == "cliproxyapi")
            {
                args.Add("cliproxyapi");
            }
            else
            {
                args.Add("cpa");
            }
        }

        private string ImportTargetLabel(string target)
        {
            string value = (target ?? "").Trim().ToLowerInvariant();
            if (value == "sub2api") return "SUB2API";
            if (value == "cliproxyapi") return "CLIProxyAPI";
            return "CPA";
        }

        private void RefreshSession_Click(object sender, RoutedEventArgs e)
        {
            PoolRow row = SelectedAccountRow();
            if (row == null) return;
            var args = new List<string> { "--email", row.Identifier, "--refresh-session" };
            AddSessionFileArg(args, row);
            RunBackend("刷新Session", args);
        }

        private void AddSessionFileArg(List<string> args, PoolRow row)
        {
            string jsonPath = File.Exists(row.Notes) && row.Notes.EndsWith(".json", StringComparison.OrdinalIgnoreCase)
                ? row.Notes
                : row.SourcePath;
            if (File.Exists(jsonPath) && jsonPath.EndsWith(".json", StringComparison.OrdinalIgnoreCase))
            {
                args.Add("--session-file");
                args.Add(jsonPath);
            }
        }

        private PoolRow SelectedAccountRow()
        {
            PoolRow row = SelectedRow ?? (AccountGrid.SelectedItem as PoolRow);
            if (row == null)
            {
                MessageBox.Show("请先选择一条账号记录。", "未选择账号", MessageBoxButton.OK, MessageBoxImage.Information);
            }
            return row;
        }

        private List<PoolRow> SelectedRowsOrCurrent()
        {
            var rows = allRows.Where(r => r.IsChecked).ToList();
            if (rows.Count == 0)
            {
                PoolRow row = SelectedRow ?? (AccountGrid.SelectedItem as PoolRow);
                if (row != null) rows.Add(row);
            }
            return rows;
        }

        private void ApplyFilter_Click(object sender, RoutedEventArgs e)
        {
            currentPage = 1;
            RefreshPagedRows();
        }

        private void ShowAll_Click(object sender, RoutedEventArgs e) => SetScope("全部");

        private void ShowMailboxPool_Click(object sender, RoutedEventArgs e) => SetScope("邮箱池");

        private void ShowRegistered_Click(object sender, RoutedEventArgs e) => SetScope("已注册");

        private void ShowPending_Click(object sender, RoutedEventArgs e) => SetScope("待处理");

        private void FirstPage_Click(object sender, RoutedEventArgs e)
        {
            currentPage = 1;
            RefreshPagedRows();
        }

        private void PrevPage_Click(object sender, RoutedEventArgs e)
        {
            currentPage--;
            RefreshPagedRows();
        }

        private void NextPage_Click(object sender, RoutedEventArgs e)
        {
            currentPage++;
            RefreshPagedRows();
        }

        private void LastPage_Click(object sender, RoutedEventArgs e)
        {
            int pageSize = PageSizeValue();
            int count = allRows.Count(FilterRow);
            currentPage = Math.Max(1, (int)Math.Ceiling(count / (double)pageSize));
            RefreshPagedRows();
        }

        private void SetScope(string scope)
        {
            ScopeFilter = scope;
            currentPage = 1;
            RefreshPagedRows();
        }

        private void ClearSelection_Click(object sender, RoutedEventArgs e)
        {
            foreach (PoolRow row in allRows) row.IsChecked = false;
        }

        private void SelectAllFiltered_Click(object sender, RoutedEventArgs e)
        {
            foreach (PoolRow row in allRows.Where(FilterRow))
            {
                row.IsChecked = true;
            }
        }

        private async void ShowInboxDialog(PoolRow row)
        {
            var dialog = new Window
            {
                Title = "收件箱 - " + row.Identifier,
                Owner = this,
                Width = 860,
                Height = 640,
                MinWidth = 700,
                MinHeight = 500,
                WindowStartupLocation = WindowStartupLocation.CenterOwner,
                Background = (System.Windows.Media.Brush)FindResource("AppBg")
            };

            var root = new Grid { Margin = new Thickness(10) };
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });

            var header = new TextBlock
            {
                Text = "正在加载收件箱...",
                FontSize = 14,
                FontWeight = FontWeights.SemiBold,
                Foreground = (System.Windows.Media.Brush)FindResource("TextMain"),
                Margin = new Thickness(0, 0, 0, 8)
            };
            Grid.SetRow(header, 0);
            root.Children.Add(header);

            var mailGrid = new DataGrid
            {
                AutoGenerateColumns = false,
                CanUserAddRows = false,
                HeadersVisibility = DataGridHeadersVisibility.Column,
                IsReadOnly = true,
                RowHeight = 28,
                GridLinesVisibility = DataGridGridLinesVisibility.Horizontal,
                AlternatingRowBackground = (System.Windows.Media.Brush)FindResource("GridAltBg"),
                Background = (System.Windows.Media.Brush)FindResource("PanelBg"),
                Foreground = (System.Windows.Media.Brush)FindResource("TextMain"),
                BorderThickness = new Thickness(0)
            };
            mailGrid.Columns.Add(new DataGridTextColumn { Header = "时间", Binding = new System.Windows.Data.Binding("ReceivedAt"), Width = 150 });
            mailGrid.Columns.Add(new DataGridTextColumn { Header = "发件人", Binding = new System.Windows.Data.Binding("From"), Width = 200 });
            mailGrid.Columns.Add(new DataGridTextColumn { Header = "主题", Binding = new System.Windows.Data.Binding("Subject"), Width = new DataGridLength(1, DataGridLengthUnitType.Star) });
            Grid.SetRow(mailGrid, 1);
            root.Children.Add(mailGrid);

            var actions = new StackPanel
            {
                Orientation = Orientation.Horizontal,
                HorizontalAlignment = HorizontalAlignment.Right,
                Margin = new Thickness(0, 8, 0, 0)
            };
            var refreshBtn = new Button { Content = "刷新", Width = 72 };
            var closeBtn = new Button { Content = "关闭", Width = 72 };
            actions.Children.Add(refreshBtn);
            actions.Children.Add(closeBtn);
            Grid.SetRow(actions, 2);
            root.Children.Add(actions);

            var mailItems = new ObservableCollection<MailItem>();
            mailGrid.ItemsSource = mailItems;

            closeBtn.Click += (_, __) => dialog.Close();

            async Task LoadEmails()
            {
                if (IsCfWorkerRow(row))
                {
                    header.Text = "正在获取 CFWorker 邮件...";
                    try
                    {
                        mailItems.Clear();
                        foreach (MailItem item in await FetchCfWorkerInbox(row.Identifier, 25))
                        {
                            mailItems.Add(item);
                        }
                        header.Text = row.Identifier + " - 最近 " + mailItems.Count + " 封邮件";
                    }
                    catch (Exception ex)
                    {
                        header.Text = "获取邮件失败：" + ex.Message;
                        Log("CFWorker收件箱获取失败：" + ex.Message);
                    }
                    return;
                }

                header.Text = "正在刷新令牌...";
                string tokenUrl = "https://login.microsoftonline.com/common/oauth2/v2.0/token";
                var tokenBody = new Dictionary<string, string>
                {
                    ["grant_type"] = "refresh_token",
                    ["client_id"] = row.ClientId,
                    ["refresh_token"] = row.RawRefreshToken,
                    ["scope"] = "https://graph.microsoft.com/.default offline_access"
                };

                try
                {
                    mailItems.Clear();
                    foreach (MailItem item in await FetchBackendInbox(row, 20))
                    {
                        mailItems.Add(item);
                    }
                    header.Text = row.Identifier + " - " + mailItems.Count + " messages";

                    if (mailItems.Count < 0)
                    {
                    var tokenResp = await httpClient.PostAsync(tokenUrl, new FormUrlEncodedContent(tokenBody));
                    string tokenJson = await tokenResp.Content.ReadAsStringAsync();
                    if (!tokenResp.IsSuccessStatusCode)
                    {
                        header.Text = "令牌刷新失败 (" + (int)tokenResp.StatusCode + ")";
                        Log("收件箱令牌刷新失败：" + tokenJson);
                        return;
                    }

                    using var tokenDoc = JsonDocument.Parse(tokenJson);
                    string accessToken = tokenDoc.RootElement.GetProperty("access_token").GetString() ?? "";

                    header.Text = "正在获取邮件...";
                    string mailUrl = "https://graph.microsoft.com/v1.0/me/messages?$top=20&$orderby=receivedDateTime desc&$select=receivedDateTime,from,subject,bodyPreview";
                    var request = new HttpRequestMessage(HttpMethod.Get, mailUrl);
                    request.Headers.Authorization = new System.Net.Http.Headers.AuthenticationHeaderValue("Bearer", accessToken);
                    var mailResp = await httpClient.SendAsync(request);
                    string mailJson = await mailResp.Content.ReadAsStringAsync();

                    if (!mailResp.IsSuccessStatusCode)
                    {
                        header.Text = "获取邮件失败 (" + (int)mailResp.StatusCode + ")";
                        Log("收件箱获取失败：" + mailJson);
                        return;
                    }

                    mailItems.Clear();
                    using var mailDoc = JsonDocument.Parse(mailJson);
                    if (mailDoc.RootElement.TryGetProperty("value", out JsonElement values))
                    {
                        foreach (JsonElement msg in values.EnumerateArray())
                        {
                            string received = msg.TryGetProperty("receivedDateTime", out JsonElement dt) ? dt.GetString() ?? "" : "";
                            string from = "";
                            if (msg.TryGetProperty("from", out JsonElement fromObj) &&
                                fromObj.TryGetProperty("emailAddress", out JsonElement addr) &&
                                addr.TryGetProperty("address", out JsonElement addrStr))
                            {
                                from = addrStr.GetString() ?? "";
                            }
                            string subject = msg.TryGetProperty("subject", out JsonElement subj) ? subj.GetString() ?? "" : "";
                            string preview = msg.TryGetProperty("bodyPreview", out JsonElement bp) ? bp.GetString() ?? "" : "";

                            if (received.Length > 19) received = received.Substring(0, 19).Replace("T", " ");
                            mailItems.Add(new MailItem { ReceivedAt = received, From = from, Subject = subject, BodyPreview = preview });
                        }
                    }
                    header.Text = row.Identifier + " - 最近 " + mailItems.Count + " 封邮件";
                    }
                }
                catch (Exception ex)
                {
                    header.Text = "加载失败：" + ex.Message;
                    Log("收件箱加载异常：" + ex.Message);
                }
            }

            refreshBtn.Click += async (_, __) => await LoadEmails();
            mailGrid.MouseDoubleClick += (_, __) =>
            {
                if (mailGrid.SelectedItem is MailItem item)
                {
                    ShowMailDetailDialog(item);
                }
            };

            dialog.Content = root;
            dialog.Show();
            await LoadEmails();
        }

        private async Task<List<MailItem>> FetchBackendInbox(PoolRow row, int limit)
        {
            string script = Path.Combine(rootDir, "chatgpt_phone_reg.py");
            if (!File.Exists(script)) throw new FileNotFoundException("Backend script not found", script);
            var args = new List<string> { "--view-inbox", "--email", row.Identifier, "--inbox-limit", limit.ToString() };
            string mailboxLine = FindMailboxLineForRow(row);
            if (mailboxLine.Length == 0 && MailboxArgForLine(row.RawLine).Length > 0)
            {
                mailboxLine = row.RawLine;
            }
            string mailboxArg = MailboxArgForLine(mailboxLine);
            string tempMailboxFile = "";
            if (mailboxArg.Length > 0)
            {
                tempMailboxFile = Path.Combine(Path.GetTempPath(), "view_inbox_mailbox_" + DateTime.Now.ToString("yyyyMMdd_HHmmss_fff") + ".txt");
                File.WriteAllText(tempMailboxFile, mailboxLine.Trim() + Environment.NewLine, new UTF8Encoding(false));
                args.AddRange(new[] { mailboxArg, tempMailboxFile });
            }
            AddSessionFileArg(args, row);
            AddProxy(args);
            var psi = new ProcessStartInfo
            {
                FileName = "python",
                Arguments = Quote(script) + " " + JoinArgs(args),
                WorkingDirectory = rootDir,
                UseShellExecute = false,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                CreateNoWindow = true,
                StandardOutputEncoding = Encoding.UTF8,
                StandardErrorEncoding = Encoding.UTF8
            };
            using var process = new Process { StartInfo = psi };
            process.Start();
            string stdout = await process.StandardOutput.ReadToEndAsync();
            string stderr = await process.StandardError.ReadToEndAsync();
            await process.WaitForExitAsync();
            if (process.ExitCode != 0)
            {
                throw new InvalidOperationException((stdout + "\n" + stderr).Trim());
            }
            using JsonDocument doc = JsonDocument.Parse(stdout);
            if (!doc.RootElement.TryGetProperty("ok", out JsonElement ok) || !ok.GetBoolean())
            {
                string error = JsonString(doc.RootElement, "error");
                throw new InvalidOperationException(error.Length > 0 ? error : stdout.Trim());
            }
            var items = new List<MailItem>();
            if (doc.RootElement.TryGetProperty("messages", out JsonElement messages) && messages.ValueKind == JsonValueKind.Array)
            {
                foreach (JsonElement msg in messages.EnumerateArray())
                {
                    string received = JsonString(msg, "receivedDateTime");
                    if (received.Length > 19) received = received.Substring(0, 19).Replace("T", " ");
                    items.Add(new MailItem
                    {
                        ReceivedAt = received,
                        From = JsonString(msg, "from"),
                        Subject = JsonString(msg, "subject"),
                        BodyPreview = JsonString(msg, "bodyPreview")
                    });
                }
            }
            return items;
        }

        private bool IsCfWorkerRow(PoolRow row)
        {
            if (row == null) return false;
            return row.MailboxProvider.Equals("cfworker", StringComparison.OrdinalIgnoreCase)
                || row.AccountType.Contains("CFWorker")
                || row.Identifier.EndsWith("@edu.liziai.cloud", StringComparison.OrdinalIgnoreCase)
                || row.RawLine.StartsWith("cfworker://", StringComparison.OrdinalIgnoreCase);
        }

        private async Task<List<MailItem>> FetchCfWorkerInbox(string email, int limit)
        {
            var emailSection = GetSection(ReadJsonObject(Path.Combine(rootDir, "config.json")), "email_registration");
            string baseUrl = GetString(emailSection, "cfworker_url").Trim().TrimEnd('/');
            string adminToken = GetString(emailSection, "cfworker_admin_token").Trim();
            string cfToken = GetString(emailSection, "cfworker_api_token").Trim();
            if (baseUrl.Length == 0) throw new InvalidOperationException("config.json 缺少 email_registration.cfworker_url");

            string normalizedEmail = email.Trim().ToLowerInvariant();
            string encoded = Uri.EscapeDataString(normalizedEmail);
            string domain = normalizedEmail.Contains("@") ? normalizedEmail.Substring(normalizedEmail.LastIndexOf('@') + 1) : "";
            string[] paths =
            {
                "/admin/emails?page=1&domain=" + Uri.EscapeDataString(domain) + "&address=" + encoded + "&to_address=" + encoded + "&email=" + encoded,
                "/admin/emails?page=1&address=" + encoded + "&to_address=" + encoded + "&email=" + encoded,
                "/api/messages?email=" + encoded + "&limit=" + limit,
                "/api/messages?address=" + encoded + "&limit=" + limit,
                "/api/messages?to_address=" + encoded + "&limit=" + limit,
                "/api/emails/" + encoded + "/messages?limit=" + limit,
                "/api/mailboxes/" + encoded + "/messages?limit=" + limit,
                "/api/mailbox/" + encoded + "?limit=" + limit,
                "/api/inbox/" + encoded + "?limit=" + limit,
                "/api/messages/" + encoded + "?limit=" + limit,
                "/messages/" + encoded + "?limit=" + limit,
                "/inbox/" + encoded + "?limit=" + limit
            };

            string lastError = "";
            foreach (string path in paths)
            {
                using var request = new HttpRequestMessage(HttpMethod.Get, baseUrl + path);
                request.Headers.Accept.ParseAdd("application/json");
                if (adminToken.Length > 0)
                {
                    request.Headers.Authorization = new System.Net.Http.Headers.AuthenticationHeaderValue("Bearer", adminToken);
                    request.Headers.TryAddWithoutValidation("X-Admin-Token", adminToken);
                }
                if (cfToken.Length > 0)
                {
                    request.Headers.TryAddWithoutValidation("X-CF-API-Token", cfToken);
                }

                using var cts = new System.Threading.CancellationTokenSource(TimeSpan.FromSeconds(15));
                using HttpResponseMessage response = await httpClient.SendAsync(request, cts.Token);
                string text = await response.Content.ReadAsStringAsync();
                if (!response.IsSuccessStatusCode)
                {
                    lastError = ((int)response.StatusCode) + " " + response.ReasonPhrase;
                    continue;
                }
                using JsonDocument doc = JsonDocument.Parse(text.Length == 0 ? "[]" : text);
                var items = ExtractCfWorkerMailItems(doc.RootElement, email, limit);
                if (items.Count > 0 || LooksEmptyMessageList(doc.RootElement)) return items;
            }
            throw new InvalidOperationException(lastError.Length > 0 ? lastError : "未找到可用的 CFWorker 收件箱接口");
        }

        private List<MailItem> ExtractCfWorkerMailItems(JsonElement root, string email, int limit)
        {
            var array = FindMessageArray(root);
            var items = new List<MailItem>();
            if (array.ValueKind != JsonValueKind.Array) return items;
            foreach (JsonElement msg in array.EnumerateArray())
            {
                if (items.Count >= limit) break;
                string to = JsonStringAny(msg, "to_address", "recipient", "mailbox", "email", "address", "to");
                if (to.Length > 0 && !to.Contains(email, StringComparison.OrdinalIgnoreCase)) continue;
                string subject = JsonStringAny(msg, "subject", "title");
                string from = JsonStringAny(msg, "from_email", "from_address", "sender", "from");
                string received = JsonStringAny(msg, "receivedDateTime", "received_at", "created_at", "date", "timestamp");
                string body = JsonStringAny(msg, "bodyPreview", "preview", "text", "content", "body", "html", "extracted_json");
                if (msg.TryGetProperty("body", out JsonElement bodyObj) && bodyObj.ValueKind == JsonValueKind.Object)
                {
                    body = JsonStringAny(bodyObj, "content", "text", "html");
                }
                if (from.StartsWith("{")) from = "";
                received = FormatCfWorkerReceivedAt(received);
                items.Add(new MailItem
                {
                    ReceivedAt = received,
                    From = from,
                    Subject = subject,
                    BodyPreview = body
                });
            }
            return items;
        }

        private string FormatCfWorkerReceivedAt(string value)
        {
            string text = (value ?? "").Trim();
            if (long.TryParse(text, out long epoch))
            {
                try
                {
                    DateTimeOffset dto = epoch > 10000000000L
                        ? DateTimeOffset.FromUnixTimeMilliseconds(epoch)
                        : DateTimeOffset.FromUnixTimeSeconds(epoch);
                    return dto.LocalDateTime.ToString("yyyy-MM-dd HH:mm:ss");
                }
                catch
                {
                    return text;
                }
            }
            if (text.Length > 19) return text.Substring(0, 19).Replace("T", " ");
            return text;
        }

        private JsonElement FindMessageArray(JsonElement element)
        {
            if (element.ValueKind == JsonValueKind.Array) return element;
            if (element.ValueKind != JsonValueKind.Object) return default;
            foreach (string key in new[] { "messages", "mails", "emails", "items", "data", "value", "results" })
            {
                if (!element.TryGetProperty(key, out JsonElement child)) continue;
                if (child.ValueKind == JsonValueKind.Array) return child;
                JsonElement nested = FindMessageArray(child);
                if (nested.ValueKind == JsonValueKind.Array) return nested;
            }
            return default;
        }

        private bool LooksEmptyMessageList(JsonElement element)
        {
            JsonElement array = FindMessageArray(element);
            return array.ValueKind == JsonValueKind.Array && array.GetArrayLength() == 0;
        }

        private string JsonStringAny(JsonElement obj, params string[] properties)
        {
            if (obj.ValueKind != JsonValueKind.Object) return obj.ValueKind == JsonValueKind.String ? obj.GetString() ?? "" : "";
            foreach (string property in properties)
            {
                if (!obj.TryGetProperty(property, out JsonElement value)) continue;
                if (value.ValueKind == JsonValueKind.String) return value.GetString() ?? "";
                if (value.ValueKind == JsonValueKind.Number) return value.ToString();
            }
            return "";
        }

        private void ShowMailDetailDialog(MailItem item)
        {
            if (item == null) return;
            string code = ExtractVerificationCode(item.BodyPreview);
            var dialog = new Window
            {
                Title = item.Subject.Length > 0 ? item.Subject : "邮件详情",
                Owner = this,
                Width = 720,
                Height = 460,
                MinWidth = 560,
                MinHeight = 360,
                WindowStartupLocation = WindowStartupLocation.CenterOwner,
                Background = (System.Windows.Media.Brush)FindResource("AppBg")
            };

            var root = new Grid { Margin = new Thickness(14) };
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });

            var title = new TextBlock
            {
                Text = item.Subject,
                FontSize = 16,
                FontWeight = FontWeights.SemiBold,
                TextWrapping = TextWrapping.Wrap,
                Foreground = (System.Windows.Media.Brush)FindResource("TextMain")
            };
            Grid.SetRow(title, 0);
            root.Children.Add(title);

            var meta = new TextBlock
            {
                Text = item.ReceivedAt + "    " + item.From,
                Margin = new Thickness(0, 6, 0, 10),
                Foreground = (System.Windows.Media.Brush)FindResource("TextSub")
            };
            Grid.SetRow(meta, 1);
            root.Children.Add(meta);

            var body = new TextBox
            {
                Text = item.BodyPreview,
                IsReadOnly = true,
                AcceptsReturn = true,
                TextWrapping = TextWrapping.Wrap,
                VerticalScrollBarVisibility = ScrollBarVisibility.Auto,
                HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled,
                VerticalContentAlignment = VerticalAlignment.Top,
                Height = double.NaN,
                Background = (System.Windows.Media.Brush)FindResource("PanelBg"),
                Foreground = (System.Windows.Media.Brush)FindResource("TextMain"),
                BorderBrush = (System.Windows.Media.Brush)FindResource("Line")
            };
            Grid.SetRow(body, 2);
            root.Children.Add(body);

            var actions = new StackPanel
            {
                Orientation = Orientation.Horizontal,
                HorizontalAlignment = HorizontalAlignment.Right,
                Margin = new Thickness(0, 10, 0, 0)
            };
            var copyCodeBtn = new Button { Content = code.Length > 0 ? "复制验证码 " + code : "未识别验证码", MinWidth = 120, IsEnabled = code.Length > 0 };
            var copyBodyBtn = new Button { Content = "复制正文", Width = 86 };
            var closeBtn = new Button { Content = "关闭", Width = 72 };
            copyCodeBtn.Click += (_, __) =>
            {
                Clipboard.SetText(code);
                Log("验证码已复制：" + code);
            };
            copyBodyBtn.Click += (_, __) => Clipboard.SetText(item.BodyPreview);
            closeBtn.Click += (_, __) => dialog.Close();
            actions.Children.Add(copyCodeBtn);
            actions.Children.Add(copyBodyBtn);
            actions.Children.Add(closeBtn);
            Grid.SetRow(actions, 3);
            root.Children.Add(actions);

            dialog.Content = root;
            dialog.ShowDialog();
        }

        private string ExtractVerificationCode(string text)
        {
            Match match = Regex.Match(text ?? "", @"(?<!\d)\d{5,8}(?!\d)");
            return match.Success ? match.Value : "";
        }

        private sealed class MailItem
        {
            public string ReceivedAt { get; set; } = "";
            public string From { get; set; } = "";
            public string Subject { get; set; } = "";
            public string BodyPreview { get; set; } = "";
        }

        private void ShowAccountDetail(PoolRow row)
        {
            if (row == null) return;
            string detail = BuildAccountDetail(row);
            string paypalUrl = row.PayPalUrl ?? "";
            bool hasPayPal = !string.IsNullOrWhiteSpace(paypalUrl);
            var dialog = new Window
            {
                Title = "账号详情 - " + row.Identifier,
                Owner = this,
                Width = 960,
                Height = 740,
                MinWidth = 780,
                MinHeight = 580,
                WindowStartupLocation = WindowStartupLocation.CenterOwner,
                Background = (System.Windows.Media.Brush)FindResource("AppBg")
            };

            var root = new Grid { Margin = new Thickness(16) };
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });     // 0: title
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });     // 1: summary
            if (hasPayPal)
                root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto }); // 2: paypal url
            root.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) }); // detail
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });     // actions

            // Title with status badge
            var titlePanel = new StackPanel { Orientation = Orientation.Horizontal, Margin = new Thickness(0, 0, 0, 12) };
            titlePanel.Children.Add(new TextBlock
            {
                Text = row.Identifier,
                FontSize = 18,
                FontWeight = FontWeights.SemiBold,
                Foreground = (System.Windows.Media.Brush)FindResource("TextMain"),
                VerticalAlignment = VerticalAlignment.Center
            });
            if (!string.IsNullOrWhiteSpace(row.Status))
            {
                titlePanel.Children.Add(new Border
                {
                    Background = (System.Windows.Media.Brush)FindResource("PrimarySoft"),
                    CornerRadius = new CornerRadius(4),
                    Padding = new Thickness(8, 2, 8, 2),
                    Margin = new Thickness(12, 0, 0, 0),
                    VerticalAlignment = VerticalAlignment.Center,
                    Child = new TextBlock
                    {
                        Text = row.Status,
                        FontSize = 11,
                        Foreground = (System.Windows.Media.Brush)FindResource("TextSub")
                    }
                });
            }
            Grid.SetRow(titlePanel, 0);
            root.Children.Add(titlePanel);

            // Summary cards - 2-column layout
            var summaryGrid = new Grid { Margin = new Thickness(0, 0, 0, 12) };
            summaryGrid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            summaryGrid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            for (int i = 0; i < 3; i++) summaryGrid.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });

            var infoItems = new (string label, string value)[]
            {
                ("邮箱", row.Identifier),
                ("类型", row.AccountType ?? ""),
                ("状态", row.Status ?? ""),
                ("支付状态", row.PayPalStatus ?? ""),
                ("支付金额", row.PayPalAmount ?? ""),
                ("Refresh Token", row.RefreshTokenStatus ?? ""),
                ("创建时间", row.CreatedAt ?? ""),
                ("更新时间", row.CompletedAt ?? ""),
            };

            int idx = 0;
            for (int r = 0; r < 3; r++)
            {
                for (int c = 0; c < 2 && idx < infoItems.Length; c++, idx++)
                {
                    var (label, value) = infoItems[idx];
                    var card = new Border
                    {
                        Background = (System.Windows.Media.Brush)FindResource("PanelBg"),
                        BorderBrush = (System.Windows.Media.Brush)FindResource("Line"),
                        BorderThickness = new Thickness(1),
                        CornerRadius = new CornerRadius(8),
                        Padding = new Thickness(12, 8, 12, 8),
                        Margin = new Thickness(c == 0 ? 0 : 6, r == 0 ? 0 : 6, c == 1 ? 0 : 6, 0)
                    };
                    var cardStack = new StackPanel();
                    cardStack.Children.Add(new TextBlock
                    {
                        Text = label,
                        FontSize = 11,
                        Foreground = (System.Windows.Media.Brush)FindResource("TextMuted"),
                        Margin = new Thickness(0, 0, 0, 4)
                    });
                    cardStack.Children.Add(new TextBlock
                    {
                        Text = string.IsNullOrWhiteSpace(value) ? "—" : value,
                        FontSize = 13,
                        FontWeight = FontWeights.Medium,
                        Foreground = (System.Windows.Media.Brush)FindResource("TextMain"),
                        TextTrimming = TextTrimming.CharacterEllipsis
                    });
                    card.Child = cardStack;
                    Grid.SetRow(card, r);
                    Grid.SetColumn(card, c);
                    summaryGrid.Children.Add(card);
                }
            }
            Grid.SetRow(summaryGrid, 1);
            root.Children.Add(summaryGrid);

            // PayPal URL display (if present)
            if (hasPayPal)
            {
                var urlPanel = new Border
                {
                    Background = (System.Windows.Media.Brush)FindResource("PanelBg"),
                    BorderBrush = (System.Windows.Media.Brush)FindResource("Line"),
                    BorderThickness = new Thickness(1),
                    CornerRadius = new CornerRadius(8),
                    Padding = new Thickness(12, 6, 12, 6),
                    Margin = new Thickness(0, 0, 0, 10)
                };
                var urlStack = new StackPanel();
                urlStack.Children.Add(new TextBlock
                {
                    Text = "支付订阅链接",
                    FontSize = 11,
                    Foreground = (System.Windows.Media.Brush)FindResource("TextMuted"),
                    Margin = new Thickness(0, 0, 0, 4)
                });
                urlStack.Children.Add(new TextBox
                {
                    Text = paypalUrl,
                    IsReadOnly = true,
                    TextWrapping = TextWrapping.Wrap,
                    BorderThickness = new Thickness(0),
                    Background = Brushes.Transparent,
                    Foreground = (System.Windows.Media.Brush)FindResource("TextSub"),
                    FontSize = 12,
                    Padding = new Thickness(0)
                });
                urlPanel.Child = urlStack;
                Grid.SetRow(urlPanel, 2);
                root.Children.Add(urlPanel);
            }

            // Raw detail text
            var detailBorder = new Border
            {
                Background = (System.Windows.Media.Brush)FindResource("PanelBg"),
                BorderBrush = (System.Windows.Media.Brush)FindResource("Line"),
                BorderThickness = new Thickness(1),
                CornerRadius = new CornerRadius(8),
                Margin = new Thickness(0, 0, 0, 10)
            };
            var text = new TextBox
            {
                Text = detail,
                IsReadOnly = true,
                AcceptsReturn = true,
                TextWrapping = TextWrapping.NoWrap,
                FontFamily = new System.Windows.Media.FontFamily("Consolas"),
                FontSize = 12,
                Foreground = (System.Windows.Media.Brush)FindResource("TextMain"),
                VerticalScrollBarVisibility = ScrollBarVisibility.Auto,
                HorizontalScrollBarVisibility = ScrollBarVisibility.Auto,
                VerticalAlignment = VerticalAlignment.Stretch,
                HorizontalAlignment = HorizontalAlignment.Stretch,
                MinHeight = 200,
                Background = Brushes.Transparent,
                BorderThickness = new Thickness(0),
                Padding = new Thickness(10, 8, 10, 8)
            };
            detailBorder.Child = text;
            int detailRow = hasPayPal ? 3 : 2;
            Grid.SetRow(detailBorder, detailRow);
            root.Children.Add(detailBorder);

            // Action buttons - two rows for better spacing
            var actionsGrid = new Grid { Margin = new Thickness(0, 4, 0, 0) };
            actionsGrid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            actionsGrid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });

            // Left: secondary actions
            var leftActions = new StackPanel { Orientation = Orientation.Horizontal };
            var openButton = new Button { Content = "打开源文件", MinWidth = 100, Margin = new Thickness(0, 0, 8, 0) };
            openButton.Click += (_, __) => OpenAccountJson(row);
            leftActions.Children.Add(openButton);

            // Right: primary actions
            var rightActions = new StackPanel { Orientation = Orientation.Horizontal, HorizontalAlignment = HorizontalAlignment.Right };
            var openPayPalButton = new Button { Content = "打开支付链接", MinWidth = 120, IsEnabled = hasPayPal, Margin = new Thickness(0, 0, 8, 0) };
            openPayPalButton.Click += (_, __) => OpenPayPalUrl(paypalUrl, row.Identifier);
            var copyPayPalButton = new Button { Content = "复制支付链接", MinWidth = 120, IsEnabled = hasPayPal, Margin = new Thickness(0, 0, 8, 0) };
            copyPayPalButton.Click += (_, __) => CopyPayPalUrl(paypalUrl);
            var markPayPalCompleteButton = new Button { Content = "标记支付完成", MinWidth = 120, Style = (System.Windows.Style)FindResource("PrimaryButton"), Margin = new Thickness(0, 0, 8, 0) };
            markPayPalCompleteButton.Click += (_, __) =>
            {
                MarkPayPalComplete(row);
                dialog.Close();
            };
            var closeButton = new Button { Content = "关闭", MinWidth = 80 };
            closeButton.Click += (_, __) => dialog.Close();
            rightActions.Children.Add(openPayPalButton);
            rightActions.Children.Add(copyPayPalButton);
            rightActions.Children.Add(markPayPalCompleteButton);
            rightActions.Children.Add(closeButton);

            Grid.SetColumn(leftActions, 0);
            Grid.SetColumn(rightActions, 1);
            actionsGrid.Children.Add(leftActions);
            actionsGrid.Children.Add(rightActions);
            Grid.SetRow(actionsGrid, detailRow + 1);
            root.Children.Add(actionsGrid);

            dialog.Content = root;
            dialog.ShowDialog();
        }

        private void OpenAccountJson(PoolRow row)
        {
            string path = ResolveAccountJsonPath(row);
            if (string.IsNullOrWhiteSpace(path))
            {
                MessageBox.Show("未找到该账号对应的 JSON 文件。", "打开源文件", MessageBoxButton.OK, MessageBoxImage.Information);
                return;
            }
            OpenPath(path);
        }

        private string ResolveAccountJsonPath(PoolRow row)
        {
            if (row == null) return "";
            string notes = (row.Notes ?? "").Trim();
            if (File.Exists(notes) && notes.EndsWith(".json", StringComparison.OrdinalIgnoreCase)) return notes;
            string source = (row.SourcePath ?? "").Trim();
            if (File.Exists(source) && source.EndsWith(".json", StringComparison.OrdinalIgnoreCase)) return source;
            if (!File.Exists(source) || !source.EndsWith(".sqlite3", StringComparison.OrdinalIgnoreCase)) return "";

            try
            {
                string sql = "SELECT email,json_path,raw_json FROM accounts WHERE id=" + OnlyDigits(row.RawLine);
                var rows = SqliteNative.Query(source, sql);
                if (rows.Count == 0) return "";
                Dictionary<string, string> data = rows[0];
                string jsonPath = data.TryGetValue("json_path", out string rawJsonPath) ? rawJsonPath : "";
                if (File.Exists(jsonPath) && jsonPath.EndsWith(".json", StringComparison.OrdinalIgnoreCase)) return jsonPath;

                string rawJson = data.TryGetValue("raw_json", out string rawRawJson) ? rawRawJson : "";
                if (string.IsNullOrWhiteSpace(rawJson)) return "";
                string email = data.TryGetValue("email", out string rawEmail) ? rawEmail : row.Identifier;
                string safeEmail = Regex.Replace((email ?? "unknown").Trim(), @"[^a-zA-Z0-9_.@+-]+", "_");
                string dir = Path.Combine(rootDir, "runtime", "account_json");
                Directory.CreateDirectory(dir);
                string outPath = Path.Combine(dir, "account_" + safeEmail + ".json");
                File.WriteAllText(outPath, PrettyJson(rawJson), new UTF8Encoding(false));
                return outPath;
            }
            catch (Exception ex)
            {
                Log("打开账号JSON失败：" + ex.Message);
                return "";
            }
        }

        private string PrettyJson(string rawJson)
        {
            try
            {
                using JsonDocument document = JsonDocument.Parse(rawJson);
                return JsonSerializer.Serialize(document.RootElement, new JsonSerializerOptions { WriteIndented = true });
            }
            catch
            {
                return rawJson;
            }
        }

        private void AddDetailRow(Grid parent, int row, string label, string value)
        {
            var labelBlock = new TextBlock
            {
                Text = label,
                Margin = new Thickness(10, 7, 10, 7),
                VerticalAlignment = VerticalAlignment.Top,
                Foreground = (System.Windows.Media.Brush)FindResource("TextSub")
            };
            Grid.SetRow(labelBlock, row);
            Grid.SetColumn(labelBlock, 0);
            parent.Children.Add(labelBlock);

            bool longValue = label.Contains("链接") || (value ?? "").StartsWith("http", StringComparison.OrdinalIgnoreCase);
            var valueBox = new TextBox
            {
                Text = value ?? "",
                Margin = new Thickness(0, 4, 10, 4),
                IsReadOnly = true,
                BorderThickness = longValue ? new Thickness(1) : new Thickness(0),
                Background = (System.Windows.Media.Brush)FindResource("PanelBg"),
                Foreground = (System.Windows.Media.Brush)FindResource("TextMain"),
                TextWrapping = longValue ? TextWrapping.Wrap : TextWrapping.NoWrap,
                HorizontalScrollBarVisibility = longValue ? ScrollBarVisibility.Disabled : ScrollBarVisibility.Auto,
                VerticalScrollBarVisibility = longValue ? ScrollBarVisibility.Auto : ScrollBarVisibility.Disabled,
                MinHeight = longValue ? 58 : 0,
                MaxHeight = longValue ? 96 : double.PositiveInfinity,
                Padding = longValue ? new Thickness(6, 4, 6, 4) : new Thickness(0)
            };
            Grid.SetRow(valueBox, row);
            Grid.SetColumn(valueBox, 1);
            parent.Children.Add(valueBox);
        }

        private string BuildAccountDetail(PoolRow row)
        {
            var lines = new List<string>
            {
                "email: " + row.Identifier,
                "type: " + row.AccountType,
                "status: " + row.Status,
                "created_at: " + row.CreatedAt,
                "updated_at: " + row.CompletedAt,
                "source: " + row.Notes,
                ""
            };

            try
            {
                if (row.SourcePath.EndsWith(".sqlite3", StringComparison.OrdinalIgnoreCase))
                {
                    string sql = "SELECT * FROM accounts WHERE id=" + OnlyDigits(row.RawLine);
                    var rows = SqliteNative.Query(row.SourcePath, sql);
                    if (rows.Count > 0)
                    {
                        foreach (KeyValuePair<string, string> item in rows[0])
                        {
                            lines.Add(item.Key + ": " + MaskSensitiveField(item.Key, item.Value));
                        }
                    }
                    return string.Join(Environment.NewLine, lines);
                }

                if (File.Exists(row.SourcePath) && row.SourcePath.EndsWith(".json", StringComparison.OrdinalIgnoreCase))
                {
                    Dictionary<string, object> data = ReadJsonObject(row.SourcePath);
                    AppendJsonDetail(lines, data, "");
                }
            }
            catch (Exception ex)
            {
                lines.Add("detail_error: " + ex.Message);
            }
            return string.Join(Environment.NewLine, lines);
        }

        private void AppendJsonDetail(List<string> lines, Dictionary<string, object> data, string prefix)
        {
            foreach (KeyValuePair<string, object> item in data)
            {
                string key = string.IsNullOrEmpty(prefix) ? item.Key : prefix + "." + item.Key;
                if (item.Value is Dictionary<string, object> nested)
                {
                    AppendJsonDetail(lines, nested, key);
                    continue;
                }
                if (item.Value is List<object> list)
                {
                    lines.Add(key + ": [" + list.Count + " item(s)]");
                    continue;
                }
                lines.Add(key + ": " + MaskSensitiveField(key, Convert.ToString(item.Value) ?? ""));
            }
        }

        private string MaskSensitiveField(string key, string value)
        {
            string lower = (key ?? "").ToLowerInvariant();
            if (lower.Contains("token") || lower.Contains("cookie") || lower.Contains("password") || lower.Contains("api_key"))
            {
                return Mask(value);
            }
            return value ?? "";
        }

        private void ShowConfigDialog()
        {
            string path = Path.Combine(rootDir, "config.json");
            EnsureConfigFile(path);
            var config = ReadJsonObject(path);
            var email = GetSection(config, "email_registration");
            var proxy = GetSection(config, "proxy");
            var paypal = GetSection(config, "paypal");
            var paypalBrowser = GetSection(config, "paypal_browser");
            var paypalNocard = GetSection(config, "paypal_nocard");
            var gopay = GetSection(config, "gopay");
            var gopayStageProxies = GetChildSection(gopay, "stage_proxies");
            var gopayWaRebind = GetChildSection(gopay, "wa_rebind");
            var gopayOtp = GetChildSection(gopay, "otp");
            var gopayOtpSmsBower = GetChildSection(gopayOtp, "smsbower");
            var storage = GetSection(config, "storage");
            var output = GetSection(config, "output");
            var cpaMode = GetSection(config, "cpa_mode");
            var sub2api = GetSection(config, "sub2api");
            var codexOauth = GetSection(config, "codex_oauth");
            var phoneReuse = GetSection(config, "phone_reuse");
            var smsBower = GetChildSection(phoneReuse, "smsbower");
            var nextSms = GetChildSection(phoneReuse, "nextsms");

            var dialog = new Window
            {
                Title = "配置",
                Owner = this,
                Width = 860,
                Height = 660,
                MinWidth = 760,
                MinHeight = 560,
                WindowStartupLocation = WindowStartupLocation.CenterOwner,
                Background = (System.Windows.Media.Brush)FindResource("AppBg")
            };

            var root = new Grid { Margin = new Thickness(16) };
            root.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });

            var content = new Grid();
            content.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(178) });
            content.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(12) });
            content.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            Grid.SetRow(content, 0);
            root.Children.Add(content);

            var sidebar = new StackPanel();
            sidebar.Children.Add(new TextBlock
            {
                Text = "配置分类",
                FontSize = 13,
                FontWeight = FontWeights.SemiBold,
                Foreground = (Brush)FindResource("TextMuted"),
                Margin = new Thickness(4, 0, 0, 12)
            });
            var sidebarShell = new Border
            {
                Background = (Brush)FindResource("SidebarBg"),
                BorderBrush = (Brush)FindResource("Line"),
                BorderThickness = new Thickness(1),
                CornerRadius = new CornerRadius(6),
                Padding = new Thickness(10),
                Child = sidebar
            };
            Grid.SetColumn(sidebarShell, 0);
            content.Children.Add(sidebarShell);

            var host = new Grid();
            var hostScroll = new ScrollViewer
            {
                Content = host,
                VerticalScrollBarVisibility = ScrollBarVisibility.Auto,
                Padding = new Thickness(0)
            };
            var hostShell = new Border
            {
                Background = (Brush)FindResource("PanelBg"),
                BorderBrush = (Brush)FindResource("Line"),
                BorderThickness = new Thickness(1),
                CornerRadius = new CornerRadius(6),
                Padding = new Thickness(18),
                Child = hostScroll
            };
            Grid.SetColumn(hostShell, 2);
            content.Children.Add(hostShell);

            var fields = new Dictionary<string, TextBox>();
            var comboFields = new Dictionary<string, ComboBox>();
            var categories = new List<ConfigCategory>();

            var mailForm = AddConfigCategory(sidebar, host, categories, "邮箱", "邮箱池和 OTP 轮询配置。");
            int row = 0;
            AddConfigField(mailForm, fields, row++, "OTP轮询间隔秒", "otp_poll_interval", GetString(email, "otp_poll_interval"));
            AddConfigField(mailForm, fields, row++, "邮箱池文件", "token_file", GetString(email, "token_file"));

            var cfForm = AddConfigCategory(sidebar, host, categories, "CFWorker", "临时域名邮箱和 Cloudflare Worker 接入配置。");
            row = 0;
            AddConfigField(cfForm, fields, row++, "CFWorker URL", "cfworker_url", GetString(email, "cfworker_url"));
            AddConfigField(cfForm, fields, row++, "CFWorker 域名", "cfworker_domain", GetString(email, "cfworker_domain"));
            AddConfigField(cfForm, fields, row++, "CFWorker Admin Token", "cfworker_admin_token", GetString(email, "cfworker_admin_token"));
            AddConfigField(cfForm, fields, row++, "Cloudflare API Token", "cfworker_api_token", GetString(email, "cfworker_api_token"));

            var phoneForm = AddConfigCategory(sidebar, host, categories, "手机接码", "SMSBower / NextSMS 手机号接码、复用次数和 Codex OAuth 接码开关。");
            row = 0;
            AddConfigComboField(phoneForm, comboFields, row++, "接码来源", "phone_source", FirstNonEmpty(GetString(phoneReuse, "source"), "smsbower"), new[] { "smsbower", "nextsms", "phone_pool" });
            AddConfigField(phoneForm, fields, row++, "SMSBower API Key", "smsbower_api_key", GetString(smsBower, "api_key"));
            AddConfigField(phoneForm, fields, row++, "服务代码", "smsbower_service", GetString(smsBower, "service"));
            AddConfigComboField(phoneForm, comboFields, row++, "国家代码", "smsbower_country", GetString(smsBower, "country"), SmsBowerCountryOptions, "38");
            AddConfigField(phoneForm, fields, row++, "NextSMS API Key", "nextsms_api_key", GetString(nextSms, "api_key"));
            AddConfigField(phoneForm, fields, row++, "NextSMS Endpoint", "nextsms_endpoint", FirstNonEmpty(GetString(nextSms, "endpoint"), "https://sms.nextactionplus.com/api/"));
            AddConfigField(phoneForm, fields, row++, "NextSMS Service", "nextsms_service", FirstNonEmpty(GetString(nextSms, "service"), "openai"));
            AddConfigField(phoneForm, fields, row++, "NextSMS Country", "nextsms_country", FirstNonEmpty(GetString(nextSms, "country"), "US"));
            AddConfigField(phoneForm, fields, row++, "NextSMS Pricing", "nextsms_pricing_option", FirstNonEmpty(GetString(nextSms, "pricing_option"), "0"));
            AddConfigField(phoneForm, fields, row++, "NextSMS Pool Size", "nextsms_pool_size", FirstNonEmpty(GetString(nextSms, "pool_size"), "1"));
            AddConfigField(phoneForm, fields, row++, "NextSMS SMS Timeout", "nextsms_sms_timeout", FirstNonEmpty(GetString(nextSms, "sms_timeout"), "120"));
            AddConfigField(phoneForm, fields, row++, "NextSMS Poll Interval", "nextsms_sms_poll_interval", FirstNonEmpty(GetString(nextSms, "sms_poll_interval"), "5"));
            AddConfigField(phoneForm, fields, row++, "NextSMS Number Attempts", "nextsms_number_attempts", FirstNonEmpty(GetString(nextSms, "number_attempts"), "3"));
            AddConfigField(phoneForm, fields, row++, "GoPay SMSBower服务代码", "smsbower_gopay_service", GetString(smsBower, "gopay_service"));
            AddConfigField(phoneForm, fields, row++, "GoPay SMSBower国家代码", "smsbower_gopay_country", GetString(smsBower, "gopay_country"));
            AddConfigField(phoneForm, fields, row++, "GoPay SMSBower最低价格", "smsbower_gopay_min_price", GetString(smsBower, "gopay_min_price"));
            AddConfigField(phoneForm, fields, row++, "GoPay SMSBower最高价格", "smsbower_gopay_max_price", GetString(smsBower, "gopay_max_price"));
            AddConfigField(phoneForm, fields, row++, "最低价格", "smsbower_min_price", GetString(smsBower, "min_price"));
            AddConfigField(phoneForm, fields, row++, "最高价格", "smsbower_max_price", GetString(smsBower, "max_price"));
            AddConfigField(phoneForm, fields, row++, "目标价格", "smsbower_target_price", GetString(smsBower, "target_price"));
            AddConfigField(phoneForm, fields, row++, "号码池数量", "smsbower_pool_size", GetString(smsBower, "pool_size"));
            AddConfigField(phoneForm, fields, row++, "短信等待秒", "smsbower_sms_timeout", GetString(smsBower, "sms_timeout"));
            AddConfigField(phoneForm, fields, row++, "短信轮询间隔秒", "smsbower_sms_poll_interval", GetString(smsBower, "sms_poll_interval"));
            AddConfigField(phoneForm, fields, row++, "复用次数", "phone_max_reuse_count", GetString(phoneReuse, "max_reuse_count"));
            AddConfigField(phoneForm, fields, row++, "发码冷却秒", "phone_send_cooldown_seconds", GetString(phoneReuse, "send_cooldown_seconds"));
            AddConfigField(phoneForm, fields, row++, "发码重试次数", "phone_send_retry_attempts", GetString(phoneReuse, "send_retry_attempts"));
            AddConfigField(phoneForm, fields, row++, "发码重试延迟秒", "phone_send_retry_delay_seconds", GetString(phoneReuse, "send_retry_delay_seconds"));
            AddConfigField(phoneForm, fields, row++, "状态文件", "phone_state_file", GetString(phoneReuse, "state_file"));
            AddConfigField(phoneForm, fields, row++, "固定号码池", "phone_pool_lines", FormatPhonePool(phoneReuse), multiline: true);
            AddConfigField(phoneForm, fields, row++, "OAuth超时秒", "codex_registration_timeout", GetString(codexOauth, "registration_timeout"));
            AddConfigField(phoneForm, fields, row++, "允许邮箱OTP兜底", "codex_allow_passwordless_takeover", GetString(codexOauth, "allow_passwordless_takeover"));
            AddConfigField(phoneForm, fields, row++, "自动手机验证", "codex_auto_phone_verification", GetString(codexOauth, "auto_phone_verification"));
            AddConfigField(phoneForm, fields, row++, "注册要求RT", "codex_require_registration_refresh_token", GetString(codexOauth, "require_registration_refresh_token"));
            AddConfigField(phoneForm, fields, row++, "注册要求手机号", "codex_require_registration_phone_verification", GetString(codexOauth, "require_registration_phone_verification"));

            var cpaForm = AddConfigCategory(sidebar, host, categories, "CPA", "CPA 导入接口配置。");
            row = 0;
            AddConfigField(cpaForm, fields, row++, "CPA地址", "cpa_api_url", GetString(cpaMode, "api_url"));
            AddConfigField(cpaForm, fields, row++, "CPA Token", "cpa_api_token", GetString(cpaMode, "api_token"));
            var sub2Form = AddConfigCategory(sidebar, host, categories, "SUB2API", "SUB2API 导入、分组和代理配置。");
            row = 0;
            AddConfigField(sub2Form, fields, row++, "SUB2API地址", "sub2api_url", GetString(sub2api, "api_url"));
            AddConfigField(sub2Form, fields, row++, "SUB2API Token", "sub2api_token", GetString(sub2api, "api_token"));
            AddConfigField(sub2Form, fields, row++, "SUB2API邮箱", "sub2api_email", GetString(sub2api, "email"));
            AddConfigField(sub2Form, fields, row++, "SUB2API密码", "sub2api_password", GetString(sub2api, "password"));
            AddConfigField(sub2Form, fields, row++, "SUB2API分组", "sub2api_group", GetString(sub2api, "group_name"));
            AddConfigField(sub2Form, fields, row++, "SUB2API分组ID", "sub2api_group_ids", GetString(sub2api, "group_ids"));
            AddConfigField(sub2Form, fields, row++, "SUB2API代理", "sub2api_proxy", GetString(sub2api, "proxy_name"));
            AddConfigField(sub2Form, fields, row++, "SUB2API代理ID", "sub2api_proxy_id", GetString(sub2api, "proxy_id"));
            AddConfigField(sub2Form, fields, row++, "SUB2API优先级", "sub2api_priority", GetString(sub2api, "priority"));
            AddConfigField(sub2Form, fields, row++, "SUB2API并发", "sub2api_concurrency", GetString(sub2api, "concurrency"));

            var proxyForm = AddConfigCategory(sidebar, host, categories, "代理 / 支付", "默认代理、PayPal 链接生成代理和直链模式。");
            row = 0;
            AddConfigField(proxyForm, fields, row++, "默认代理", "default_proxy", GetString(proxy, "default"));
            AddConfigField(proxyForm, fields, row++, "PayPal代理", "paypal_proxy", FirstListValue(paypal, "proxies"));
            AddConfigComboField(proxyForm, comboFields, row++, "订单生成地区", "paypal_billing_region", GetBillingRegionCode(paypal), BillingRegionOptions, "DE");
            AddConfigComboField(proxyForm, comboFields, row++, "PayPal直链生成模式", "paypal_link_generation_type", GetLinkGenerationType(paypal), LinkGenerationTypeOptions, "hosted_long_url");

            var paypalBrowserForm = AddConfigCategory(sidebar, host, categories, "PayPal浏览器", "项目内置浏览器支付、身份生成和接码号码池。");
            row = 0;
            AddConfigField(paypalBrowserForm, fields, row++, "启用", "paypal_browser_enabled", FirstNonEmpty(GetString(paypalBrowser, "enabled"), "true"));
            AddConfigField(paypalBrowserForm, fields, row++, "浏览器引擎", "paypal_browser_browser_engine", FirstNonEmpty(GetString(paypalBrowser, "browser_engine"), "camoufox"));
            AddConfigField(paypalBrowserForm, fields, row++, "身份国家", "paypal_browser_country", FirstNonEmpty(GetString(paypalBrowser, "country"), "US"));
            AddConfigField(paypalBrowserForm, fields, row++, "无头模式", "paypal_browser_headless", FirstNonEmpty(GetString(paypalBrowser, "headless"), "true"));
            AddConfigField(paypalBrowserForm, fields, row++, "允许人工人机验证", "paypal_browser_manual_human_verification", FirstNonEmpty(GetString(paypalBrowser, "manual_human_verification"), "false"));
            AddConfigField(paypalBrowserForm, fields, row++, "人机验证等待秒", "paypal_browser_human_verification_timeout", FirstNonEmpty(GetString(paypalBrowser, "human_verification_timeout"), "300"));
            AddConfigField(paypalBrowserForm, fields, row++, "支付邮箱模式", "paypal_browser_email_mode", FirstNonEmpty(GetString(paypalBrowser, "email_mode"), "random"));
            AddConfigField(paypalBrowserForm, fields, row++, "接码号码池", "paypal_browser_phone_pool", FormatPhonePool(paypalBrowser, paypalNocard), multiline: true);

            var gopayForm = AddConfigCategory(sidebar, host, categories, "GoPay", "GoPay 生链、协议支付服务和分阶段代理配置。");
            row = 0;
            AddConfigField(gopayForm, fields, row++, "一键支付模式", "gopay_one_click_mode", FirstNonEmpty(GetString(gopay, "one_click_mode"), "protocol"));
            AddConfigField(gopayForm, fields, row++, "自动打开链接", "gopay_open_link", FirstNonEmpty(GetString(gopay, "open_link"), "true"));
            AddConfigField(gopayForm, fields, row++, "自动生成链接", "gopay_auto_generate", FirstNonEmpty(GetString(gopay, "auto_generate"), "true"));
            AddConfigField(gopayForm, fields, row++, "Provider接口", "gopay_provider_api", FirstNonEmpty(GetString(gopay, "provider_api"), "byte-v-forge"));
            AddConfigField(gopayForm, fields, row++, "PaymentService地址", "gopay_payment_service_addr", FirstNonEmpty(GetString(gopay, "payment_service_addr"), "127.0.0.1:50051"));
            AddConfigField(gopayForm, fields, row++, "grpcurl路径", "gopay_grpcurl_path", FirstNonEmpty(GetString(gopay, "grpcurl_path"), "grpcurl"));
            AddConfigField(gopayForm, fields, row++, "gRPC服务名", "gopay_payment_service", FirstNonEmpty(GetString(gopay, "payment_service"), "payment.PaymentService"));
            AddConfigField(gopayForm, fields, row++, "Proto目录", "gopay_proto_import_path", FirstNonEmpty(GetString(gopay, "proto_import_path"), "services\\gopay-flow\\proto"));
            AddConfigField(gopayForm, fields, row++, "Proto文件", "gopay_proto_path", FirstNonEmpty(GetString(gopay, "proto_path"), "services\\gopay-flow\\proto\\payment.proto"));
            AddConfigField(gopayForm, fields, row++, "Provider超时秒", "gopay_provider_timeout_seconds", FirstNonEmpty(GetString(gopay, "provider_timeout_seconds"), "600"));
            AddConfigField(gopayForm, fields, row++, "服务配置模板", "gopay_provider_config_path", FirstNonEmpty(GetString(gopay, "provider_config_path"), "services\\gopay-flow\\config.gopay.base.json"));
            AddConfigField(gopayForm, fields, row++, "Tokenization", "gopay_tokenization", FirstNonEmpty(GetString(gopay, "tokenization"), "qris"));
            AddConfigField(gopayForm, fields, row++, "GoPay手机号", "gopay_phone", FirstNonEmpty(GetString(gopay, "phone"), GetString(gopay, "phone_number")));
            AddConfigField(gopayForm, fields, row++, "国家区号", "gopay_country_code", FirstNonEmpty(GetString(gopay, "country_code"), "62"));
            AddConfigField(gopayForm, fields, row++, "OTP渠道", "gopay_otp_channel", FirstNonEmpty(GetString(gopay, "otp_channel"), "sms"));
            AddConfigField(gopayForm, fields, row++, "OTP来源", "gopay_otp_source", FirstNonEmpty(GetString(gopay, "otp_source"), FirstNonEmpty(GetString(gopayOtp, "source"), "smsbower")));
            AddConfigField(gopayForm, fields, row++, "GoPay SMSBower服务代码", "gopay_smsbower_service", GetString(gopayOtpSmsBower, "service"));
            AddConfigField(gopayForm, fields, row++, "GoPay SMSBower国家代码", "gopay_smsbower_country", GetString(gopayOtpSmsBower, "country"));
            AddConfigField(gopayForm, fields, row++, "GoPay SMSBower最低价格", "gopay_smsbower_min_price", FirstNonEmpty(GetString(gopayOtpSmsBower, "min_price"), GetString(smsBower, "gopay_min_price")));
            AddConfigField(gopayForm, fields, row++, "GoPay SMSBower最高价格", "gopay_smsbower_max_price", FirstNonEmpty(GetString(gopayOtpSmsBower, "max_price"), GetString(smsBower, "gopay_max_price")));
            AddConfigField(gopayForm, fields, row++, "GoPay PIN", "gopay_pin", GetString(gopay, "pin"));
            AddConfigField(gopayForm, fields, row++, "人工确认后自动确认", "gopay_confirm_after_manual", FirstNonEmpty(GetString(gopay, "confirm_after_manual"), "false"));
            AddConfigField(gopayForm, fields, row++, "MuMu主程序", "gopay_emulator_exe", FirstNonEmpty(GetString(gopay, "emulator_exe"), "D:\\Program Files\\Netease\\MuMuPlayer\\nx_main\\MuMuNxMain.exe"));
            AddConfigField(gopayForm, fields, row++, "ADB路径", "gopay_adb_path", FirstNonEmpty(GetString(gopay, "adb_path"), "D:\\Program Files\\Netease\\MuMuPlayer\\nx_main\\adb.exe"));
            AddConfigField(gopayForm, fields, row++, "ADB Serial", "gopay_adb_serial", FirstNonEmpty(GetString(gopay, "adb_serial"), "emulator-5554"));
            AddConfigField(gopayForm, fields, row++, "ADB Sidecar", "gopay_adb_sidecar_addr", FirstNonEmpty(GetString(gopay, "adb_sidecar_addr"), "127.0.0.1:9999"));
            AddConfigField(gopayForm, fields, row++, "WA换绑启用", "gopay_wa_enabled", FirstNonEmpty(GetString(gopayWaRebind, "enabled"), "false"));
            AddConfigField(gopayForm, fields, row++, "WA支付后换绑", "gopay_wa_rebind_after_payment", FirstNonEmpty(GetString(gopayWaRebind, "rebind_after_payment"), "true"));
            AddConfigField(gopayForm, fields, row++, "GoPay App服务", "gopay_wa_app_service_addr", FirstNonEmpty(GetString(gopayWaRebind, "gopay_app_service_addr"), "127.0.0.1:50060"));
            AddConfigField(gopayForm, fields, row++, "GoPay App Proto目录", "gopay_wa_app_proto_import_path", FirstNonEmpty(GetString(gopayWaRebind, "gopay_app_proto_import_path"), "services\\gopay-app\\proto"));
            AddConfigField(gopayForm, fields, row++, "GoPay App Proto文件", "gopay_wa_app_proto_path", FirstNonEmpty(GetString(gopayWaRebind, "gopay_app_proto_path"), "services\\gopay-app\\proto\\gopay_app.proto"));
            AddConfigField(gopayForm, fields, row++, "WA UserId", "gopay_wa_user_id", FirstNonEmpty(GetString(gopayWaRebind, "user_id"), "local"));
            AddConfigField(gopayForm, fields, row++, "WA支付手机号", "gopay_wa_phone", GetString(gopayWaRebind, "wa_phone"));
            AddConfigField(gopayForm, fields, row++, "换绑目标手机号", "gopay_wa_rebind_phone", GetString(gopayWaRebind, "rebind_phone"));
            AddConfigField(gopayForm, fields, row++, "Checkout代理", "gopay_proxy_checkout", GetString(gopayStageProxies, "checkout"));
            AddConfigField(gopayForm, fields, row++, "Stripe Init代理", "gopay_proxy_stripe_init", GetString(gopayStageProxies, "stripe_init"));
            AddConfigField(gopayForm, fields, row++, "PM Create代理", "gopay_proxy_payment_method", GetString(gopayStageProxies, "payment_method"));
            AddConfigField(gopayForm, fields, row++, "Confirm代理", "gopay_proxy_confirm", GetString(gopayStageProxies, "confirm"));

            var storageForm = AddConfigCategory(sidebar, host, categories, "存储", "Session 输出目录和 SQLite 索引路径。");
            row = 0;
            AddConfigField(storageForm, fields, row++, "Session目录", "output_directory", GetString(output, "directory"));
            AddConfigField(storageForm, fields, row++, "SQLite路径", "sqlite_path", GetString(storage, "sqlite_path"));
            if (categories.Count > 0) SelectConfigCategory(categories, categories[0]);

            var actions = new StackPanel
            {
                Orientation = Orientation.Horizontal,
                HorizontalAlignment = HorizontalAlignment.Right,
                Margin = new Thickness(0, 12, 0, 0)
            };
            var openJsonButton = new Button { Content = "打开JSON", Width = 120 };
            openJsonButton.Click += (_, __) => OpenPath(path);
            var saveButton = new Button { Content = "保存", Width = 72, Style = (Style)FindResource("PrimaryButton") };
            saveButton.Click += (_, __) =>
            {
                email["otp_poll_interval"] = fields["otp_poll_interval"].Text.Trim();
                email["token_file"] = fields["token_file"].Text.Trim();
                email["cfworker_url"] = fields["cfworker_url"].Text.Trim();
                email["cfworker_domain"] = fields["cfworker_domain"].Text.Trim();
                email["cfworker_admin_token"] = fields["cfworker_admin_token"].Text.Trim();
                email["cfworker_api_token"] = fields["cfworker_api_token"].Text.Trim();
                smsBower["api_key"] = fields["smsbower_api_key"].Text.Trim();
                smsBower["service"] = fields["smsbower_service"].Text.Trim();
                var smsBowerCountry = ConfigComboOptionValue(comboFields, "smsbower_country", "38");
                smsBower["country"] = smsBowerCountry.Value;
                smsBower["country_name"] = smsBowerCountry.Metadata;
                smsBower["country_prefix"] = smsBowerCountry.Extra;
                smsBower["gopay_service"] = fields["smsbower_gopay_service"].Text.Trim();
                smsBower["gopay_country"] = fields["smsbower_gopay_country"].Text.Trim();
                smsBower["gopay_min_price"] = fields["smsbower_gopay_min_price"].Text.Trim();
                smsBower["gopay_max_price"] = fields["smsbower_gopay_max_price"].Text.Trim();
                smsBower["min_price"] = fields["smsbower_min_price"].Text.Trim();
                smsBower["max_price"] = fields["smsbower_max_price"].Text.Trim();
                smsBower["target_price"] = fields["smsbower_target_price"].Text.Trim();
                smsBower["pool_size"] = ConfigIntegerValue(fields, "smsbower_pool_size");
                smsBower["sms_timeout"] = ConfigIntegerValue(fields, "smsbower_sms_timeout");
                smsBower["sms_poll_interval"] = ConfigIntegerValue(fields, "smsbower_sms_poll_interval");
                phoneReuse["source"] = ConfigComboValue(comboFields, "phone_source", "smsbower");
                phoneReuse["smsbower"] = smsBower;
                nextSms["api_key"] = fields["nextsms_api_key"].Text.Trim();
                nextSms["endpoint"] = fields["nextsms_endpoint"].Text.Trim();
                nextSms["service"] = fields["nextsms_service"].Text.Trim();
                nextSms["country"] = fields["nextsms_country"].Text.Trim();
                nextSms["pricing_option"] = ConfigIntegerValue(fields, "nextsms_pricing_option");
                nextSms["pool_size"] = ConfigIntegerValue(fields, "nextsms_pool_size");
                nextSms["sms_timeout"] = ConfigIntegerValue(fields, "nextsms_sms_timeout");
                nextSms["sms_poll_interval"] = ConfigIntegerValue(fields, "nextsms_sms_poll_interval");
                nextSms["number_attempts"] = ConfigIntegerValue(fields, "nextsms_number_attempts");
                phoneReuse["nextsms"] = nextSms;
                phoneReuse["max_reuse_count"] = ConfigIntegerValue(fields, "phone_max_reuse_count");
                phoneReuse["send_cooldown_seconds"] = ConfigIntegerValue(fields, "phone_send_cooldown_seconds");
                phoneReuse["send_retry_attempts"] = ConfigIntegerValue(fields, "phone_send_retry_attempts");
                phoneReuse["send_retry_delay_seconds"] = ConfigIntegerValue(fields, "phone_send_retry_delay_seconds");
                phoneReuse["state_file"] = fields["phone_state_file"].Text.Trim();
                phoneReuse["phone_pool"] = ParsePhonePoolLines(fields["phone_pool_lines"].Text);
                codexOauth["registration_timeout"] = ConfigIntegerValue(fields, "codex_registration_timeout");
                codexOauth["allow_passwordless_takeover"] = ConfigBoolValue(fields, "codex_allow_passwordless_takeover", GetBool(codexOauth, "allow_passwordless_takeover", false));
                codexOauth["auto_phone_verification"] = ConfigBoolValue(fields, "codex_auto_phone_verification", GetBool(codexOauth, "auto_phone_verification", false));
                codexOauth["require_registration_refresh_token"] = ConfigBoolValue(fields, "codex_require_registration_refresh_token", GetBool(codexOauth, "require_registration_refresh_token", true));
                codexOauth["require_registration_phone_verification"] = ConfigBoolValue(fields, "codex_require_registration_phone_verification", GetBool(codexOauth, "require_registration_phone_verification", true));
                proxy["default"] = fields["default_proxy"].Text.Trim();
                paypal["proxies"] = new List<object> { fields["paypal_proxy"].Text.Trim() };
                paypal["billing_regions"] = new List<object> { ConfigComboOptionValue(comboFields, "paypal_billing_region", "DE").Value };
                paypal["link_generation_type"] = ConfigComboOptionValue(comboFields, "paypal_link_generation_type", "hosted_long_url").Value;
                paypalBrowser["enabled"] = ConfigBoolValue(fields, "paypal_browser_enabled", GetBool(paypalBrowser, "enabled", true));
                paypalBrowser.Remove("pp_auto_path");
                paypalBrowser.Remove("engine");
                paypalBrowser.Remove("firefox_path");
                paypalBrowser["browser_engine"] = fields["paypal_browser_browser_engine"].Text.Trim();
                paypalBrowser["country"] = fields["paypal_browser_country"].Text.Trim();
                paypalBrowser["headless"] = ConfigBoolValue(fields, "paypal_browser_headless", GetBool(paypalBrowser, "headless", true));
                paypalBrowser["manual_human_verification"] = ConfigBoolValue(fields, "paypal_browser_manual_human_verification", GetBool(paypalBrowser, "manual_human_verification", false));
                paypalBrowser["human_verification_timeout"] = ConfigIntegerValue(fields, "paypal_browser_human_verification_timeout");
                paypalBrowser["email_mode"] = fields["paypal_browser_email_mode"].Text.Trim();
                paypalBrowser["phone_pool"] = ParsePhonePoolLines(fields["paypal_browser_phone_pool"].Text);
                gopay["one_click_mode"] = fields["gopay_one_click_mode"].Text.Trim();
                gopay["open_link"] = ConfigBoolValue(fields, "gopay_open_link", GetBool(gopay, "open_link", true));
                gopay["auto_generate"] = ConfigBoolValue(fields, "gopay_auto_generate", GetBool(gopay, "auto_generate", true));
                gopay["provider_api"] = fields["gopay_provider_api"].Text.Trim();
                gopay["payment_service_addr"] = fields["gopay_payment_service_addr"].Text.Trim();
                gopay["grpcurl_path"] = fields["gopay_grpcurl_path"].Text.Trim();
                gopay["payment_service"] = fields["gopay_payment_service"].Text.Trim();
                gopay["proto_import_path"] = fields["gopay_proto_import_path"].Text.Trim();
                gopay["proto_path"] = fields["gopay_proto_path"].Text.Trim();
                gopay["provider_timeout_seconds"] = ConfigIntegerValue(fields, "gopay_provider_timeout_seconds");
                gopay["provider_config_path"] = fields["gopay_provider_config_path"].Text.Trim();
                gopay["tokenization"] = fields["gopay_tokenization"].Text.Trim();
                gopay["phone"] = fields["gopay_phone"].Text.Trim();
                gopay["country_code"] = fields["gopay_country_code"].Text.Trim();
                gopay["otp_channel"] = fields["gopay_otp_channel"].Text.Trim();
                gopay["otp_source"] = fields["gopay_otp_source"].Text.Trim();
                gopayOtp["source"] = fields["gopay_otp_source"].Text.Trim();
                gopayOtpSmsBower["api_key"] = fields["smsbower_api_key"].Text.Trim();
                gopayOtpSmsBower["endpoint"] = GetString(smsBower, "endpoint");
                gopayOtpSmsBower["service"] = fields["gopay_smsbower_service"].Text.Trim();
                gopayOtpSmsBower["country"] = fields["gopay_smsbower_country"].Text.Trim();
                gopayOtpSmsBower["min_price"] = fields["gopay_smsbower_min_price"].Text.Trim();
                gopayOtpSmsBower["max_price"] = fields["gopay_smsbower_max_price"].Text.Trim();
                gopayOtpSmsBower["sms_timeout"] = ConfigIntegerValue(fields, "smsbower_sms_timeout");
                gopayOtpSmsBower["sms_poll_interval"] = ConfigIntegerValue(fields, "smsbower_sms_poll_interval");
                gopayOtp["smsbower"] = gopayOtpSmsBower;
                gopay["otp"] = gopayOtp;
                gopay["pin"] = fields["gopay_pin"].Text.Trim();
                gopay["confirm_after_manual"] = ConfigBoolValue(fields, "gopay_confirm_after_manual", GetBool(gopay, "confirm_after_manual", false));
                gopay["emulator_exe"] = fields["gopay_emulator_exe"].Text.Trim();
                gopay["adb_path"] = fields["gopay_adb_path"].Text.Trim();
                gopay["adb_serial"] = fields["gopay_adb_serial"].Text.Trim();
                gopay["adb_sidecar_addr"] = fields["gopay_adb_sidecar_addr"].Text.Trim();
                gopayWaRebind["enabled"] = ConfigBoolValue(fields, "gopay_wa_enabled", GetBool(gopayWaRebind, "enabled", false));
                gopayWaRebind["rebind_after_payment"] = ConfigBoolValue(fields, "gopay_wa_rebind_after_payment", GetBool(gopayWaRebind, "rebind_after_payment", true));
                gopayWaRebind["gopay_app_service_addr"] = fields["gopay_wa_app_service_addr"].Text.Trim();
                gopayWaRebind["gopay_app_service"] = FirstNonEmpty(GetString(gopayWaRebind, "gopay_app_service"), "gopay_app.GopayAppService");
                gopayWaRebind["gopay_app_proto_import_path"] = fields["gopay_wa_app_proto_import_path"].Text.Trim();
                gopayWaRebind["gopay_app_proto_path"] = fields["gopay_wa_app_proto_path"].Text.Trim();
                gopayWaRebind["user_id"] = fields["gopay_wa_user_id"].Text.Trim();
                gopayWaRebind["wa_phone"] = fields["gopay_wa_phone"].Text.Trim();
                gopayWaRebind["rebind_phone"] = fields["gopay_wa_rebind_phone"].Text.Trim();
                gopayWaRebind["timeout_seconds"] = ConfigIntegerValue(fields, "gopay_provider_timeout_seconds");
                gopay["wa_rebind"] = gopayWaRebind;
                gopay["billing_regions"] = new List<object> { "ID" };
                gopayStageProxies["checkout"] = fields["gopay_proxy_checkout"].Text.Trim();
                gopayStageProxies["stripe_init"] = fields["gopay_proxy_stripe_init"].Text.Trim();
                gopayStageProxies["payment_method"] = fields["gopay_proxy_payment_method"].Text.Trim();
                gopayStageProxies["confirm"] = fields["gopay_proxy_confirm"].Text.Trim();
                gopay["stage_proxies"] = gopayStageProxies;
                output["directory"] = fields["output_directory"].Text.Trim();
                storage["sqlite_path"] = fields["sqlite_path"].Text.Trim();
                cpaMode["api_url"] = fields["cpa_api_url"].Text.Trim();
                cpaMode["api_token"] = fields["cpa_api_token"].Text.Trim();
                sub2api["api_url"] = fields["sub2api_url"].Text.Trim();
                sub2api["api_token"] = fields["sub2api_token"].Text.Trim();
                sub2api["email"] = fields["sub2api_email"].Text.Trim();
                sub2api["password"] = fields["sub2api_password"].Text.Trim();
                sub2api["group_name"] = fields["sub2api_group"].Text.Trim();
                sub2api["group_ids"] = fields["sub2api_group_ids"].Text.Trim();
                sub2api["proxy_name"] = fields["sub2api_proxy"].Text.Trim();
                sub2api["proxy_id"] = fields["sub2api_proxy_id"].Text.Trim();
                sub2api["priority"] = fields["sub2api_priority"].Text.Trim();
                sub2api["concurrency"] = fields["sub2api_concurrency"].Text.Trim();
                config["email_registration"] = email;
                config["proxy"] = proxy;
                config["paypal"] = paypal;
                config["paypal_browser"] = paypalBrowser;
                config["gopay"] = gopay;
                config["output"] = output;
                config["storage"] = storage;
                config["cpa_mode"] = cpaMode;
                config["sub2api"] = sub2api;
                config["codex_oauth"] = codexOauth;
                config["phone_reuse"] = phoneReuse;
                SaveConfig(path, config);
                ProxyText = fields["default_proxy"].Text.Trim();
                Log("配置已保存。");
                dialog.Close();
            };
            var cancelButton = new Button { Content = "取消", Width = 72 };
            cancelButton.Click += (_, __) => dialog.Close();
            actions.Children.Add(openJsonButton);
            actions.Children.Add(saveButton);
            actions.Children.Add(cancelButton);
            Grid.SetRow(actions, 1);
            root.Children.Add(actions);

            dialog.Content = root;
            dialog.ShowDialog();
        }

        private sealed class ConfigCategory
        {
            public Button Button { get; set; } = new Button();
            public FrameworkElement Panel { get; set; } = new StackPanel();
        }

        private sealed class ConfigComboOption
        {
            public ConfigComboOption(string value, string label, string metadata = "", string extra = "")
            {
                Value = value;
                Label = label;
                Metadata = metadata;
                Extra = extra;
            }

            public string Value { get; }
            public string Label { get; }
            public string Metadata { get; }
            public string Extra { get; }

            public override string ToString()
            {
                return Label;
            }
        }

        private Grid AddConfigCategory(StackPanel sidebar, Grid host, List<ConfigCategory> categories, string title, string description)
        {
            var button = new Button
            {
                Content = title,
                Style = (Style)FindResource("SidebarButton"),
                Width = double.NaN
            };

            var panel = new StackPanel
            {
                Visibility = Visibility.Collapsed
            };
            panel.Children.Add(new TextBlock
            {
                Text = title,
                FontSize = 20,
                FontWeight = FontWeights.SemiBold,
                Foreground = (Brush)FindResource("TextMain"),
                Margin = new Thickness(0, 0, 0, 6)
            });
            panel.Children.Add(new TextBlock
            {
                Text = description,
                TextWrapping = TextWrapping.Wrap,
                Foreground = (Brush)FindResource("TextSub"),
                Margin = new Thickness(0, 0, 0, 18)
            });

            var form = new Grid();
            form.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(168) });
            form.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            panel.Children.Add(form);
            host.Children.Add(panel);
            sidebar.Children.Add(button);

            var category = new ConfigCategory { Button = button, Panel = panel };
            categories.Add(category);
            button.Click += (_, __) => SelectConfigCategory(categories, category);
            return form;
        }

        private void SelectConfigCategory(List<ConfigCategory> categories, ConfigCategory selected)
        {
            foreach (ConfigCategory category in categories)
            {
                bool isSelected = ReferenceEquals(category, selected);
                category.Panel.Visibility = isSelected ? Visibility.Visible : Visibility.Collapsed;
                category.Button.Background = (Brush)FindResource(isSelected ? "PanelHover" : "PanelBg");
                category.Button.BorderBrush = (Brush)FindResource(isSelected ? "Primary" : "Line");
                category.Button.Foreground = (Brush)FindResource("TextMain");
            }
        }

        private void AddConfigField(Grid form, Dictionary<string, TextBox> fields, int row, string label, string key, string value, bool multiline = false)
        {
            form.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            var text = new TextBlock
            {
                Text = label,
                VerticalAlignment = VerticalAlignment.Center,
                Foreground = (System.Windows.Media.Brush)FindResource("TextSub"),
                Margin = new Thickness(0, 0, 12, 10)
            };
            Grid.SetRow(text, row);
            Grid.SetColumn(text, 0);
            form.Children.Add(text);

            var box = new TextBox
            {
                Text = value ?? "",
                Margin = new Thickness(0, 0, 0, 10),
                AcceptsReturn = multiline,
                TextWrapping = multiline ? TextWrapping.NoWrap : TextWrapping.NoWrap,
                VerticalScrollBarVisibility = multiline ? ScrollBarVisibility.Auto : ScrollBarVisibility.Disabled,
                HorizontalScrollBarVisibility = multiline ? ScrollBarVisibility.Auto : ScrollBarVisibility.Disabled,
                MinHeight = multiline ? 112 : 0
            };
            if (multiline)
            {
                box.FontFamily = new System.Windows.Media.FontFamily("Consolas");
            }
            Grid.SetRow(box, row);
            Grid.SetColumn(box, 1);
            form.Children.Add(box);
            fields[key] = box;
        }

        private void AddConfigComboField(Grid form, Dictionary<string, ComboBox> fields, int row, string label, string key, string value, IEnumerable<string> options)
        {
            form.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            var text = new TextBlock
            {
                Text = label,
                VerticalAlignment = VerticalAlignment.Center,
                Foreground = (System.Windows.Media.Brush)FindResource("TextSub"),
                Margin = new Thickness(0, 0, 12, 10)
            };
            Grid.SetRow(text, row);
            Grid.SetColumn(text, 0);
            form.Children.Add(text);

            var combo = new ComboBox
            {
                Margin = new Thickness(0, 0, 0, 10),
                IsEditable = false
            };
            string selected = FirstNonEmpty(value, "smsbower").Trim();
            bool matched = false;
            foreach (string option in options)
            {
                combo.Items.Add(option);
                if (option.Equals(selected, StringComparison.OrdinalIgnoreCase))
                {
                    combo.SelectedItem = option;
                    matched = true;
                }
            }
            if (!matched && combo.Items.Count > 0)
            {
                combo.SelectedIndex = 0;
            }
            Grid.SetRow(combo, row);
            Grid.SetColumn(combo, 1);
            form.Children.Add(combo);
            fields[key] = combo;
        }

        private void AddConfigComboField(Grid form, Dictionary<string, ComboBox> fields, int row, string label, string key, string value, IEnumerable<ConfigComboOption> options, string fallback)
        {
            form.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            var text = new TextBlock
            {
                Text = label,
                VerticalAlignment = VerticalAlignment.Center,
                Foreground = (System.Windows.Media.Brush)FindResource("TextSub"),
                Margin = new Thickness(0, 0, 12, 10)
            };
            Grid.SetRow(text, row);
            Grid.SetColumn(text, 0);
            form.Children.Add(text);

            var combo = new ComboBox
            {
                Margin = new Thickness(0, 0, 0, 10),
                IsEditable = false
            };
            string selected = FirstNonEmpty(value, fallback).Trim();
            bool matched = false;
            foreach (ConfigComboOption option in options)
            {
                combo.Items.Add(option);
                if (option.Value.Equals(selected, StringComparison.OrdinalIgnoreCase))
                {
                    combo.SelectedItem = option;
                    matched = true;
                }
            }
            if (!matched && combo.Items.Count > 0)
            {
                combo.SelectedIndex = 0;
            }
            Grid.SetRow(combo, row);
            Grid.SetColumn(combo, 1);
            form.Children.Add(combo);
            fields[key] = combo;
        }

        private Dictionary<string, object> GetSection(Dictionary<string, object> config, string section)
        {
            if (config.TryGetValue(section, out object value) && value is Dictionary<string, object> map)
            {
                return map;
            }
            var created = new Dictionary<string, object>(StringComparer.OrdinalIgnoreCase);
            config[section] = created;
            return created;
        }

        private Dictionary<string, object> GetChildSection(Dictionary<string, object> parent, string key)
        {
            if (parent.TryGetValue(key, out object value) && value is Dictionary<string, object> map)
            {
                return map;
            }
            var created = new Dictionary<string, object>(StringComparer.OrdinalIgnoreCase);
            parent[key] = created;
            return created;
        }

        private object ConfigIntegerValue(Dictionary<string, TextBox> fields, string key)
        {
            string raw = fields.TryGetValue(key, out TextBox box) ? box.Text.Trim() : "";
            if (int.TryParse(raw, out int value)) return value;
            return raw;
        }

        private string ConfigComboValue(Dictionary<string, ComboBox> fields, string key, string fallback)
        {
            if (!fields.TryGetValue(key, out ComboBox combo)) return fallback;
            return Convert.ToString(combo.SelectedItem) ?? fallback;
        }

        private ConfigComboOption ConfigComboOptionValue(Dictionary<string, ComboBox> fields, string key, string fallback)
        {
            if (fields.TryGetValue(key, out ComboBox combo) && combo.SelectedItem is ConfigComboOption selected)
            {
                return selected;
            }
            return SmsBowerCountryOptions.FirstOrDefault(option => option.Value.Equals(fallback, StringComparison.OrdinalIgnoreCase))
                ?? SmsBowerCountryOptions.First();
        }

        private bool ConfigBoolValue(Dictionary<string, TextBox> fields, string key, bool fallback)
        {
            string raw = fields.TryGetValue(key, out TextBox box) ? box.Text.Trim() : "";
            if (raw.Length == 0) return fallback;
            if (raw.Equals("true", StringComparison.OrdinalIgnoreCase) || raw == "1" || raw.Equals("yes", StringComparison.OrdinalIgnoreCase) || raw.Equals("on", StringComparison.OrdinalIgnoreCase))
            {
                return true;
            }
            if (raw.Equals("false", StringComparison.OrdinalIgnoreCase) || raw == "0" || raw.Equals("no", StringComparison.OrdinalIgnoreCase) || raw.Equals("off", StringComparison.OrdinalIgnoreCase))
            {
                return false;
            }
            return fallback;
        }

        private bool GetBool(Dictionary<string, object> data, string key, bool fallback)
        {
            if (!data.TryGetValue(key, out object value) || value == null) return fallback;
            if (value is bool flag) return flag;
            string raw = Convert.ToString(value) ?? "";
            if (raw.Equals("true", StringComparison.OrdinalIgnoreCase) || raw == "1" || raw.Equals("yes", StringComparison.OrdinalIgnoreCase) || raw.Equals("on", StringComparison.OrdinalIgnoreCase))
            {
                return true;
            }
            if (raw.Equals("false", StringComparison.OrdinalIgnoreCase) || raw == "0" || raw.Equals("no", StringComparison.OrdinalIgnoreCase) || raw.Equals("off", StringComparison.OrdinalIgnoreCase))
            {
                return false;
            }
            return fallback;
        }

        private string FormatPhonePool(Dictionary<string, object> phoneReuse)
        {
            if (!phoneReuse.TryGetValue("phone_pool", out object value) || value is not List<object> list)
            {
                return "";
            }
            var lines = new List<string>();
            foreach (object item in list)
            {
                if (item is not Dictionary<string, object> entry) continue;
                string phone = GetString(entry, "phone").Trim();
                string smsApiUrl = GetString(entry, "sms_api_url").Trim();
                if (phone.Length == 0 || smsApiUrl.Length == 0) continue;
                lines.Add(phone + "----" + smsApiUrl);
            }
            return string.Join(Environment.NewLine, lines);
        }

        private string FormatPhonePool(Dictionary<string, object> primary, Dictionary<string, object> fallback)
        {
            string value = FormatPhonePool(primary);
            return value.Length > 0 ? value : FormatPhonePool(fallback);
        }

        private List<object> ParsePhonePoolLines(string raw)
        {
            var items = new List<object>();
            foreach (string sourceLine in (raw ?? "").Split(new[] { "\r\n", "\n" }, StringSplitOptions.None))
            {
                string line = sourceLine.Trim();
                if (line.Length == 0) continue;
                string phone = "";
                string smsApiUrl = "";
                int marker = line.IndexOf("----", StringComparison.Ordinal);
                if (marker >= 0)
                {
                    phone = line.Substring(0, marker).Trim();
                    smsApiUrl = line.Substring(marker + 4).Trim();
                }
                else
                {
                    Match match = Regex.Match(line, @"^(\+\d+)\s+(\S+)$");
                    if (match.Success)
                    {
                        phone = match.Groups[1].Value.Trim();
                        smsApiUrl = match.Groups[2].Value.Trim();
                    }
                }
                if (phone.Length == 0 || smsApiUrl.Length == 0) continue;
                items.Add(new Dictionary<string, object>
                {
                    ["phone"] = phone,
                    ["sms_api_url"] = smsApiUrl,
                    ["provider"] = "legacy"
                });
            }
            return items;
        }

        private string FirstListValue(Dictionary<string, object> data, string key)
        {
            if (data.TryGetValue(key, out object value) && value is List<object> list && list.Count > 0)
            {
                return Convert.ToString(list[0]) ?? "";
            }
            return "";
        }

        private string GetBillingRegionCode(Dictionary<string, object> paypal)
        {
            string value = FirstListValue(paypal, "billing_regions").Trim();
            if (value.Length == 0)
            {
                value = FirstNonEmpty(GetString(paypal, "billing_region"), GetString(paypal, "billing_country"), "DE");
            }
            value = value.Trim().ToUpperInvariant();
            if (BillingRegionOptions.Any(option => option.Value.Equals(value, StringComparison.OrdinalIgnoreCase)))
            {
                return value;
            }
            return "DE";
        }

        private string GetLinkGenerationType(Dictionary<string, object> paypal)
        {
            string value = GetString(paypal, "link_generation_type").Trim();
            if (LinkGenerationTypeOptions.Any(option => option.Value.Equals(value, StringComparison.OrdinalIgnoreCase)))
            {
                return value;
            }
            return "hosted_long_url";
        }

        private void SaveConfig(string path, Dictionary<string, object> config)
        {
            var options = new JsonSerializerOptions { WriteIndented = true };
            File.WriteAllText(path, JsonSerializer.Serialize(config, options), Encoding.UTF8);
        }

        private void EnsureConfigFile(string path)
        {
            if (File.Exists(path)) return;
            string example = Path.Combine(rootDir, "config.example.json");
            if (File.Exists(example))
            {
                File.Copy(example, path);
            }
            else
            {
                File.WriteAllText(path, "{}", Encoding.UTF8);
            }
        }

        private void AddProxy(List<string> args)
        {
            if (!string.IsNullOrWhiteSpace(ProxyText))
            {
                args.Add("--proxy");
                args.Add(ProxyText.Trim());
            }
        }

        private void AddPaypalOption(List<string> args, string paymentMethod = "paypal")
        {
            if (SkipPaypalLink)
            {
                args.Add("--skip-paypal-link");
                return;
            }
            args.Add("--payment-method");
            args.Add(NormalizePaymentMethod(paymentMethod));
        }

        private string NormalizePaymentMethod(string paymentMethod)
        {
            return string.Equals((paymentMethod ?? "").Trim(), "gopay", StringComparison.OrdinalIgnoreCase) ? "gopay" : "paypal";
        }

        private int CountValue()
        {
            return int.TryParse(CountText, out int value) && value > 0 ? value : 1;
        }

        private int PageSizeValue()
        {
            return int.TryParse(PageSizeText, out int value) && value > 0 ? Math.Min(value, 500) : 25;
        }

        private string GetSessionsDir()
        {
            return Path.Combine(rootDir, "sessions");
        }

        private string GetDatabasePath()
        {
            string configured = ConfigString("storage", "sqlite_path");
            if (configured.Length == 0) return Path.Combine(rootDir, "runtime", "accounts.sqlite3");
            string expanded = Environment.ExpandEnvironmentVariables(configured);
            return Path.IsPathRooted(expanded) ? expanded : Path.Combine(rootDir, expanded);
        }

        private string GetMailboxTokenFile()
        {
            string configured = ConfigString("email_registration", "token_file");
            string expanded = configured.Length > 0 ? Environment.ExpandEnvironmentVariables(configured) : "mailbox_tokens.txt";
            return Path.IsPathRooted(expanded) ? expanded : Path.Combine(rootDir, expanded);
        }

        private string ConfigString(string section, string key)
        {
            string path = Path.Combine(rootDir, "config.json");
            if (!File.Exists(path)) return "";
            try
            {
                Dictionary<string, object> data = ReadJsonObject(path);
                if (!data.TryGetValue(section, out object sectionObj)) return "";
                if (sectionObj is not Dictionary<string, object> sectionData) return "";
                return sectionData.TryGetValue(key, out object value) ? Convert.ToString(value) ?? "" : "";
            }
            catch
            {
                return "";
            }
        }

        private string GetPaypalStatus(Dictionary<string, object> data)
        {
            if (!TryGetMap(data, "paypal", out Dictionary<string, object> paypal) || paypal.Count == 0)
            {
                return "已保存";
            }
            string method = GetString(data, "payment_method");
            if (method.Length == 0) method = GetString(paypal, "payment_method");
            if (method.Length == 0) method = GetString(paypal, "method");
            string prefix = method.Equals("gopay", StringComparison.OrdinalIgnoreCase) ? "GoPay " : "";
            if (IsPaymentLinkMethodMismatch(data, method)) return prefix + "支付失败";
            string status = GetString(data, "paypal_status");
            if (status.Length == 0) status = GetString(paypal, "status");
            if (status.Equals("completed", StringComparison.OrdinalIgnoreCase)) return prefix + "支付完成✅";
            if (status.Equals("pm_created", StringComparison.OrdinalIgnoreCase)) return prefix + "PM已创建✅";
            if (status.Equals("otp_required", StringComparison.OrdinalIgnoreCase)) return prefix + "待输入OTP";
            if (status.Equals("manual_confirmation_required", StringComparison.OrdinalIgnoreCase)) return PaymentPendingStatus(method);
            if (status.Equals("link_ready", StringComparison.OrdinalIgnoreCase)) return PaymentPendingStatus(method);
            string ok = GetString(paypal, "ok").ToLowerInvariant();
            if (ok == "true") return PaymentPendingStatus(method);
            string error = GetString(paypal, "error");
            return error.Length > 0 ? prefix + "失败" : "已保存";
        }

        private string GetPaypalUrl(Dictionary<string, object> data)
        {
            if (!TryGetMap(data, "paypal", out Dictionary<string, object> paypal)) return "";
            return GetString(paypal, "url");
        }

        private bool IsCpaImported(string rawJson)
        {
            if (string.IsNullOrWhiteSpace(rawJson)) return false;
            try
            {
                return IsCpaImported(JsonTextToObject(rawJson));
            }
            catch
            {
                return false;
            }
        }

        private bool IsCpaImported(Dictionary<string, object> data)
        {
            if (!TryGetMap(data, "cpa_import", out Dictionary<string, object> cpaImport)) return false;
            return GetString(cpaImport, "ok").Equals("true", StringComparison.OrdinalIgnoreCase);
        }

        private string GetImportedStatus(string rawJson)
        {
            if (string.IsNullOrWhiteSpace(rawJson)) return "";
            try
            {
                return GetImportedStatus(JsonTextToObject(rawJson));
            }
            catch
            {
                return "";
            }
        }

        private string GetImportedStatus(Dictionary<string, object> data)
        {
            bool cpaImported = IsImportOk(data, "cpa_import");
            bool sub2Imported = IsImportOk(data, "sub2api_import");
            if (cpaImported && sub2Imported) return "已导入CPA/SUB2";
            if (cpaImported) return "已导入CPA";
            if (sub2Imported) return "已导入SUB2";
            return "";
        }

        private bool IsImportOk(Dictionary<string, object> data, string key)
        {
            if (!TryGetMap(data, key, out Dictionary<string, object> importData)) return false;
            return GetString(importData, "ok").Equals("true", StringComparison.OrdinalIgnoreCase);
        }

        private string GetPaypalAmount(string rawJson)
        {
            if (string.IsNullOrWhiteSpace(rawJson)) return "";
            try
            {
                return GetPaypalAmount(JsonTextToObject(rawJson));
            }
            catch
            {
                return "";
            }
        }

        private string GetVerifiedPhone(string rawJson)
        {
            if (string.IsNullOrWhiteSpace(rawJson)) return "";
            try
            {
                return GetVerifiedPhone(JsonTextToObject(rawJson));
            }
            catch
            {
                return "";
            }
        }

        private string GetVerifiedPhone(Dictionary<string, object> data)
        {
            string topLevelPhone = NormalizePhoneText(FirstNonEmpty(GetString(data, "phone"), GetString(data, "phone_number")));
            if (TryGetMap(data, "response", out Dictionary<string, object> response)
                && TryGetMap(response, "phone_verification", out Dictionary<string, object> phoneVerification))
            {
                bool ok = GetString(phoneVerification, "ok").Equals("true", StringComparison.OrdinalIgnoreCase)
                    || GetString(phoneVerification, "ok").Equals("1", StringComparison.OrdinalIgnoreCase);
                string phone = NormalizePhoneText(FirstNonEmpty(
                    GetString(phoneVerification, "phone"),
                    GetString(phoneVerification, "phone_number"),
                    topLevelPhone
                ));
                return ok ? phone : "";
            }

            string refreshTokenStatus = GetString(data, "refresh_token_status");
            bool hasRt = refreshTokenStatus.Equals("oauth_present", StringComparison.OrdinalIgnoreCase)
                || refreshTokenStatus.Equals("legacy_present", StringComparison.OrdinalIgnoreCase);
            return hasRt ? topLevelPhone : "";
        }

        private string NormalizePhoneText(string raw)
        {
            string value = (raw ?? "").Trim();
            if (value.Length == 0) return "";
            string digits = new string(value.Where(char.IsDigit).ToArray());
            if (digits.Length == 0) return "";
            return "+" + digits;
        }

        private string GetPaypalAmount(Dictionary<string, object> data)
        {
            if (!TryGetMap(data, "paypal", out Dictionary<string, object> paypal)) return "";
            string currency = GetString(paypal, "currency").Trim().ToUpperInvariant();
            string rawAmount = FirstNonEmpty(
                GetString(paypal, "amount_due"),
                GetString(paypal, "due"),
                GetString(paypal, "expected_amount")
            );
            if (rawAmount.Length == 0) return "";
            if (!decimal.TryParse(rawAmount, out decimal amount)) return currency.Length > 0 ? rawAmount + " " + currency : rawAmount;
            decimal displayAmount = amount / 100m;
            string text = displayAmount.ToString("0.00");
            return currency.Length > 0 ? text + " " + currency : text;
        }

        private bool IsPaymentLinkMethodMismatch(string rawJson, string paymentMethod)
        {
            if (string.IsNullOrWhiteSpace(rawJson)) return false;
            try
            {
                return IsPaymentLinkMethodMismatch(JsonTextToObject(rawJson), paymentMethod);
            }
            catch
            {
                return false;
            }
        }

        private bool IsPaymentLinkMethodMismatch(Dictionary<string, object> data, string paymentMethod)
        {
            string requested = NormalizePaymentMethod(paymentMethod);
            if (!TryGetMap(data, "paypal", out Dictionary<string, object> paypal) || paypal.Count == 0) return false;
            string savedMethod = NormalizePaymentMethod(FirstNonEmpty(
                GetString(paypal, "payment_method"),
                GetString(paypal, "method"),
                GetString(paypal, "type")
            ));
            bool hasSavedMethod = GetString(paypal, "payment_method").Length > 0
                || GetString(paypal, "method").Length > 0
                || GetString(paypal, "type").Length > 0;
            string currency = GetString(paypal, "currency").Trim().ToLowerInvariant();
            bool hasGoPayType = PaymentMethodTypesContain(paypal, "gopay");
            bool hasPayPalType = PaymentMethodTypesContain(paypal, "paypal");
            if (requested == "gopay")
            {
                return (hasSavedMethod && savedMethod == "paypal")
                    || hasPayPalType
                    || currency == "usd";
            }
            return (hasSavedMethod && savedMethod == "gopay")
                || hasGoPayType
                || currency == "idr";
        }

        private bool PaymentMethodTypesContain(Dictionary<string, object> paypal, string expected)
        {
            if (!paypal.TryGetValue("payment_method_types", out object raw) || raw == null) return false;
            string target = expected.Trim().ToLowerInvariant();
            if (raw is List<object> items)
            {
                return items.Any(item => string.Equals(Convert.ToString(item)?.Trim(), target, StringComparison.OrdinalIgnoreCase));
            }
            return Convert.ToString(raw)?.IndexOf(target, StringComparison.OrdinalIgnoreCase) >= 0;
        }

        private string FirstNonEmpty(params string[] values)
        {
            foreach (string value in values)
            {
                if (!string.IsNullOrWhiteSpace(value)) return value.Trim();
            }
            return "";
        }

        private string GetTimingText(Dictionary<string, object> data)
        {
            if (TryGetMap(data, "pipeline_timing", out Dictionary<string, object> pipeline))
            {
                string total = GetString(pipeline, "total_seconds");
                if (total.Length > 0) return total + "s";
            }
            if (TryGetMap(data, "timing", out Dictionary<string, object> timing))
            {
                string total = GetString(timing, "total_seconds");
                if (total.Length > 0) return total + "s";
            }
            if (TryGetMap(data, "paypal", out Dictionary<string, object> paypal))
            {
                return GetString(paypal, "proxy");
            }
            return "";
        }

        private string DisplayAccountStatus(string status, string paypalOk, string access, string error, string paypalStatus, string refreshTokenStatus, string importedStatus)
        {
            if (!string.IsNullOrWhiteSpace(importedStatus)) return importedStatus;
            bool hasRt = refreshTokenStatus.Equals("oauth_present", StringComparison.OrdinalIgnoreCase)
                || refreshTokenStatus.Equals("legacy_present", StringComparison.OrdinalIgnoreCase);
            if (status.Equals("account_deactivated", StringComparison.OrdinalIgnoreCase)
                || LooksAccountDeactivatedError(error)) return "账号掉号";
            if (hasRt && LooksPhoneVerificationError(error)) return "手机验证";
            if (status.Equals("at_invalid", StringComparison.OrdinalIgnoreCase)
                || status.Equals("access_token_invalid", StringComparison.OrdinalIgnoreCase)
                || status.Equals("token_invalidated", StringComparison.OrdinalIgnoreCase)
                || LooksAtInvalidError(error)) return "AT失效";
            if (paypalStatus.Equals("completed", StringComparison.OrdinalIgnoreCase)) return "支付完成✅";
            if (paypalStatus.Equals("pm_created", StringComparison.OrdinalIgnoreCase)
                || status.Equals("paypal_pm_created", StringComparison.OrdinalIgnoreCase)) return "PM已创建✅";
            if (status.Equals("paypal_failed", StringComparison.OrdinalIgnoreCase) || paypalStatus.Equals("failed", StringComparison.OrdinalIgnoreCase)) return "支付链接失败";
            if (paypalStatus.Equals("manual_confirmation_required", StringComparison.OrdinalIgnoreCase)
                || paypalStatus.Equals("link_ready", StringComparison.OrdinalIgnoreCase)
                || paypalOk == "1"
                || status.Equals("paypal_ready", StringComparison.OrdinalIgnoreCase)) return "待支付";
            if (hasRt && access.Length > 0) return "已注册";
            if (!string.IsNullOrWhiteSpace(error) || status.Equals("failed", StringComparison.OrdinalIgnoreCase)) return "失败";
            return access.Length > 0 ? "已注册" : "待处理";
        }

        private bool LooksAtInvalidError(string error)
        {
            string text = (error ?? "").ToLowerInvariant();
            return text.Contains("token_invalidated")
                || text.Contains("token_expired")
                || text.Contains("authentication token has been invalidated")
                || text.Contains("could not validate your token")
                || LooksPhoneVerificationError(text)
                || LooksAccountDeactivatedError(text)
                || text.Contains("oauth_refresh_http_401");
        }

        private bool LooksPhoneVerificationError(string error)
        {
            string text = (error ?? "").ToLowerInvariant();
            return text.Contains("secondary_phone_verification_required")
                || text.Contains("add_phone_required");
        }

        private bool LooksAccountDeactivatedError(string error)
        {
            string text = (error ?? "").ToLowerInvariant();
            return text.Contains("account_deactivated")
                || text.Contains("account_deatived")
                || text.Contains("deleted or deactivated")
                || text.Contains("account has been deleted")
                || text.Contains("account has been deactivated");
        }

        private string DisplayPayPalStatus(string paypalStatus, string paypalOk, string paypalUrl, string paymentMethod = "")
        {
            string prefix = string.Equals((paymentMethod ?? "").Trim(), "gopay", StringComparison.OrdinalIgnoreCase) ? "GoPay " : "";
            if (paypalStatus.Equals("completed", StringComparison.OrdinalIgnoreCase)) return prefix + "支付完成✅";
            if (paypalStatus.Equals("pm_created", StringComparison.OrdinalIgnoreCase)) return prefix + "PM已创建✅";
            if (paypalStatus.Equals("failed", StringComparison.OrdinalIgnoreCase)) return prefix + "支付失败";
            if (paypalStatus.Equals("otp_required", StringComparison.OrdinalIgnoreCase)) return prefix + "待输入OTP";
            if (paypalStatus.Equals("manual_confirmation_required", StringComparison.OrdinalIgnoreCase)) return PaymentPendingStatus(paymentMethod);
            if (paypalStatus.Equals("link_ready", StringComparison.OrdinalIgnoreCase)) return PaymentPendingStatus(paymentMethod);
            if (paypalOk == "1" && !string.IsNullOrWhiteSpace(paypalUrl)) return PaymentPendingStatus(paymentMethod);
            if (!string.IsNullOrWhiteSpace(paypalUrl)) return PaymentPendingStatus(paymentMethod);
            return "";
        }

        private string PaymentPendingStatus(string paymentMethod)
        {
            return PaymentMethodLabel(paymentMethod) + "待支付";
        }

        private string PaymentMethodLabel(string paymentMethod)
        {
            return NormalizePaymentMethod(paymentMethod).Equals("gopay", StringComparison.OrdinalIgnoreCase) ? "GoPay" : "PayPal";
        }

        private string DisplayRtStatus(string refreshTokenStatus)
        {
            string value = (refreshTokenStatus ?? "").Trim();
            return value.Equals("oauth_present", StringComparison.OrdinalIgnoreCase)
                || value.Equals("legacy_present", StringComparison.OrdinalIgnoreCase)
                ? "已获取"
                : "未获取";
        }

        private string DisplayRefreshTokenStatus(string refreshTokenStatus)
        {
            if (refreshTokenStatus.Equals("oauth_present", StringComparison.OrdinalIgnoreCase)) return "已获取";
            if (refreshTokenStatus.Equals("legacy_present", StringComparison.OrdinalIgnoreCase)) return "旧token";
            if (refreshTokenStatus.Equals("no_rt", StringComparison.OrdinalIgnoreCase)) return "无RT";
            if (refreshTokenStatus.Equals("missing", StringComparison.OrdinalIgnoreCase)) return "缺失";
            return refreshTokenStatus ?? "";
        }

        private string DbTimingText(Dictionary<string, string> data)
        {
            string pipeline = data.TryGetValue("pipeline_total_seconds", out string pipelineSeconds) ? pipelineSeconds : "";
            if (!string.IsNullOrWhiteSpace(pipeline) && pipeline != "0.0" && pipeline != "0") return pipeline + "s";
            string timing = data.TryGetValue("timing_total_seconds", out string timingSeconds) ? timingSeconds : "";
            return string.IsNullOrWhiteSpace(timing) || timing == "0.0" || timing == "0" ? "" : timing + "s";
        }

        private string UnixTimeText(string raw)
        {
            if (!long.TryParse(raw, out long seconds) || seconds <= 0) return "";
            return DateTimeOffset.FromUnixTimeSeconds(seconds).LocalDateTime.ToString("yyyy-MM-dd HH:mm:ss");
        }

        private string OnlyDigits(string raw)
        {
            string digits = new string((raw ?? "").Where(char.IsDigit).ToArray());
            return digits.Length == 0 ? "0" : digits;
        }

        private bool IsUnderDirectory(string path, string directory)
        {
            try
            {
                string fullPath = Path.GetFullPath(path).TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
                string fullDir = Path.GetFullPath(directory).TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
                return fullPath.Equals(fullDir, StringComparison.OrdinalIgnoreCase)
                    || fullPath.StartsWith(fullDir + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase)
                    || fullPath.StartsWith(fullDir + Path.AltDirectorySeparatorChar, StringComparison.OrdinalIgnoreCase);
            }
            catch
            {
                return false;
            }
        }

        private bool TryGetMap(Dictionary<string, object> data, string key, out Dictionary<string, object> map)
        {
            map = null;
            if (!data.TryGetValue(key, out object value)) return false;
            map = value as Dictionary<string, object>;
            return map != null;
        }

        private Dictionary<string, object> ReadJsonObject(string path)
        {
            using JsonDocument document = JsonDocument.Parse(File.ReadAllText(path, Encoding.UTF8));
            return JsonDocumentToObject(document);
        }

        private Dictionary<string, object> JsonTextToObject(string json)
        {
            using JsonDocument document = JsonDocument.Parse(json);
            return JsonDocumentToObject(document);
        }

        private Dictionary<string, object> JsonDocumentToObject(JsonDocument document)
        {
            var output = new Dictionary<string, object>(StringComparer.OrdinalIgnoreCase);
            if (document.RootElement.ValueKind != JsonValueKind.Object) return output;
            foreach (JsonProperty property in document.RootElement.EnumerateObject())
            {
                output[property.Name] = JsonValueToObject(property.Value);
            }
            return output;
        }

        private object JsonValueToObject(JsonElement element)
        {
            switch (element.ValueKind)
            {
                case JsonValueKind.String: return element.GetString() ?? "";
                case JsonValueKind.Number:
                    return element.TryGetInt64(out long n) ? n : element.GetDouble();
                case JsonValueKind.True: return true;
                case JsonValueKind.False: return false;
                case JsonValueKind.Object:
                    var obj = new Dictionary<string, object>(StringComparer.OrdinalIgnoreCase);
                    foreach (JsonProperty property in element.EnumerateObject()) obj[property.Name] = JsonValueToObject(property.Value);
                    return obj;
                case JsonValueKind.Array:
                    return element.EnumerateArray().Select(JsonValueToObject).ToList();
                default: return "";
            }
        }

        private string GetString(Dictionary<string, object> data, string key)
        {
            return data.TryGetValue(key, out object value) && value != null ? Convert.ToString(value) ?? "" : "";
        }

        private string DisplayText(object value)
        {
            if (value is ComboBoxItem item) return Convert.ToString(item.Content) ?? "";
            return Convert.ToString(value) ?? "";
        }

        private string JoinArgs(List<string> args) => string.Join(" ", args.Select(Quote));

        private string Quote(string value)
        {
            value ??= "";
            return value.IndexOfAny(new[] { ' ', '\t', '"', '&', '|' }) < 0 ? value : "\"" + value.Replace("\"", "\\\"") + "\"";
        }

        private string Mask(string value)
        {
            value = (value ?? "").Trim();
            return value.Length <= 12 ? value : value.Substring(0, 6) + "..." + value.Substring(value.Length - 4);
        }

        private string SafeTime(DateTime time) => time.ToString("yyyy-MM-dd HH:mm:ss");

        private void OpenPath(string path)
        {
            try
            {
                if (File.Exists(path) || Directory.Exists(path))
                {
                    if (File.Exists(path) && ShouldOpenWithNotepad(path))
                    {
                        OpenWithNotepad(path);
                        return;
                    }
                    Process.Start(new ProcessStartInfo(path) { UseShellExecute = true });
                    return;
                }
                if (Path.GetExtension(path).Length > 0)
                {
                    string directory = Path.GetDirectoryName(Path.GetFullPath(path)) ?? rootDir;
                    Directory.CreateDirectory(directory);
                    string example = Path.Combine(rootDir, "config.example.json");
                    if (Path.GetFileName(path).Equals("config.json", StringComparison.OrdinalIgnoreCase) && File.Exists(example))
                    {
                        File.Copy(example, path);
                    }
                    else if (!File.Exists(path))
                    {
                        File.WriteAllText(path, "", Encoding.UTF8);
                    }
                    OpenWithNotepad(path);
                    return;
                }
                Directory.CreateDirectory(path);
                Process.Start(new ProcessStartInfo(path) { UseShellExecute = true });
            }
            catch (Exception ex)
            {
                Log("打开失败：" + ex.Message);
            }
        }

        private bool ShouldOpenWithNotepad(string path)
        {
            string extension = Path.GetExtension(path).ToLowerInvariant();
            return extension == ".json" || extension == ".txt" || extension == ".log";
        }

        private void OpenWithNotepad(string path)
        {
            var psi = new ProcessStartInfo("notepad.exe")
            {
                UseShellExecute = false
            };
            psi.ArgumentList.Add(path);
            Process.Start(psi);
        }

        private void OpenUrl(string url)
        {
            try
            {
                if (!Uri.TryCreate(url, UriKind.Absolute, out Uri uri) ||
                    (uri.Scheme != Uri.UriSchemeHttp && uri.Scheme != Uri.UriSchemeHttps))
                {
                    Log("无效链接：" + url);
                    return;
                }
                Process.Start(new ProcessStartInfo(url) { UseShellExecute = true });
            }
            catch (Exception ex)
            {
                Log("打开链接失败：" + ex.Message);
            }
        }

        private void OpenPayPalUrl(string url, string accountEmail = "")
        {
            if (!IsHttpUrl(url))
            {
                Log("无效支付链接：" + url);
                return;
            }
            string chrome = FindChromePath();
            if (chrome.Length == 0)
            {
                Log("未找到 Chrome，使用系统默认浏览器打开支付链接。");
                OpenUrl(url);
                return;
            }
            try
            {
                var psi = new ProcessStartInfo
                {
                    FileName = chrome,
                    UseShellExecute = false
                };
                psi.ArgumentList.Add("--new-window");
                psi.ArgumentList.Add("--incognito");
                psi.ArgumentList.Add(url);
                Process.Start(psi);
                Log("已用 Chrome 无痕窗口打开支付链接。");
            }
            catch (Exception ex)
            {
                Log("Chrome 打开失败：" + ex.Message);
                OpenUrl(url);
            }
        }

        private void CopyPayPalUrl(string url)
        {
            if (!IsHttpUrl(url))
            {
                Log("无效支付链接，无法复制。");
                return;
            }
            try
            {
                Clipboard.SetText(url);
                Log("支付链接已复制。");
            }
            catch (Exception ex)
            {
                Log("复制支付链接失败：" + ex.Message);
            }
        }

        private string FindChromePath()
        {
            string[] candidates =
            {
                Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles), "Google", "Chrome", "Application", "chrome.exe"),
                Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFilesX86), "Google", "Chrome", "Application", "chrome.exe"),
                Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Google", "Chrome", "Application", "chrome.exe")
            };
            return candidates.FirstOrDefault(File.Exists) ?? "";
        }

        private bool IsHttpUrl(string url)
        {
            return Uri.TryCreate(url, UriKind.Absolute, out Uri uri)
                && (uri.Scheme == Uri.UriSchemeHttp || uri.Scheme == Uri.UriSchemeHttps);
        }

        private void ClearLog_Click(object sender, RoutedEventArgs e)
        {
            LogText = "";
        }

        private void Log(string text)
        {
            LogText += "[" + DateTime.Now.ToString("HH:mm:ss") + "] " + text + Environment.NewLine;
        }

        private void UiLog(string text)
        {
            Dispatcher.BeginInvoke(new Action(() => Log(text)), DispatcherPriority.Background);
        }

        private void OnPropertyChanged(string name)
        {
            PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(name));
        }
    }

    public sealed class PoolRow : INotifyPropertyChanged
    {
        private bool isChecked;
        public string Id { get; set; } = "";
        public string CreatedAt { get; set; } = "";
        public string CompletedAt { get; set; } = "";
        public string Identifier { get; set; } = "";
        public string AccountType { get; set; } = "";
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
        public bool IsChecked
        {
            get => isChecked;
            set { isChecked = value; PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(nameof(IsChecked))); }
        }
        public event PropertyChangedEventHandler PropertyChanged;
    }

    public sealed class RegisterOptions
    {
        public string Source { get; set; } = "pool";
        public int Count { get; set; } = 1;
        public int Workers { get; set; } = 4;
        public string PaymentMethod { get; set; } = "paypal";
    }

    public sealed class TaskRow : INotifyPropertyChanged
    {
        private string status = "";
        private string cost = "";
        private string doneAt = "";
        public string Name { get; set; } = "";
        public string Task { get; set; } = "";
        public string Info { get; set; } = "";
        public string Retry { get; set; } = "0";
        public string Status { get => status; set { status = value; Notify(nameof(Status)); } }
        public string Cost { get => cost; set { cost = value; Notify(nameof(Cost)); } }
        public string DoneAt { get => doneAt; set { doneAt = value; Notify(nameof(DoneAt)); } }
        public event PropertyChangedEventHandler PropertyChanged;
        private void Notify(string name) => PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(name));
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

}
