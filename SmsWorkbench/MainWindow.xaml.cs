using System;
using System.Collections.Generic;
using System.Collections.ObjectModel;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Net.Http;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Threading;

namespace SmsWorkbench
{
    public partial class MainWindow : Window, INotifyPropertyChanged
    {
        private static readonly HttpClient httpClient = new HttpClient();
        private readonly string rootDir;
        private readonly ObservableCollection<PoolRow> allRows = new ObservableCollection<PoolRow>();
        private Process runningProcess;
        private int taskSeq = 1;
        private string searchText = "";
        private string countText = "1";
        private string pageSizeText = "25";
        private string proxyText = "";
        private object scopeFilter = "全部";
        private string luckmailTokenText = "";
        private string purchaseProjectText = "openai";
        private string purchaseEmailTypeText = "ms_imap";
        private string purchaseDomainText = "outlook.com";
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
        private string chataiMailboxFilePath = "";

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

        public string LuckmailTokenText
        {
            get => luckmailTokenText;
            set { luckmailTokenText = value ?? ""; OnPropertyChanged(nameof(LuckmailTokenText)); }
        }

        public string PurchaseProjectText
        {
            get => purchaseProjectText;
            set { purchaseProjectText = value ?? ""; OnPropertyChanged(nameof(PurchaseProjectText)); }
        }

        public string PurchaseEmailTypeText
        {
            get => purchaseEmailTypeText;
            set { purchaseEmailTypeText = value ?? ""; OnPropertyChanged(nameof(PurchaseEmailTypeText)); }
        }

