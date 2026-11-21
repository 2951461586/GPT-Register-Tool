namespace SmsWorkbench.Services
{
    /// <summary>
    /// Coordinates mailbox-pool and account-table refreshes (full merged reads,
    /// fallback split reads, incremental delta reads, row deduplication, and
    /// checked-row restoration) independently of the window.
    /// </summary>
    public sealed class PoolRefreshCoordinator
    {
        public const int MaxIncrementalAccountRefreshes = 10;

        private readonly AccountRefreshCursor accountCursor = new();
        private bool refreshRunning;
        private bool refreshPending;
        private bool refreshPendingFull;

        public AccountRefreshCursor Cursor => accountCursor;

        public bool IsRefreshRunning => refreshRunning;

        public async Task RefreshPoolsAsync(
            IDesktopReadClient desktopRead,
            PoolViewModel pool,
            StatusBarViewModel statusBar,
            string chataiMailboxPath,
            Func<string> getDatabasePath,
            Func<JsonElement, Dictionary<string, object>> toDictionary,
            Func<string, string> mask,
            Action<string> log,
            bool preserveView = false,
            bool forceFull = false,
            bool isDesignMode = false,
            CancellationToken ct = default)
        {
            if (refreshRunning)
            {
                refreshPending = true;
                refreshPendingFull |= forceFull;
                return;
            }

            refreshRunning = true;
            HashSet<string>? checkedIds = preserveView
                ? CaptureCheckedIdentifiers(pool.AllRows)
                : null;
            int restorePage = preserveView ? pool.CurrentPage : 1;
            try
            {
                if (preserveView
                    && !forceFull
                    && await TryApplyIncrementalAccountsAsync(
                        desktopRead, pool, statusBar, getDatabasePath, toDictionary, log, ct).ConfigureAwait(true))
                {
                    return;
                }

                var staged = new List<PoolRow>();
                try
                {
                    JsonElement merged = await desktopRead.ReadPoolsAsync(chataiMailboxPath, ct).ConfigureAwait(true);
                    ApplyMailboxPoolPayload(merged, staged, mask, log);
                    ApplyAccountsPayload(merged, staged, getDatabasePath, toDictionary, log);
                }
                catch (Exception ex)
                {
                    log("合并读取邮箱池/账号失败，改用分开读取：" + SensitiveDataSanitizer.Redact(ex.Message));
                    await LoadMailboxPoolAsync(desktopRead, chataiMailboxPath, staged, isDesignMode, mask, log, ct).ConfigureAwait(true);
                    await LoadSessionPoolAsync(desktopRead, staged, isDesignMode, getDatabasePath, toDictionary, log, ct).ConfigureAwait(true);
                }

                List<PoolRow> deduped = DeduplicateRows(staged);
                if (checkedIds != null)
                {
                    RestoreCheckedIdentifiers(deduped, checkedIds);
                }

                pool.ReplaceAll(deduped);
                pool.CurrentPage = restorePage;
                pool.UpdateOverview();
                pool.RefreshPagedRows();
                statusBar.Text = pool.SummaryText;
                log("邮箱池和 session 状态已刷新。");
            }
            finally
            {
                refreshRunning = false;
                if (refreshPending)
                {
                    bool full = refreshPendingFull;
                    refreshPending = false;
                    refreshPendingFull = false;
                    await RefreshPoolsAsync(
                        desktopRead,
                        pool,
                        statusBar,
                        chataiMailboxPath,
                        getDatabasePath,
                        toDictionary,
                        mask,
                        log,
                        preserveView: true,
                        forceFull: full,
                        isDesignMode: isDesignMode,
                        ct: ct).ConfigureAwait(true);
                }
            }
        }

        public static HashSet<string> CaptureCheckedIdentifiers(IEnumerable<PoolRow> rows)
        {
            return rows
                .Where(r => r.IsChecked)
                .Select(r => (r.Identifier ?? "").Trim().ToLowerInvariant())
                .Where(id => id.Length > 0)
                .ToHashSet(StringComparer.OrdinalIgnoreCase);
        }

        public static void RestoreCheckedIdentifiers(IEnumerable<PoolRow> rows, IReadOnlySet<string> checkedIds)
        {
            foreach (PoolRow row in rows)
            {
                string id = (row.Identifier ?? "").Trim();
                if (id.Length > 0 && checkedIds.Contains(id))
                {
                    row.IsChecked = true;
                }
            }
        }

        public static List<PoolRow> DeduplicateRows(List<PoolRow> rows)
        {
            var best = new Dictionary<string, PoolRow>(StringComparer.OrdinalIgnoreCase);
            foreach (PoolRow row in rows)
            {
                string key = MailboxPoolFileStore.NormalizeEmailKey(row.Identifier);
                if (key.Length == 0) continue;
                if (!best.TryGetValue(key, out PoolRow? existing) || RowPriority(row) > RowPriority(existing))
                {
                    best[key] = row;
                }
            }

            if (best.Count == 0) return rows;
            return rows.Where(row =>
            {
                string key = MailboxPoolFileStore.NormalizeEmailKey(row.Identifier);
                return key.Length == 0 || ReferenceEquals(best[key], row);
            }).ToList();
        }

        public static int RowPriority(PoolRow row)
        {
            if (row.SourcePath.EndsWith(".sqlite3", StringComparison.OrdinalIgnoreCase)) return 30;
            if (row.AccountType.Contains("Session", StringComparison.Ordinal)) return 20;
            if (row.PayPalUrl.Length > 0 || row.Status.Contains("PayPal", StringComparison.Ordinal)) return 15;
            return 10;
        }

        public static string ResolveChataiMailboxFilePath(string? configuredPath, string configDir, string rootDir)
        {
            if (!string.IsNullOrWhiteSpace(configuredPath) && File.Exists(configuredPath))
            {
                return configuredPath;
            }

            string[] candidates = { "hotmail.txt", "chatai_mailbox.txt", "chatai.txt" };
            string[] searchDirectories = { configDir, rootDir };
            foreach (string directory in searchDirectories)
            {
                foreach (string name in candidates)
                {
                    string path = Path.Combine(directory, name);
                    if (File.Exists(path)) return path;
                }
            }

            foreach (string directory in searchDirectories)
            {
                if (!Directory.Exists(directory)) continue;
                foreach (string path in Directory.GetFiles(directory, "*chatai*.txt", SearchOption.TopDirectoryOnly))
                {
                    return path;
                }
            }

            return "";
        }

        public void ApplyMailboxPoolPayload(
            JsonElement payload,
            List<PoolRow> target,
            Func<string, string> mask,
            Action<string> log)
        {
            if (!payload.TryGetProperty("files", out JsonElement files) || files.ValueKind != JsonValueKind.Array)
            {
                log("读取邮箱池 backend 失败：响应缺少 files 数组。");
                return;
            }
            foreach (JsonElement file in files.EnumerateArray())
            {
                AddMailboxPoolFileRows(file, target, mask);
            }
        }

        public void ApplyAccountsPayload(
            JsonElement payload,
            List<PoolRow> target,
            Func<string> getDatabasePath,
            Func<JsonElement, Dictionary<string, object>> toDictionary,
            Action<string> log)
        {
            if (!payload.TryGetProperty("accounts", out JsonElement accounts) || accounts.ValueKind != JsonValueKind.Array)
            {
                log("读取账号 backend 失败：响应缺少 accounts 数组。");
                return;
            }
            AccountRefreshPlan plan = AccountRefreshPlanReader.Read(payload);
            accountCursor.Record(plan);
            string databasePath = getDatabasePath();
            foreach (JsonElement account in plan.Accounts)
            {
                target.Add(BuildRowFromPayload(account, databasePath, toDictionary));
            }
        }

        public void ApplyAccountsDelta(
            JsonElement payload,
            PoolViewModel pool,
            Func<string> getDatabasePath,
            Func<JsonElement, Dictionary<string, object>> toDictionary,
            Action<string> log)
        {
            AccountRefreshPlan plan = AccountRefreshPlanReader.Read(payload);
            if (!plan.IsDelta)
            {
                ApplyAccountsPayload(payload, pool.AllRows, getDatabasePath, toDictionary, log);
                return;
            }

            string databasePath = getDatabasePath();
            var changed = plan.Accounts.Select(account => BuildRowFromPayload(account, databasePath, toDictionary)).ToList();
            pool.MergeDatabaseRows(changed, plan.CurrentIds!, databasePath);
            accountCursor.Record(plan);
        }

        public async Task<bool> TryApplyIncrementalAccountsAsync(
            IDesktopReadClient desktopRead,
            PoolViewModel pool,
            StatusBarViewModel statusBar,
            Func<string> getDatabasePath,
            Func<JsonElement, Dictionary<string, object>> toDictionary,
            Action<string> log,
            CancellationToken ct)
        {
            if (!accountCursor.CanRefreshIncrementally(MaxIncrementalAccountRefreshes))
            {
                return false;
            }
            try
            {
                JsonElement delta = await desktopRead
                    .ReadAccountsDeltaAsync(accountCursor.Cursor, ct)
                    .ConfigureAwait(true);
                ApplyAccountsDelta(delta, pool, getDatabasePath, toDictionary, log);
            }
            catch (Exception ex)
            {
                log("增量刷新账号失败，改用全量刷新：" + SensitiveDataSanitizer.Redact(ex.Message));
                accountCursor.ForceFull(MaxIncrementalAccountRefreshes);
                return false;
            }
            accountCursor.RecordIncremental();
            pool.UpdateOverview();
            pool.RefreshPagedRows();
            statusBar.Text = pool.SummaryText;
            return true;
        }

        private async Task LoadMailboxPoolAsync(
            IDesktopReadClient desktopRead,
            string chataiPath,
            List<PoolRow> target,
            bool isDesignMode,
            Func<string, string> mask,
            Action<string> log,
            CancellationToken ct)
        {
            if (isDesignMode) return;
            try
            {
                JsonElement payload = await desktopRead.ReadMailboxPoolAsync(chataiPath, ct).ConfigureAwait(true);
                ApplyMailboxPoolPayload(payload, target, mask, log);
            }
            catch (Exception ex)
            {
                log("读取邮箱池 backend 失败：" + SensitiveDataSanitizer.Redact(ex.Message));
            }
        }

        private async Task LoadSessionPoolAsync(
            IDesktopReadClient desktopRead,
            List<PoolRow> target,
            bool isDesignMode,
            Func<string> getDatabasePath,
            Func<JsonElement, Dictionary<string, object>> toDictionary,
            Action<string> log,
            CancellationToken ct)
        {
            if (isDesignMode) return;
            try
            {
                JsonElement payload = await desktopRead.ReadAccountsAsync(ct).ConfigureAwait(true);
                ApplyAccountsPayload(payload, target, getDatabasePath, toDictionary, log);
            }
            catch (Exception ex)
            {
                log("读取账号 backend 失败：" + SensitiveDataSanitizer.Redact(ex.Message));
            }
        }

        private static PoolRow BuildRowFromPayload(
            JsonElement account,
            string databasePath,
            Func<JsonElement, Dictionary<string, object>> toDictionary)
        {
            Dictionary<string, object> data = toDictionary(account);
            string rawJson = account.TryGetProperty("session", out JsonElement sessionElement)
                && sessionElement.ValueKind == JsonValueKind.Object
                ? sessionElement.GetRawText()
                : "{}";
            return AccountRowFactory.BuildBackendAccountRow(data, databasePath, rawJson);
        }

        private static void AddMailboxPoolFileRows(JsonElement file, List<PoolRow> target, Func<string, string> mask)
        {
            string path = JsonString(file, "path");
            if (path.Length == 0 || !File.Exists(path)) return;
            if (!file.TryGetProperty("lines", out JsonElement lines) || lines.ValueKind != JsonValueKind.Array) return;
            string fileTime = File.GetLastWriteTime(path).ToString("yyyy-MM-dd HH:mm:ss", CultureInfo.InvariantCulture);
            int index = 0;
            foreach (JsonElement line in lines.EnumerateArray())
            {
                AddMailboxPoolLineRow(path, fileTime, index, line, target, mask);
                index++;
            }
        }

        private static void AddMailboxPoolLineRow(
            string path,
            string fileTime,
            int index,
            JsonElement line,
            List<PoolRow> target,
            Func<string, string> mask)
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
            target.Add(new PoolRow
            {
                Id = "M" + (index + 1),
                CreatedAt = fileTime,
                CompletedAt = fileTime,
                Identifier = email,
                AccountType = MailboxPoolAccountType(provider),
                RegistrationStatus = JsonString(line, "registration_status"),
                Status = RegistrationStatusPresentation.MailboxStatus(
                    JsonString(line, "registration_status"), MailboxPoolStatus(provider, authMode)),
                RefreshToken = MailboxPoolRefreshDisplay(provider, refreshToken, mask),
                Notes = path,
                SourcePath = path,
                RawLine = mailboxLine,
                MailboxLine = mailboxLine,
                MailboxProvider = provider,
                MailboxToken = provider is "remail" or "smailr" or "icloud_url" ? token : "",
                ClientId = JsonString(line, "client_id"),
                RawRefreshToken = provider is "gmail" or "chatai" ? refreshToken : "",
            });
        }

        public static string MailboxPoolAccountType(string provider) => provider switch
        {
            "cfworker" => "CFWorker邮箱池",
            "remail" => "ReMail邮箱池",
            "smailr" => "Smailr邮箱池",
            "icloud_url" => "iCloud邮箱池",
            "gmail" => "Gmail邮箱池",
            "chatai" => "Chatai邮箱池",
            _ => "邮箱池",
        };

        public static string MailboxPoolStatus(string provider, string authMode)
        {
            if (provider == "gmail") return authMode == "oauth_refresh" ? "已授权" : "可收信";
            if (provider is "chatai" or "graph") return "已授权";
            return "可收信";
        }

        public static string MailboxPoolRefreshDisplay(string provider, string refreshToken, Func<string, string> mask) => provider switch
        {
            "cfworker" => "CFWorker",
            "remail" => "ReMail",
            "smailr" => "Smailr",
            "icloud_url" => "接码链接",
            "gmail" => refreshToken.Length > 0 ? mask(refreshToken) : "AppPassword",
            _ => refreshToken.Length > 0 ? mask(refreshToken) : "",
        };

        private static string JsonString(JsonElement element, string property)
        {
            if (element.ValueKind != JsonValueKind.Object || !element.TryGetProperty(property, out JsonElement value))
            {
                return "";
            }
            return value.ValueKind switch
            {
                JsonValueKind.String => value.GetString() ?? "",
                JsonValueKind.Number or JsonValueKind.True or JsonValueKind.False => value.ToString(),
                _ => "",
            };
        }
    }
}
