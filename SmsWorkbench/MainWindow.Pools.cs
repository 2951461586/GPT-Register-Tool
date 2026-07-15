namespace SmsWorkbench
{
    public partial class MainWindow
    {
        // Pool/session loading, filtering, overview
        private bool FilterRow(object item)
        {
            return item is PoolRow row && FilterRow(row);
        }

        private bool FilterRow(PoolRow row)
        {
            if (row == null) return false;
            string scope = DisplayText(ScopeFilter);
            string term = (SearchText ?? "").Trim().ToLowerInvariant();

            if (scope == "邮箱池" && !IsMailboxPoolLikeRow(row)) return false;
            if (scope == "已注册" && !row.AccountType.Contains("Session") && !row.AccountType.Contains("SQLite")) return false;
            if (scope == "待处理" && !row.Status.Contains("待") && !row.Status.Contains("缺") && !row.Status.Contains("失败")) return false;
            if (term.Length == 0) return true;

            string text = (row.Identifier + " " + row.AccountType + " " + row.Status + " " + row.Notes).ToLowerInvariant();
            return text.Contains(term);
        }

        private bool IsMailboxPoolLikeRow(PoolRow row)
        {
            if (row == null) return false;
            return row.AccountType.Contains("邮箱池") || row.AccountType.Contains("Chatai");
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
                    || line.EndsWith("@edu.liziai.cloud", StringComparison.OrdinalIgnoreCase)
                    || line.EndsWith("@liziai.cloud", StringComparison.OrdinalIgnoreCase))
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

                if (line.StartsWith("gmail://", StringComparison.OrdinalIgnoreCase))
                {
                    string payload = line.Substring("gmail://".Length).Trim();
                    string email = "";
                    string refreshToken = "";
                    string clientId = "";
                    string accountType = "Gmail邮箱池";
                    string status = "可收信";
                    if (payload.Contains("----"))
                    {
                        string[] gmailParts = payload.Split(new[] { "----" }, StringSplitOptions.None);
                        if (gmailParts.Length >= 2)
                        {
                            email = gmailParts[0].Trim();
                            if (gmailParts.Length >= 4)
                            {
                                clientId = gmailParts[1].Trim();
                                refreshToken = gmailParts[3].Trim();
                                status = "已授权";
                            }
                        }
                    }
                    else
                    {
                        string[] gmailParts = payload.Split(new[] { "---" }, StringSplitOptions.None);
                        if (gmailParts.Length >= 2)
                        {
                            email = gmailParts[0].Trim();
                        }
                    }
                    if (email.Length == 0) continue;
                    string refreshTokenDisplay = refreshToken.Length > 0 ? Mask(refreshToken) : "AppPassword";
                    allRows.Add(new PoolRow
                    {
                        Id = "M" + (i + 1),
                        CreatedAt = SafeTime(File.GetLastWriteTime(path)),
                        CompletedAt = SafeTime(File.GetLastWriteTime(path)),
                        Identifier = email,
                        AccountType = accountType,
                        Status = status,
                        RefreshToken = refreshTokenDisplay,
                        Notes = path,
                        SourcePath = path,
                        RawLine = line,
                        MailboxLine = line,
                        ClientId = clientId,
                        RawRefreshToken = refreshToken,
                        MailboxProvider = "gmail"
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
                        MailboxLine = line,
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
                    MailboxLine = line,
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
                    Dictionary<string, object> rawData = new Dictionary<string, object>(StringComparer.OrdinalIgnoreCase);
                    try
                    {
                        if (!string.IsNullOrWhiteSpace(rawJson)) rawData = JsonTextToObject(rawJson);
                    }
                    catch { }
                    foreach (var kv in data)
                    {
                        if (!rawData.ContainsKey(kv.Key)) rawData[kv.Key] = kv.Value;
                    }
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
                    bool isGmailMailbox = mailboxProvider.Equals("gmail", StringComparison.OrdinalIgnoreCase);
                    bool isChataiMailbox = mailboxProvider.Equals("chatai", StringComparison.OrdinalIgnoreCase) || (mailboxClientId.Length > 0 && !isCfWorkerMailbox);
                    var dbRow = new PoolRow
                    {
                        Id = "DB" + data["id"],
                        CreatedAt = UnixTimeText(data.TryGetValue("created_at", out string created) ? created : ""),
                        CompletedAt = UnixTimeText(data.TryGetValue("updated_at", out string updated) ? updated : ""),
                        Identifier = data.TryGetValue("email", out string email) ? email : "",
                        AccountType = isCfWorkerMailbox ? "SQLite/CFWorker" : isGmailMailbox ? "SQLite/Gmail" : isChataiMailbox ? "SQLite/Chatai" : "SQLite",
                        AccountPlanType = GetAccountPlanType(rawData),
                        QuotaStatus = GetQuotaStatus(rawData),
                        Status = DisplayAccountStatus(status, paypalOk, access, error, paypalStatus, refreshTokenStatus, importedStatus),
                        PayPalStatus = DisplayPayPalStatus(paypalStatus, paypalOk, paypalUrl, paymentMethod),
                        PayPalAmount = paypalAmount,
                        RefreshTokenStatus = DisplayRtStatus(refreshTokenStatus),
                        Phone = verifiedPhone,
                        HasAccessToken = !string.IsNullOrWhiteSpace(access),
                        PayPalUrl = paypalUrl,
                        RefreshToken = isCfWorkerMailbox ? "CFWorker" : isGmailMailbox ? (mailboxRefreshToken.Length > 0 ? Mask(mailboxRefreshToken) : "AppPassword") : Mask(isChataiMailbox ? mailboxRefreshToken : access),
                        Proxy = DbTimingText(data),
                        Notes = string.IsNullOrWhiteSpace(jsonPath) ? dbPath : jsonPath,
                        SourcePath = dbPath,
                        RawLine = data["id"],
                        ClientId = mailboxClientId,
                        RawRefreshToken = mailboxRefreshToken,
                        MailboxLine = mailboxLine,
                        MailboxProvider = mailboxProvider
                    };
                    PopulateQuotaFields(dbRow, rawData);
                    allRows.Add(dbRow);
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
                        bool isGmailMailbox = mailboxProvider.Equals("gmail", StringComparison.OrdinalIgnoreCase);
                        var sessionRow = new PoolRow
                        {
                            Id = "S" + (allRows.Count + 1),
                            CreatedAt = SafeTime(File.GetCreationTime(path)),
                            CompletedAt = SafeTime(File.GetLastWriteTime(path)),
                            Identifier = email,
                            AccountType = mailboxProvider.Equals("cfworker", StringComparison.OrdinalIgnoreCase) ? "Session/CFWorker" : isGmailMailbox ? "Session/Gmail" : "Session",
                            AccountPlanType = GetAccountPlanType(data),
                            QuotaStatus = GetQuotaStatus(data),
                            Status = importedStatus.Length > 0
                                ? importedStatus
                                : DisplayAccountStatus(GetString(data, "status"), "", access, GetString(data, "error"), paypalStatus, refreshTokenStatus, importedStatus),
                            PayPalStatus = paypalStatus,
                            PayPalAmount = paypalAmount,
                            RefreshTokenStatus = DisplayRtStatus(refreshTokenStatus),
                            Phone = verifiedPhone,
                            HasAccessToken = !string.IsNullOrWhiteSpace(access),
                            PayPalUrl = paypalUrl,
                            RefreshToken = mailboxProvider.Equals("cfworker", StringComparison.OrdinalIgnoreCase) ? "CFWorker" : isGmailMailbox ? (mailboxRefreshToken.Length > 0 ? Mask(mailboxRefreshToken) : "AppPassword") : Mask(access),
                            Proxy = timing,
                            Notes = path,
                            SourcePath = path,
                            ClientId = mailboxClientId,
                            RawRefreshToken = mailboxRefreshToken,
                            MailboxLine = mailboxLine,
                            MailboxProvider = mailboxProvider
                        };
                        PopulateQuotaFields(sessionRow, data);
                        allRows.Add(sessionRow);
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
                "ALTER TABLE accounts ADD COLUMN oauth_refresh_token TEXT DEFAULT ''",
                "ALTER TABLE accounts ADD COLUMN workspace_status TEXT DEFAULT ''",
                "ALTER TABLE accounts ADD COLUMN workspace_id TEXT DEFAULT ''",
                "ALTER TABLE accounts ADD COLUMN workspace_name TEXT DEFAULT ''",
                "ALTER TABLE accounts ADD COLUMN workspace_switch_result TEXT DEFAULT ''",
                "ALTER TABLE accounts ADD COLUMN workspace_updated_at INTEGER DEFAULT 0",
                "ALTER TABLE accounts ADD COLUMN account_type TEXT DEFAULT ''",
                "ALTER TABLE accounts ADD COLUMN quota_status TEXT DEFAULT ''"
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

    }
}