        public string PurchaseDomainText
        {
            get => purchaseDomainText;
            set { purchaseDomainText = value ?? ""; OnPropertyChanged(nameof(PurchaseDomainText)); }
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
            PurchaseProjectText = ConfigString("email_registration", "luckmail_purchase_project_code");
            if (PurchaseProjectText.Length == 0) PurchaseProjectText = "openai";
            PurchaseEmailTypeText = ConfigString("email_registration", "luckmail_purchase_email_type");
            if (PurchaseEmailTypeText.Length == 0) PurchaseEmailTypeText = "ms_imap";
            PurchaseDomainText = ConfigString("email_registration", "luckmail_purchase_domain");
            if (PurchaseDomainText.Length == 0) PurchaseDomainText = "outlook.com";
            ProxyText = ConfigString("proxy", "default");
            RefreshPools();
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
            int mailboxes = allRows.Count(r => r.AccountType.Contains("邮箱池") || r.AccountType.Contains("Chatai"));
            int registered = allRows.Count(IsRegisteredRow);
            int paypal = allRows.Count(IsPayPalCompletedRow);
            int attention = allRows.Count(r => r.Status.Contains("待") || r.Status.Contains("缺") || r.Status.Contains("失败"));
            TotalCountText = allRows.Count.ToString();
            MailboxCountText = mailboxes.ToString();
            RegisteredCountText = registered.ToString();
            PaypalCountText = paypal.ToString();
            AttentionCountText = attention.ToString();
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
                    string[] parts = line.Split(new[] { "----" }, StringSplitOptions.None);
                    if (parts.Length < 4) continue;
                    allRows.Add(new PoolRow
                    {
                        Id = "M" + (i + 1),
                        CreatedAt = SafeTime(File.GetLastWriteTime(path)),
                        CompletedAt = SafeTime(File.GetLastWriteTime(path)),
                        Identifier = parts[0].Trim(),
                        AccountType = "Chatai邮箱池",
                        Status = "已授权",
                        RefreshToken = Mask(parts[3].Trim()),
                        Notes = path,
                        SourcePath = path,
                        RawLine = line,
                        ClientId = parts[2].Trim(),
                        RawRefreshToken = parts[3].Trim(),
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
                string sql = "SELECT id,email,access_token,status,error,paypal_ok,paypal_url,paypal_status,refresh_token_status,json_path,raw_json,pipeline_total_seconds,timing_total_seconds,created_at,updated_at FROM accounts ORDER BY updated_at DESC";
                var rows = SqliteNative.Query(dbPath, sql);
                if (rows.Count == 0) return false;
                foreach (Dictionary<string, string> data in rows)
                {
                    string status = data.TryGetValue("status", out string rawStatus) ? rawStatus : "";
                    string error = data.TryGetValue("error", out string rawError) ? rawError : "";
                    string paypalOk = data.TryGetValue("paypal_ok", out string rawPaypalOk) ? rawPaypalOk : "";
                    string paypalUrl = data.TryGetValue("paypal_url", out string rawPaypalUrl) ? rawPaypalUrl : "";
                    string paypalStatus = data.TryGetValue("paypal_status", out string rawPaypalStatus) ? rawPaypalStatus : "";
                    string refreshTokenStatus = data.TryGetValue("refresh_token_status", out string rawRefreshTokenStatus) ? rawRefreshTokenStatus : "";
                    string access = data.TryGetValue("access_token", out string rawAccess) ? rawAccess : "";
                    string jsonPath = data.TryGetValue("json_path", out string rawJsonPath) ? rawJsonPath : "";
                    string rawJson = data.TryGetValue("raw_json", out string rawRawJson) ? rawRawJson : "";
                    string paypalAmount = GetPaypalAmount(rawJson);
                    string importedStatus = GetImportedStatus(rawJson);
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
                        PayPalStatus = DisplayPayPalStatus(paypalStatus, paypalOk, paypalUrl),
                        PayPalAmount = paypalAmount,
                        RefreshTokenStatus = DisplayRefreshTokenStatus(refreshTokenStatus),
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
                        TryReadMailboxFromRawJson(JsonSerializer.Serialize(data), out string mailboxProvider, out string mailboxClientId, out string mailboxRefreshToken, out string mailboxLine);
                        string timing = GetTimingText(data);
                        allRows.Add(new PoolRow
                        {
                            Id = "S" + (allRows.Count + 1),
                            CreatedAt = SafeTime(File.GetCreationTime(path)),
                            CompletedAt = SafeTime(File.GetLastWriteTime(path)),
                            Identifier = email,
                            AccountType = mailboxProvider.Equals("cfworker", StringComparison.OrdinalIgnoreCase) ? "Session/CFWorker" : "Session",
                            Status = importedStatus.Length > 0 ? importedStatus : access.Length > 0 ? paypalStatus : "缺access_token",
                            PayPalStatus = paypalStatus,
                            PayPalAmount = paypalAmount,
                            RefreshTokenStatus = DisplayRefreshTokenStatus(refreshTokenStatus),
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

        private void BuyAndRegister_Click(object sender, RoutedEventArgs e)
        {
            var args = new List<string> { "--buy-luckmail-mailbox", "--count", CountValue().ToString(), "--workers", "4" };
            AddPurchaseArgs(args);
            AddProxy(args);
            AddPaypalOption(args);
            RunBackend("购买邮箱并注册", args);
        }

        private void RegisterFromPool_Click(object sender, RoutedEventArgs e)
        {
            var args = new List<string> { "--count", CountValue().ToString(), "--workers", "4" };
            AddProxy(args);
            AddPaypalOption(args);
            RunBackend("邮箱池注册", args);
        }

        private void RegisterWithToken_Click(object sender, RoutedEventArgs e)
        {
            string token = (LuckmailTokenText ?? "").Trim();
            if (token.Length == 0)
            {
                MessageBox.Show("请先输入 LuckMail 邮箱 token。", "缺少 token", MessageBoxButton.OK, MessageBoxImage.Information);
                return;
            }
            var args = new List<string> { "--luckmail-token", token };
            AddProxy(args);
            AddPaypalOption(args);
            RunBackend("Token注册", args);
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
            if (!IsCfWorkerRow(row) && (string.IsNullOrWhiteSpace(row.RawRefreshToken) || string.IsNullOrWhiteSpace(row.ClientId)))
            {
                MessageBox.Show("选中记录不是 Chatai 或 edu.liziai.cloud 邮箱。", "格式不匹配", MessageBoxButton.OK, MessageBoxImage.Information);
                return;
            }
            ShowInboxDialog(row);
        }

        private void OneClickRegister_Click(object sender, RoutedEventArgs e)
        {
            RegisterOptions options = ShowRegisterOptionsDialog();
            if (options == null) return;

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
                AddProxy(cfArgs);
                AddPaypalOption(cfArgs);
                RunBackend("CFWorker邮箱注册+支付链接", cfArgs);
                return;
            }

            string mailboxArg = "--chatai-mailbox-file";
            string mailboxFile = GetChataiMailboxFilePath();
            int count = options.Count;
            string taskName = "一键注册+支付链接";
            if (TryCreateSelectedMailboxFile(out string selectedArg, out string selectedFile, out int selectedCount))
            {
                mailboxArg = selectedArg;
                mailboxFile = selectedFile;
                count = selectedCount;
                taskName = "选中邮箱注册+支付链接";
            }
            if (string.IsNullOrWhiteSpace(mailboxFile) || !File.Exists(mailboxFile))
            {
                MessageBox.Show("未找到 Chatai 邮箱文件，请先导入。", "缺少邮箱文件", MessageBoxButton.OK, MessageBoxImage.Information);
                return;
            }
            var args = new List<string> { mailboxArg, mailboxFile, "--count", count.ToString(), "--workers", options.Workers.ToString() };
            AddProxy(args);
            AddPaypalOption(args);
            RunBackend(taskName, args);
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
                MessageBox.Show("请先勾选或选择账号记录。", "未选择账号", MessageBoxButton.OK, MessageBoxImage.Information);
                return;
            }
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
            AddProxy(args);
            RunBackend("一键支付 (" + rows.Count + ")", args);
        }

        private RegisterOptions ShowRegisterOptionsDialog()
        {
            var dialog = new Window
            {
                Title = "一键注册+支付链接",
                Owner = this,
                Width = 420,
                Height = 240,
                MinWidth = 380,
                MinHeight = 220,
                WindowStartupLocation = WindowStartupLocation.CenterOwner,
                Background = (System.Windows.Media.Brush)FindResource("AppBg")
            };

            var root = new Grid { Margin = new Thickness(14) };
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(110) });
            root.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });

            var sourceLabel = new TextBlock { Text = "邮箱来源", VerticalAlignment = VerticalAlignment.Center, Margin = new Thickness(0, 0, 10, 10), Foreground = (System.Windows.Media.Brush)FindResource("TextSub") };
            var sourceBox = new ComboBox { Margin = new Thickness(0, 0, 0, 10) };
            sourceBox.Items.Add(new ComboBoxItem { Content = "Chatai/邮箱池", Tag = "pool" });
            sourceBox.Items.Add(new ComboBoxItem { Content = "edu.liziai.cloud (CFWorker)", Tag = "cfworker" });
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
            var workerBox = new TextBox { Text = "4", Margin = new Thickness(0, 0, 0, 10) };
            Grid.SetRow(workerLabel, 2);
            Grid.SetColumn(workerLabel, 0);
            Grid.SetRow(workerBox, 2);
            Grid.SetColumn(workerBox, 1);
            root.Children.Add(workerLabel);
            root.Children.Add(workerBox);

            var actions = new StackPanel { Orientation = Orientation.Horizontal, HorizontalAlignment = HorizontalAlignment.Right, Margin = new Thickness(0, 10, 0, 0) };
            var ok = new Button { Content = "开始", Width = 72, Style = (Style)FindResource("PrimaryButton") };
            var cancel = new Button { Content = "取消", Width = 72 };
            actions.Children.Add(ok);
            actions.Children.Add(cancel);
            Grid.SetRow(actions, 3);
            Grid.SetColumnSpan(actions, 2);
            root.Children.Add(actions);

            RegisterOptions selected = null;
            ok.Click += (_, __) =>
            {
                int count = ParsePositiveInt(countBox.Text, 1, 200, 1);
                int workers = ParsePositiveInt(workerBox.Text, 1, 4, 4);
                selected = new RegisterOptions
                {
                    Source = ((sourceBox.SelectedItem as ComboBoxItem)?.Tag as string) ?? "pool",
                    Count = count,
                    Workers = workers
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
                string clientId = JsonString(mailbox, "token");
                string provider = JsonString(mailbox, "provider");
                if (email.Length == 0) return "";
                if (provider.Equals("cfworker", StringComparison.OrdinalIgnoreCase))
                {
                    return "cfworker://" + email;
                }
                if (refreshToken.Length == 0) return "";
                if (provider.Equals("chatai", StringComparison.OrdinalIgnoreCase) || clientId.Length > 0)
                {
                    return email + "----" + password + "----" + clientId + "----" + refreshToken;
                }
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
                clientId = JsonString(mailbox, "token");
                provider = JsonString(mailbox, "provider");
                if (email.Length == 0) return false;

                if (provider.Equals("cfworker", StringComparison.OrdinalIgnoreCase))
                {
                    mailboxLine = "cfworker://" + email;
                    return true;
                }

                if (refreshToken.Length == 0) return false;

                if (provider.Equals("chatai", StringComparison.OrdinalIgnoreCase) || clientId.Length > 0)
                {
                    mailboxLine = email + "----" + password + "----" + clientId + "----" + refreshToken;
                }
                else
                {
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

            runningProcess = new Process { StartInfo = psi, EnableRaisingEvents = true };
            runningProcess.OutputDataReceived += (_, ev) => { if (ev.Data != null) UiLog(ev.Data); };
            runningProcess.ErrorDataReceived += (_, ev) => { if (ev.Data != null) UiLog(ev.Data); };
            runningProcess.Exited += (_, __) =>
            {
                Dispatcher.BeginInvoke(new Action(() =>
                {
                    task.Status = runningProcess.ExitCode == 0 ? "完成" : "失败";
                    task.Cost = ((int)(DateTime.Now - started).TotalSeconds).ToString();
                    task.DoneAt = SafeTime(DateTime.Now);
                    StatusText = taskName + " 已结束";
                    RefreshPools();
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

        private void DeleteSelected_Click(object sender, RoutedEventArgs e)
        {
            var selected = allRows.Where(r => r.IsChecked).ToList();
            if (selected.Count == 0 && SelectedRow != null) selected.Add(SelectedRow);
            if (selected.Count == 0)
            {
                MessageBox.Show("请先勾选或选择要删除的记录。", "提示", MessageBoxButton.OK, MessageBoxImage.Information);
                return;
            }
            if (MessageBox.Show("确定删除选中的 " + selected.Count + " 条记录？", "确认", MessageBoxButton.YesNo, MessageBoxImage.Warning) != MessageBoxResult.Yes) return;
            foreach (PoolRow row in selected) DeleteRow(row);
            RefreshPools();
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

        private void ToggleSidebar_Click(object sender, RoutedEventArgs e)
        {
            sidebarCollapsed = !sidebarCollapsed;
            SidebarColumn.Width = new GridLength(sidebarCollapsed ? 64 : 256);
            SidebarHeaderPanel.Margin = sidebarCollapsed ? new Thickness(10, 14, 10, 10) : new Thickness(12, 14, 12, 14);
            SidebarToggleButton.Content = sidebarCollapsed ? "›" : "‹";
            SidebarToggleButton.Width = sidebarCollapsed ? 34 : 32;
            SidebarBrandColumn.Width = sidebarCollapsed ? new GridLength(0) : new GridLength(38);
            SidebarTextColumn.Width = sidebarCollapsed ? new GridLength(0) : new GridLength(1, GridUnitType.Star);
            SidebarToggleColumn.Width = sidebarCollapsed ? new GridLength(1, GridUnitType.Star) : new GridLength(40);
            SidebarBrand.Visibility = sidebarCollapsed ? Visibility.Collapsed : Visibility.Visible;
            SidebarHeaderText.Visibility = sidebarCollapsed ? Visibility.Collapsed : Visibility.Visible;
            SidebarNavScroll.Visibility = Visibility.Visible;
            SidebarBottomActions.Visibility = Visibility.Visible;
            SidebarNavStack.Margin = sidebarCollapsed ? new Thickness(10, 0, 10, 14) : new Thickness(12, 0, 12, 14);
            SidebarBottomActions.Margin = sidebarCollapsed ? new Thickness(10, 0, 10, 14) : new Thickness(12, 0, 12, 14);
            SetSidebarCompact(SidebarNavScroll, sidebarCollapsed);
            SetSidebarCompact(SidebarBottomActions, sidebarCollapsed);
        }

        private void SetSidebarCompact(DependencyObject root, bool compact)
        {
            Style buttonStyle = (Style)FindResource(compact ? "SidebarIconButton" : "SidebarButton");
            SetSidebarCompactRecursive(root, compact, buttonStyle);
        }

        private void SetSidebarCompactRecursive(DependencyObject node, bool compact, Style buttonStyle)
        {
            if (node is FrameworkElement element)
            {
                string tag = element.Tag as string ?? "";
                if (tag == "SidebarText" || tag == "SidebarSection")
                {
                    element.Visibility = compact ? Visibility.Collapsed : Visibility.Visible;
                }
                if (node is Button button && button != SidebarToggleButton)
                {
                    button.Style = buttonStyle;
                }
            }

            int children = VisualTreeHelper.GetChildrenCount(node);
            for (int i = 0; i < children; i++)
            {
                SetSidebarCompactRecursive(VisualTreeHelper.GetChild(node, i), compact, buttonStyle);
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
                MessageBox.Show("请先勾选或选择账号记录。", "未选择账号", MessageBoxButton.OK, MessageBoxImage.Information);
                return;
            }

            if (rows.Count == 1)
            {
                PoolRow row = rows[0];
                var singleArgs = new List<string> { "--email", row.Identifier, "--regenerate-paypal-link", "--workers", "4" };
                AddSessionFileArg(singleArgs, row);
                RunBackend("重新生成PayPal链接", singleArgs);
                return;
            }

            string emailFile = Path.Combine(Path.GetTempPath(), "paypal_regen_emails_" + DateTime.Now.ToString("yyyyMMdd_HHmmss") + ".txt");
            File.WriteAllLines(emailFile, rows.Select(r => r.Identifier.Trim()), new UTF8Encoding(false));
            var args = new List<string> { "--regenerate-paypal-link", "--email-file", emailFile, "--workers", "4" };
            RunBackend("批量重新生成PayPal链接 (" + rows.Count + ")", args);
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

        private void ImportPaidCpa_Click(object sender, RoutedEventArgs e)
        {
            string target = ShowImportTargetDialog("一键导入");
            if (target.Length == 0) return;

            var selected = SelectedRowsOrCurrent()
                .Where(IsPayPalCompletedRow)
                .Where(r => !string.IsNullOrWhiteSpace(r.Identifier))
                .GroupBy(r => r.Identifier.Trim().ToLowerInvariant())
                .Select(g => g.First())
                .ToList();
            var rows = selected.Count > 0
                ? selected
                : allRows.Where(IsPayPalCompletedRow)
                    .Where(r => !string.IsNullOrWhiteSpace(r.Identifier))
                    .GroupBy(r => r.Identifier.Trim().ToLowerInvariant())
                    .Select(g => g.First())
                    .ToList();

            if (rows.Count == 0)
            {
                MessageBox.Show("没有找到支付完成的账号。", "一键导入", MessageBoxButton.OK, MessageBoxImage.Information);
                return;
            }

            string emailFile = Path.Combine(Path.GetTempPath(), "paid_import_emails_" + DateTime.Now.ToString("yyyyMMdd_HHmmss") + ".txt");
            File.WriteAllLines(emailFile, rows.Select(r => r.Identifier.Trim()), new UTF8Encoding(false));
            var args = new List<string> { "--import-cpa", "--email-file", emailFile, "--workers", "4", "--refresh-timeout", "60" };
            AddImportTargetArg(args, target);
            RunBackend("一键导入" + ImportTargetLabel(target) + " (" + rows.Count + ")", args);
        }

        private void ReimportCpa401_Click(object sender, RoutedEventArgs e)
        {
            string target = ShowImportTargetDialog("一键重导");
            if (target.Length == 0) return;

            string mailboxFile = GetChataiMailboxFilePath();
            if (string.IsNullOrWhiteSpace(mailboxFile) || !File.Exists(mailboxFile))
            {
                MessageBox.Show("未找到 Chatai 邮箱文件，请先导入。", "缺少邮箱文件", MessageBoxButton.OK, MessageBoxImage.Information);
                return;
            }

            var args = new List<string>
            {
                "--reimport-cpa-401-survivors",
                "--chatai-mailbox-file",
                mailboxFile,
                "--workers",
                "1",
                "--refresh-timeout",
                "180"
            };
            AddImportTargetArg(args, target);
            AddProxy(args);
            RunBackend("一键重导" + ImportTargetLabel(target), args);
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
            combo.Items.Add(new ComboBoxItem { Content = "SUB2API", Tag = "sub2api" });
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
            args.Add((target ?? "").Equals("sub2api", StringComparison.OrdinalIgnoreCase) ? "sub2api" : "cpa");
        }

        private string ImportTargetLabel(string target)
        {
            return (target ?? "").Equals("sub2api", StringComparison.OrdinalIgnoreCase) ? "SUB2API" : "CPA";
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
                    ["scope"] = "offline_access https://graph.microsoft.com/Mail.Read"
                };

                try
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
                "/admin/emails?page=1&domain=" + Uri.EscapeDataString(domain),
                "/admin/emails?page=1",
                "/api/messages?email=" + encoded + "&limit=" + limit,
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
                if (items.Count > 0 || path.StartsWith("/admin/emails", StringComparison.OrdinalIgnoreCase) || LooksEmptyMessageList(doc.RootElement)) return items;
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
            var dialog = new Window
            {
                Title = "账号详情 - " + row.Identifier,
                Owner = this,
                Width = 940,
                Height = 720,
                MinWidth = 760,
                MinHeight = 560,
                WindowStartupLocation = WindowStartupLocation.CenterOwner,
                Background = (System.Windows.Media.Brush)FindResource("AppBg")
            };

            var root = new Grid { Margin = new Thickness(12) };
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });

            var title = new TextBlock
            {
                Text = row.Identifier,
                FontSize = 18,
                FontWeight = FontWeights.SemiBold,
                Foreground = (System.Windows.Media.Brush)FindResource("TextMain"),
                Margin = new Thickness(0, 0, 0, 10)
            };
            Grid.SetRow(title, 0);
            root.Children.Add(title);

            var summary = new Grid
            {
                Margin = new Thickness(0, 0, 0, 10),
                Background = (System.Windows.Media.Brush)FindResource("PanelBg")
            };
            summary.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(110) });
            summary.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            for (int i = 0; i < 6; i++) summary.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            AddDetailRow(summary, 0, "邮箱", row.Identifier);
            AddDetailRow(summary, 1, "支付状态", row.PayPalStatus);
            AddDetailRow(summary, 2, "支付金额", row.PayPalAmount);
            AddDetailRow(summary, 3, "Refresh", row.RefreshTokenStatus);
            AddDetailRow(summary, 4, "更新时间", row.CompletedAt);
            AddDetailRow(summary, 5, "支付订阅链接", paypalUrl);
            Grid.SetRow(summary, 1);
            root.Children.Add(summary);

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
                MinHeight = 260,
                Background = (System.Windows.Media.Brush)FindResource("PanelBg")
            };
            Grid.SetRow(text, 2);
            root.Children.Add(text);

            var actions = new StackPanel
            {
                Orientation = Orientation.Horizontal,
                HorizontalAlignment = HorizontalAlignment.Right,
                Margin = new Thickness(0, 10, 0, 0)
            };
            var openPayPalButton = new Button { Content = "打开支付链接", Width = 108, IsEnabled = !string.IsNullOrWhiteSpace(paypalUrl) };
            openPayPalButton.Click += (_, __) => OpenPayPalUrl(paypalUrl, row.Identifier);
            var copyPayPalButton = new Button { Content = "复制支付链接", Width = 108, IsEnabled = !string.IsNullOrWhiteSpace(paypalUrl) };
            copyPayPalButton.Click += (_, __) => CopyPayPalUrl(paypalUrl);
            var markPayPalCompleteButton = new Button { Content = "标记支付完成", Width = 112 };
            markPayPalCompleteButton.Click += (_, __) =>
            {
                MarkPayPalComplete(row);
                dialog.Close();
            };
            var openButton = new Button { Content = "打开源文件", Width = 96 };
            openButton.Click += (_, __) => OpenAccountJson(row);
            var closeButton = new Button { Content = "关闭", Width = 72 };
            closeButton.Click += (_, __) => dialog.Close();
            actions.Children.Add(openPayPalButton);
            actions.Children.Add(copyPayPalButton);
            actions.Children.Add(markPayPalCompleteButton);
            actions.Children.Add(openButton);
            actions.Children.Add(closeButton);
            Grid.SetRow(actions, 3);
            root.Children.Add(actions);

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
            var storage = GetSection(config, "storage");
            var output = GetSection(config, "output");
            var cpaMode = GetSection(config, "cpa_mode");
            var sub2api = GetSection(config, "sub2api");

            var dialog = new Window
            {
                Title = "配置",
                Owner = this,
                Width = 720,
                Height = 620,
                MinWidth = 640,
                MinHeight = 520,
                WindowStartupLocation = WindowStartupLocation.CenterOwner,
                Background = (System.Windows.Media.Brush)FindResource("AppBg")
            };

            var root = new Grid { Margin = new Thickness(14) };
            root.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });

            var form = new Grid();
            form.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(170) });
            form.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });

