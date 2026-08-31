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

            if (scope == "有试用" && !PromotionStatusPresentation.IsTrialEligible(row.PromotionStatus)) return false;
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

        /// <summary>
        /// Fire-and-forget refresh entry kept for existing sync callers; the
        /// actual work is async so the UI thread never waits on a backend call.
        /// </summary>
        private void RefreshPools()
        {
            _ = RefreshPoolsAsync(_lifetimeCts.Token);
        }

        private async Task RefreshPoolsAsync(CancellationToken ct = default)
        {
            if (poolsRefreshRunning)
                return;
            poolsRefreshRunning = true;
            try
            {
                allRows.Clear();
                try
                {
                    // One merged request (accounts + mailbox pool) through the
                    // resident channel; falls back to the two separate reads.
                    JsonElement merged = await desktopRead.ReadPoolsAsync(GetChataiMailboxFilePath());
                    ApplyMailboxPoolPayload(merged);
                    ApplyAccountsPayload(merged);
                }
                catch (Exception ex)
                {
                    Log("合并读取邮箱池/账号失败，改用分开读取：" + SensitiveDataSanitizer.Redact(ex.Message));
                    await LoadMailboxPoolAsync();
                    await LoadSessionPoolAsync();
                }
                DeduplicateRows();
                currentPage = 1;
                UpdateOverview();
                RefreshPagedRows();
                StatusText = $"共 {allRows.Count} 条；当前筛选 {filteredCount} 条";
                Log("邮箱池和 session 状态已刷新。");
            }
            finally
            {
                poolsRefreshRunning = false;
            }
        }

        private void RefreshPagedRows()
        {
            if (PagedRows == null) return;
            var filtered = AccountGridOrdering.Apply(
                allRows.Where(FilterRow),
                accountSortMember,
                accountSortDirection).ToList();
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
            int trialEligible = allRows.Count(r => PromotionStatusPresentation.IsTrialEligible(r.PromotionStatus));
            int registered = allRows.Count(IsRegisteredRow);
            int attention = allRows.Count(r => r.Status.Contains("待") || r.Status.Contains("缺") || r.Status.Contains("失败"));
            TotalCountText = allRows.Count.ToString();
            TrialCountText = trialEligible.ToString();
            RegisteredCountText = registered.ToString();
            AttentionCountText = attention.ToString();
        }

        private bool IsRegisteredRow(PoolRow row)
        {
            return row.AccountType.Contains("Session")
                || row.SourcePath.EndsWith(".sqlite3", StringComparison.OrdinalIgnoreCase)
                || row.Status.Contains("已注册")
                || row.Status.Contains("PayPal");
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
            if (row.SourcePath.EndsWith(".sqlite3", StringComparison.OrdinalIgnoreCase)) return 30;
            if (row.AccountType.Contains("Session")) return 20;
            if (row.PayPalUrl.Length > 0 || row.Status.Contains("PayPal")) return 15;
            return 10;
        }

        private string NormalizeEmailKey(string email)
        {
            return MailboxPoolFileStore.NormalizeEmailKey(email);
        }

        private async Task LoadMailboxPoolAsync(CancellationToken ct = default)
        {
            if (System.ComponentModel.DesignerProperties.GetIsInDesignMode(this)) return;
            try
            {
                JsonElement payload = await desktopRead.ReadMailboxPoolAsync(GetChataiMailboxFilePath());
                ApplyMailboxPoolPayload(payload);
            }
            catch (Exception ex)
            {
                Log("读取邮箱池 backend 失败：" + SensitiveDataSanitizer.Redact(ex.Message));
            }
        }

        private void ApplyMailboxPoolPayload(JsonElement payload)
        {
            if (!payload.TryGetProperty("files", out JsonElement files) || files.ValueKind != JsonValueKind.Array)
            {
                Log("读取邮箱池 backend 失败：响应缺少 files 数组。");
                return;
            }
            foreach (JsonElement file in files.EnumerateArray())
            {
                AddMailboxPoolFileRows(file);
            }
        }

        private void AddMailboxPoolFileRows(JsonElement file)
        {
            string path = JsonString(file, "path");
            if (path.Length == 0 || !File.Exists(path)) return;
            if (!file.TryGetProperty("lines", out JsonElement lines) || lines.ValueKind != JsonValueKind.Array) return;
            string fileTime = SafeTime(File.GetLastWriteTime(path));
            int index = 0;
            foreach (JsonElement line in lines.EnumerateArray())
            {
                AddMailboxPoolLineRow(path, fileTime, index, line);
                index++;
            }
        }

        private void AddMailboxPoolLineRow(string path, string fileTime, int index, JsonElement line)
        {
            string provider = JsonString(line, "provider").ToLowerInvariant();
            string email = JsonString(line, "email");
            if (email.Length == 0) return;
            string authMode = JsonString(line, "auth_mode");
            string rawLine = JsonString(line, "raw_line");
            string mailboxLine = provider == "cfworker" && !rawLine.StartsWith("cfworker://", StringComparison.OrdinalIgnoreCase)
                ? "cfworker://" + email
                : rawLine;
            string refreshToken = JsonString(line, "refresh_token");
            string token = JsonString(line, "token");
            allRows.Add(new PoolRow
            {
                Id = "M" + (index + 1),
                CreatedAt = fileTime,
                CompletedAt = fileTime,
                Identifier = email,
                AccountType = MailboxPoolAccountType(provider),
                Status = MailboxPoolStatus(provider, authMode),
                RefreshToken = MailboxPoolRefreshDisplay(provider, refreshToken),
                Notes = path,
                SourcePath = path,
                RawLine = mailboxLine,
                MailboxLine = mailboxLine,
                MailboxProvider = provider,
                MailboxToken = provider is "remail" or "smailr" or "icloud_url" ? token : "",
                ClientId = JsonString(line, "client_id"),
                RawRefreshToken = provider is "gmail" or "chatai" ? refreshToken : ""
            });
        }

        private static string MailboxPoolAccountType(string provider) => provider switch
        {
            "cfworker" => "CFWorker邮箱池",
            "remail" => "ReMail邮箱池",
            "smailr" => "Smailr邮箱池",
            "icloud_url" => "iCloud邮箱池",
            "gmail" => "Gmail邮箱池",
            "chatai" => "Chatai邮箱池",
            _ => "邮箱池",
        };

        private static string MailboxPoolStatus(string provider, string authMode)
        {
            if (provider == "gmail") return authMode == "oauth_refresh" ? "已授权" : "可收信";
            if (provider is "chatai" or "graph" or "chongzhi") return "已授权";
            return "可收信";
        }

        private string MailboxPoolRefreshDisplay(string provider, string refreshToken)
        {
            switch (provider)
            {
                case "cfworker": return "CFWorker";
                case "remail": return "ReMail";
                case "smailr": return "Smailr";
                case "icloud_url": return "接码链接";
                case "gmail": return refreshToken.Length > 0 ? Mask(refreshToken) : "AppPassword";
                default: return refreshToken.Length > 0 ? Mask(refreshToken) : "";
            }
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

        private async Task LoadSessionPoolAsync(CancellationToken ct = default)
        {
            if (System.ComponentModel.DesignerProperties.GetIsInDesignMode(this)) return;
            try
            {
                JsonElement payload = await desktopRead.ReadAccountsAsync();
                ApplyAccountsPayload(payload);
            }
            catch (Exception ex)
            {
                Log("读取账号 backend 失败：" + SensitiveDataSanitizer.Redact(ex.Message));
            }
        }

        private void ApplyAccountsPayload(JsonElement payload)
        {
            if (!payload.TryGetProperty("accounts", out JsonElement accounts) || accounts.ValueKind != JsonValueKind.Array)
            {
                Log("读取账号 backend 失败：响应缺少 accounts 数组。");
                return;
            }
            // Per-refresh values hoisted out of the row loop; each used to
            // re-read and re-parse config.json once (or twice) per account.
            string databasePath = GetDatabasePath();
            foreach (JsonElement account in accounts.EnumerateArray())
            {
                Dictionary<string, object> data = JsonElementToDictionary(account);
                string rawJson = account.TryGetProperty("session", out JsonElement sessionElement)
                    && sessionElement.ValueKind == JsonValueKind.Object
                    ? sessionElement.GetRawText()
                    : "{}";
                AddBackendAccountRow(data, databasePath, rawJson);
            }
        }

        private void AddBackendAccountRow(Dictionary<string, object> data, string databasePath, string rawJson)
        {
            string status = GetString(data, "status");
            bool hasAccess = ParseBoolean(FirstNonEmpty(
                GetString(data, "access_token_present"),
                GetString(data, "has_access_token")));
            bool hasPaymentUrl = ParseBoolean(FirstNonEmpty(
                GetString(data, "payment_url_present"),
                GetString(data, "has_payment_url")));
            string accessState = hasAccess ? "present" : "";
            string paymentMethod = GetString(data, "payment_method");
            string paypalUrl = hasPaymentUrl ? "backend://payment-url" : "";
            string paypalStatus = GetString(data, "paypal_status");
            string paypalOk = GetString(data, "paypal_ok");
            string refreshStatus = GetString(data, "refresh_token_status");
            if (ParseBoolean(FirstNonEmpty(
                    GetString(data, "refresh_token_present"),
                    GetString(data, "has_refresh_token")))
                && (refreshStatus.Length == 0 || refreshStatus.Equals("no_rt", StringComparison.OrdinalIgnoreCase)))
            {
                refreshStatus = "oauth_present";
            }
            string provider = GetString(data, "mailbox_provider");
            var row = new PoolRow
            {
                Id = "DB" + GetString(data, "id"),
                CreatedAt = UnixTimeText(GetString(data, "created_at")),
                CompletedAt = UnixTimeText(GetString(data, "updated_at")),
                Identifier = GetString(data, "email"),
                AccountType = MailboxTypeDisplay(provider, GetString(data, "email")),
                AccountPlanType = AccountStatusInterpreter.GetAccountPlanType(data),
                Source = GetString(data, "source"),
                RegisterMethod = GetString(data, "register_method"),
                SessionType = GetString(data, "session_type"),
                PlanType = FirstNonEmpty(GetString(data, "plan_type"), GetString(data, "account_type")),
                RegistrationCountry = GetString(data, "registration_country"),
                Status = AccountStatusInterpreter.DisplayAccountStatus(status, paypalOk, accessState, GetString(data, "error"), paypalStatus, refreshStatus, AccountStatusInterpreter.GetImportedStatus(rawJson)),
                PayPalStatus = AccountStatusInterpreter.DisplayPayPalStatus(paypalStatus, paypalOk, paypalUrl, paymentMethod),
                PayPalAmount = AccountStatusInterpreter.GetPaypalAmount(rawJson),
                PromotionStatus = AccountStatusInterpreter.DisplayPromotionStatus(
                    GetString(data, "promotion_status"),
                    AccountStatusInterpreter.DisplayPayPalStatus(paypalStatus, paypalOk, paypalUrl, paymentMethod),
                    AccountStatusInterpreter.GetPaypalAmount(rawJson)),
                RefreshTokenStatus = AccountStatusInterpreter.DisplayRtStatus(refreshStatus),
                TwoFactorStatus = AccountStatusInterpreter.HasTwoFactor(data) ? "已设置" : "未设置",
                HasAccessToken = hasAccess,
                AccessTokenProbeStatusCode = AccountStatusInterpreter.GetAccessTokenProbeStatusCode(data),
                PayPalUrl = paypalUrl,
                RefreshToken = provider == "remail" ? "ReMail" : hasAccess ? "AT" : "",
                Proxy = DbTimingText(new Dictionary<string, string>(data.ToDictionary(pair => pair.Key, pair => Convert.ToString(pair.Value) ?? ""))),
                Notes = GetString(data, "json_path").Length > 0 ? GetString(data, "json_path") : databasePath,
                SourcePath = databasePath,
                RawLine = GetString(data, "id"),
                MailboxProvider = provider
            };
            allRows.Add(row);
        }

        internal static string MailboxTypeDisplay(string provider, string email = "")
        {
            string normalized = (provider ?? "").Trim().ToLowerInvariant().Replace("-", "_");
            string domain = (email ?? "").Split('@').LastOrDefault()?.ToLowerInvariant() ?? "";
            return normalized switch
            {
                "remail" when domain is "outlook.com" or "hotmail.com" or "live.com" or "msn.com" => "remail/outlook",
                "remail" => "remail",
                "icloud_url" or "icloud" => "icloud",
                "cf_worker" or "cfworker" => "cfworker",
                "chongzhi" when domain is "outlook.com" or "hotmail.com" or "live.com" or "msn.com" => "outlook",
                "microsoft" or "graph" or "outlook" => "outlook",
                "gmail" => "gmail",
                "smailr" => "smailr",
                "chatai" => "chatai",
                "" => "unknown",
                _ => normalized,
            };
        }
    }
}
