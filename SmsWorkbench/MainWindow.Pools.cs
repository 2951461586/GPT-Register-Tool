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
            return MailboxPoolFileStore.IsMailboxPoolLike(row.AccountType, row.MailboxProvider);
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
            return MailboxPoolFileStore.NormalizeEmailKey(email);
        }

        private void LoadMailboxPool()
        {
            foreach (string path in GetKnownMailboxPoolFiles())
            {
                LoadMailboxTokenFile(path);
            }
        }

        private IReadOnlyList<string> GetKnownMailboxPoolFiles()
        {
            return MailboxPoolFileStore.DiscoverKnownFiles(
                rootDir,
                GetMailboxTokenFile(),
                chataiMailboxFilePath);
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

                if (line.StartsWith("remail://", StringComparison.OrdinalIgnoreCase))
                {
                    string[] remailParts = line.Substring("remail://".Length).Split(new[] { "---" }, 4, StringSplitOptions.None);
                    if (remailParts.Length < 3 || string.IsNullOrWhiteSpace(remailParts[0]) || string.IsNullOrWhiteSpace(remailParts[1]) || string.IsNullOrWhiteSpace(remailParts[2])) continue;
                    allRows.Add(new PoolRow
                    {
                        Id = "M" + (i + 1),
                        CreatedAt = SafeTime(File.GetLastWriteTime(path)),
                        CompletedAt = SafeTime(File.GetLastWriteTime(path)),
                        Identifier = remailParts[0].Trim(),
                        AccountType = "ReMail邮箱池",
                        Status = "可收信",
                        RefreshToken = "ReMail",
                        Notes = path,
                        SourcePath = path,
                        RawLine = line,
                        MailboxLine = line,
                        MailboxProvider = "remail",
                        MailboxToken = remailParts[1].Trim()
                    });
                    continue;
                }

                if (MailboxPoolFileStore.TryParseICloudUrlLine(line, out string icloudEmail, out string receiveUrl))
                {
                    allRows.Add(new PoolRow
                    {
                        Id = "M" + (i + 1),
                        CreatedAt = SafeTime(File.GetLastWriteTime(path)),
                        CompletedAt = SafeTime(File.GetLastWriteTime(path)),
                        Identifier = icloudEmail,
                        AccountType = "iCloud邮箱池",
                        Status = "可收信",
                        RefreshToken = "接码链接",
                        Notes = path,
                        SourcePath = path,
                        RawLine = line,
                        MailboxLine = line,
                        MailboxProvider = "icloud_url",
                        MailboxToken = receiveUrl
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
            if (!LoadSessionPoolFromBackend()) LoadSessionJsonPool();
        }

        private bool LoadSessionPoolFromBackend()
        {
            if (System.ComponentModel.DesignerProperties.GetIsInDesignMode(this)) return true;
            try
            {
                JsonElement payload = desktopRead.ReadAccountsAsync().GetAwaiter().GetResult();
                if (!payload.TryGetProperty("accounts", out JsonElement accounts) || accounts.ValueKind != JsonValueKind.Array) return false;
                foreach (JsonElement account in accounts.EnumerateArray())
                {
                    Dictionary<string, object> data = JsonElementToDictionary(account);
                    AddBackendAccountRow(data);
                }
                return accounts.GetArrayLength() > 0;
            }
            catch (Exception ex)
            {
                Log("读取账号 backend 失败：" + SensitiveDataSanitizer.Redact(ex.Message));
                return false;
            }
        }

        private void AddBackendAccountRow(Dictionary<string, object> data)
        {
            string rawJson = data.TryGetValue("session", out object session) ? JsonSerializer.Serialize(session) : "{}";
            string status = GetString(data, "status");
            bool hasAccess = GetString(data, "has_access_token").Equals("True", StringComparison.OrdinalIgnoreCase)
                || GetString(data, "has_access_token") == "1";
            bool hasPaymentUrl = GetString(data, "has_payment_url").Equals("True", StringComparison.OrdinalIgnoreCase)
                || GetString(data, "has_payment_url") == "1";
            string accessState = hasAccess ? "present" : "";
            string paymentMethod = GetString(data, "payment_method");
            string paypalUrl = hasPaymentUrl ? "backend://payment-url" : "";
            string paypalStatus = GetString(data, "paypal_status");
            string paypalOk = GetString(data, "paypal_ok");
            string refreshStatus = GetString(data, "refresh_token_status");
            string provider = GetString(data, "mailbox_provider");
            var row = new PoolRow
            {
                Id = "DB" + GetString(data, "id"),
                CreatedAt = UnixTimeText(GetString(data, "created_at")),
                CompletedAt = UnixTimeText(GetString(data, "updated_at")),
                Identifier = GetString(data, "email"),
                AccountType = "SQLite" + (provider.Length > 0 ? "/" + provider : ""),
                AccountPlanType = GetAccountPlanType(data),
                RegistrationCountry = GetString(data, "registration_country"),
                QuotaStatus = GetQuotaStatus(data),
                Status = DisplayAccountStatus(status, paypalOk, accessState, GetString(data, "error"), paypalStatus, refreshStatus, GetImportedStatus(rawJson)),
                PayPalStatus = DisplayPayPalStatus(paypalStatus, paypalOk, paypalUrl, paymentMethod),
                PayPalAmount = GetPaypalAmount(rawJson),
                RefreshTokenStatus = DisplayRtStatus(refreshStatus),
                HasAccessToken = hasAccess,
                PayPalUrl = paypalUrl,
                RefreshToken = provider == "remail" ? "ReMail" : hasAccess ? "AT" : "",
                Proxy = DbTimingText(new Dictionary<string, string>(data.ToDictionary(pair => pair.Key, pair => Convert.ToString(pair.Value) ?? ""))),
                Notes = GetString(data, "json_path").Length > 0 ? GetString(data, "json_path") : GetDatabasePath(),
                SourcePath = GetDatabasePath(),
                RawLine = GetString(data, "id"),
                MailboxProvider = provider
            };
            PopulateQuotaFields(row, data);
            allRows.Add(row);
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
                        TryReadMailboxFromRawJson(JsonSerializer.Serialize(data), out string mailboxProvider, out string mailboxClientId, out string mailboxRefreshToken, out string mailboxToken, out string mailboxLine);
                        string timing = GetTimingText(data);
                        bool isGmailMailbox = mailboxProvider.Equals("gmail", StringComparison.OrdinalIgnoreCase);
                        bool isReMailMailbox = mailboxProvider.Equals("remail", StringComparison.OrdinalIgnoreCase);
                        var sessionRow = new PoolRow
                        {
                            Id = "S" + (allRows.Count + 1),
                            CreatedAt = SafeTime(File.GetCreationTime(path)),
                            CompletedAt = SafeTime(File.GetLastWriteTime(path)),
                            Identifier = email,
                            AccountType = mailboxProvider.Equals("cfworker", StringComparison.OrdinalIgnoreCase) ? "Session/CFWorker" : isReMailMailbox ? "Session/ReMail" : isGmailMailbox ? "Session/Gmail" : "Session",
                            AccountPlanType = GetAccountPlanType(data),
                            RegistrationCountry = GetString(data, "registration_country"),
                            QuotaStatus = GetQuotaStatus(data),
                            Status = importedStatus.Length > 0
                                ? importedStatus
                                : DisplayAccountStatus(GetString(data, "status"), "", access, GetString(data, "error"), paypalStatus, refreshTokenStatus, importedStatus),
                            PayPalStatus = paypalStatus,
                            PayPalAmount = paypalAmount,
                            RefreshTokenStatus = DisplayRtStatus(refreshTokenStatus),
                            Phone = verifiedPhone,
                            HasAccessToken = !string.IsNullOrWhiteSpace(access),
                            AccessTokenProbeStatusCode = GetAccessTokenProbeStatusCode(data),
                            PayPalUrl = paypalUrl,
                            RefreshToken = mailboxProvider.Equals("cfworker", StringComparison.OrdinalIgnoreCase) ? "CFWorker" : isReMailMailbox ? "ReMail" : isGmailMailbox ? (mailboxRefreshToken.Length > 0 ? Mask(mailboxRefreshToken) : "AppPassword") : Mask(access),
                            Proxy = timing,
                            Notes = path,
                            SourcePath = path,
                            ClientId = mailboxClientId,
                            RawRefreshToken = mailboxRefreshToken,
                            MailboxLine = mailboxLine,
                            MailboxProvider = mailboxProvider,
                            MailboxToken = mailboxToken
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



    }
}