            var fields = new Dictionary<string, TextBox>();
            int row = 0;
            AddConfigField(form, fields, row++, "LuckMail API Key", "luckmail_api_key", GetString(email, "luckmail_api_key"));
            AddConfigField(form, fields, row++, "LuckMail Base URL", "luckmail_base_url", GetString(email, "luckmail_base_url"));
            AddConfigField(form, fields, row++, "购买项目", "luckmail_purchase_project_code", GetString(email, "luckmail_purchase_project_code"));
            AddConfigField(form, fields, row++, "邮箱类型", "luckmail_purchase_email_type", GetString(email, "luckmail_purchase_email_type"));
            AddConfigField(form, fields, row++, "邮箱域名", "luckmail_purchase_domain", GetString(email, "luckmail_purchase_domain"));
            AddConfigField(form, fields, row++, "OTP轮询间隔秒", "otp_poll_interval", GetString(email, "otp_poll_interval"));
            AddConfigField(form, fields, row++, "邮箱池文件", "token_file", GetString(email, "token_file"));
            AddConfigField(form, fields, row++, "CFWorker URL", "cfworker_url", GetString(email, "cfworker_url"));
            AddConfigField(form, fields, row++, "CFWorker 域名", "cfworker_domain", GetString(email, "cfworker_domain"));
            AddConfigField(form, fields, row++, "CFWorker Admin Token", "cfworker_admin_token", GetString(email, "cfworker_admin_token"));
            AddConfigField(form, fields, row++, "Cloudflare API Token", "cfworker_api_token", GetString(email, "cfworker_api_token"));
            AddConfigField(form, fields, row++, "默认代理", "default_proxy", GetString(proxy, "default"));
            AddConfigField(form, fields, row++, "PayPal代理", "paypal_proxy", FirstListValue(paypal, "proxies"));
            AddConfigField(form, fields, row++, "Session目录", "output_directory", GetString(output, "directory"));
            AddConfigField(form, fields, row++, "SQLite路径", "sqlite_path", GetString(storage, "sqlite_path"));
            AddConfigField(form, fields, row++, "CPA地址", "cpa_api_url", GetString(cpaMode, "api_url"));
            AddConfigField(form, fields, row++, "CPA Token", "cpa_api_token", GetString(cpaMode, "api_token"));
            AddConfigField(form, fields, row++, "SUB2API地址", "sub2api_url", GetString(sub2api, "api_url"));
            AddConfigField(form, fields, row++, "SUB2API Token", "sub2api_token", GetString(sub2api, "api_token"));
            AddConfigField(form, fields, row++, "SUB2API邮箱", "sub2api_email", GetString(sub2api, "email"));
            AddConfigField(form, fields, row++, "SUB2API密码", "sub2api_password", GetString(sub2api, "password"));
            AddConfigField(form, fields, row++, "SUB2API分组", "sub2api_group", GetString(sub2api, "group_name"));
            AddConfigField(form, fields, row++, "SUB2API分组ID", "sub2api_group_ids", GetString(sub2api, "group_ids"));
            AddConfigField(form, fields, row++, "SUB2API代理", "sub2api_proxy", GetString(sub2api, "proxy_name"));
            AddConfigField(form, fields, row++, "SUB2API代理ID", "sub2api_proxy_id", GetString(sub2api, "proxy_id"));
            AddConfigField(form, fields, row++, "SUB2API优先级", "sub2api_priority", GetString(sub2api, "priority"));
            AddConfigField(form, fields, row++, "SUB2API并发", "sub2api_concurrency", GetString(sub2api, "concurrency"));

            var scroll = new ScrollViewer { Content = form, VerticalScrollBarVisibility = ScrollBarVisibility.Auto };
            Grid.SetRow(scroll, 0);
            root.Children.Add(scroll);

            var actions = new StackPanel
            {
                Orientation = Orientation.Horizontal,
                HorizontalAlignment = HorizontalAlignment.Right,
                Margin = new Thickness(0, 12, 0, 0)
            };
            var openJsonButton = new Button { Content = "打开JSON", Width = 88 };
            openJsonButton.Click += (_, __) => OpenPath(path);
            var saveButton = new Button { Content = "保存", Width = 72, Style = (Style)FindResource("PrimaryButton") };
            saveButton.Click += (_, __) =>
            {
                email["luckmail_api_key"] = fields["luckmail_api_key"].Text.Trim();
                email["luckmail_base_url"] = fields["luckmail_base_url"].Text.Trim();
                email["luckmail_purchase_project_code"] = fields["luckmail_purchase_project_code"].Text.Trim();
                email["luckmail_purchase_email_type"] = fields["luckmail_purchase_email_type"].Text.Trim();
                email["luckmail_purchase_domain"] = fields["luckmail_purchase_domain"].Text.Trim();
                email["otp_poll_interval"] = fields["otp_poll_interval"].Text.Trim();
                email["token_file"] = fields["token_file"].Text.Trim();
                email["cfworker_url"] = fields["cfworker_url"].Text.Trim();
                email["cfworker_domain"] = fields["cfworker_domain"].Text.Trim();
                email["cfworker_admin_token"] = fields["cfworker_admin_token"].Text.Trim();
                email["cfworker_api_token"] = fields["cfworker_api_token"].Text.Trim();
                proxy["default"] = fields["default_proxy"].Text.Trim();
                paypal["proxies"] = new List<object> { fields["paypal_proxy"].Text.Trim() };
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
                config["output"] = output;
                config["storage"] = storage;
                config["cpa_mode"] = cpaMode;
                config["sub2api"] = sub2api;
                SaveConfig(path, config);
                PurchaseProjectText = fields["luckmail_purchase_project_code"].Text.Trim();
                PurchaseEmailTypeText = fields["luckmail_purchase_email_type"].Text.Trim();
                PurchaseDomainText = fields["luckmail_purchase_domain"].Text.Trim();
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

        private void AddConfigField(Grid form, Dictionary<string, TextBox> fields, int row, string label, string key, string value)
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
                Margin = new Thickness(0, 0, 0, 10)
            };
            Grid.SetRow(box, row);
            Grid.SetColumn(box, 1);
            form.Children.Add(box);
            fields[key] = box;
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

        private string FirstListValue(Dictionary<string, object> data, string key)
        {
            if (data.TryGetValue(key, out object value) && value is List<object> list && list.Count > 0)
            {
                return Convert.ToString(list[0]) ?? "";
            }
            return "";
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

        private void AddPurchaseArgs(List<string> args)
        {
            if (!string.IsNullOrWhiteSpace(PurchaseProjectText))
            {
                args.Add("--luckmail-purchase-project");
                args.Add(PurchaseProjectText.Trim());
            }
            if (!string.IsNullOrWhiteSpace(PurchaseEmailTypeText))
            {
                args.Add("--luckmail-purchase-email-type");
                args.Add(PurchaseEmailTypeText.Trim());
            }
            if (!string.IsNullOrWhiteSpace(PurchaseDomainText))
            {
                args.Add("--luckmail-purchase-domain");
                args.Add(PurchaseDomainText.Trim());
            }
        }

        private void AddPaypalOption(List<string> args)
        {
            if (SkipPaypalLink)
            {
                args.Add("--skip-paypal-link");
            }
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
            string status = GetString(data, "paypal_status");
            if (status.Length == 0) status = GetString(paypal, "status");
            if (status.Equals("completed", StringComparison.OrdinalIgnoreCase)) return "支付完成✅";
            if (status.Equals("link_ready", StringComparison.OrdinalIgnoreCase)) return "待人工支付";
            string ok = GetString(paypal, "ok").ToLowerInvariant();
            if (ok == "true") return "PayPal已生成";
            string error = GetString(paypal, "error");
            return error.Length > 0 ? "PayPal失败" : "已保存";
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
            if (IsImportOk(data, "sub2api_import") || IsImportOk(data, "cpa_import")) return "已导入CPA/SUB2";
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
            if (!string.IsNullOrWhiteSpace(error) || status.Equals("failed", StringComparison.OrdinalIgnoreCase)) return "失败";
            if (paypalStatus.Equals("completed", StringComparison.OrdinalIgnoreCase)) return "支付完成✅";
            if (status.Equals("paypal_failed", StringComparison.OrdinalIgnoreCase) || paypalStatus.Equals("failed", StringComparison.OrdinalIgnoreCase)) return "支付链接失败";
            if (paypalOk == "1" || status.Equals("paypal_ready", StringComparison.OrdinalIgnoreCase)) return "PayPal已生成";
            return access.Length > 0 ? "已注册" : "待处理";
        }

        private string DisplayPayPalStatus(string paypalStatus, string paypalOk, string paypalUrl)
        {
            if (paypalStatus.Equals("completed", StringComparison.OrdinalIgnoreCase)) return "支付完成✅";
            if (paypalStatus.Equals("failed", StringComparison.OrdinalIgnoreCase)) return "支付失败";
            if (paypalStatus.Equals("link_ready", StringComparison.OrdinalIgnoreCase)) return "待人工支付";
            if (paypalOk == "1" && !string.IsNullOrWhiteSpace(paypalUrl)) return "待人工支付";
            if (!string.IsNullOrWhiteSpace(paypalUrl)) return "待人工支付";
            return "";
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
                    Process.Start(new ProcessStartInfo("notepad.exe", path) { UseShellExecute = true });
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
}
