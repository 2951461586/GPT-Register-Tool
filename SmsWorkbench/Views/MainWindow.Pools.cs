// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch stays `annotations`.
#nullable enable

using SmsWorkbench.Localization;

namespace SmsWorkbench
{
    public partial class MainWindow
    {
        // Row cache, filter, paging, sorting and selection live in PoolViewModel
        // (MainWindow.Pool); only the refresh-in-flight flag is view-level.
        private bool poolsRefreshRunning;
        private bool poolsRefreshPending;
        private bool poolsRefreshPendingFull;

        // Incremental accounts refresh: the cursor is the backend's max
        // `updated_at`, and every N delta reads forces a full rebuild so a lost
        // row or a missed deletion cannot stay stale forever.
        private const int MaxIncrementalAccountRefreshes = 10;
        private long accountsCursor;
        private int incrementalAccountRefreshes;

        // Pool/session loading, filtering, overview
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
            _ = RefreshPoolsAsync(ct: _lifetimeCts.Token);
        }

        /// <summary>
        /// Refresh that keeps the operator's place in the grid. Used by the
        /// mid-batch hot refresh: rebuilding <c>Pool.AllRows</c> from scratch drops
        /// both the checkbox state and the current page, and a 25-account
        /// liveness batch would otherwise yank the view back to page one and
        /// clear the selection two dozen times.
        /// </summary>
        private void RefreshPoolsPreservingView(bool forceFull = false)
        {
            _ = RefreshPoolsAsync(preserveView: true, forceFull: forceFull, ct: _lifetimeCts.Token);
        }

        private async Task RefreshPoolsAsync(bool preserveView = false, bool forceFull = false, CancellationToken ct = default)
        {
            if (poolsRefreshRunning)
            {
                // Do not drop a language-switch reload because a periodic refresh
                // happened to be in flight; run it immediately afterwards.
                poolsRefreshPending = true;
                poolsRefreshPendingFull |= forceFull;
                return;
            }
            poolsRefreshRunning = true;
            // Snapshot before the clear: rows are rebuilt from scratch below,
            // so identity has to be carried across by email.
            HashSet<string>? checkedIds = preserveView
                ? Pool.AllRows.Where(r => r.IsChecked)
                         .Select(r => (r.Identifier ?? "").Trim().ToLowerInvariant())
                         .Where(id => id.Length > 0)
                         .ToHashSet(StringComparer.OrdinalIgnoreCase)
                : null;
            int restorePage = preserveView ? Pool.CurrentPage : 1;
            try
            {
                if (preserveView && !forceFull
                    && await TryApplyIncrementalAccountsAsync(ct).ConfigureAwait(true))
                    return;
                Pool.AllRows.Clear();
                try
                {
                    // One merged request (accounts + mailbox pool) through the
                    // resident channel; falls back to the two separate reads.
                    JsonElement merged = await desktopRead.ReadPoolsAsync(GetChataiMailboxFilePath(), ct);
                    ApplyMailboxPoolPayload(merged);
                    ApplyAccountsPayload(merged);
                }
                catch (Exception ex)
                {
                    Log("合并读取邮箱池/账号失败，改用分开读取：" + SensitiveDataSanitizer.Redact(ex.Message));
                    await LoadMailboxPoolAsync(ct);
                    await LoadSessionPoolAsync(ct);
                }
                DeduplicateRows();
                if (checkedIds != null)
                {
                    foreach (PoolRow row in Pool.AllRows)
                    {
                        string id = (row.Identifier ?? "").Trim();
                        if (id.Length > 0 && checkedIds.Contains(id))
                            row.IsChecked = true;
                    }
                }
                // RefreshPagedRows already clamps an out-of-range page, so a
                // restored page stays valid even when the row count shrinks.
                Pool.CurrentPage = restorePage;
                Pool.UpdateOverview();
                Pool.RefreshPagedRows();
                StatusBar.Text = Pool.SummaryText;
                Log("邮箱池和 session 状态已刷新。");
            }
            finally
            {
                poolsRefreshRunning = false;
                if (poolsRefreshPending)
                {
                    bool full = poolsRefreshPendingFull;
                    poolsRefreshPending = false;
                    poolsRefreshPendingFull = false;
                    _ = RefreshPoolsAsync(preserveView: true, forceFull: full, ct: ct);
                }
            }
        }

        private void DeduplicateRows()
        {
            var best = new Dictionary<string, PoolRow>(StringComparer.OrdinalIgnoreCase);
            foreach (PoolRow row in Pool.AllRows.ToList())
            {
                string key = NormalizeEmailKey(row.Identifier);
                if (key.Length == 0) continue;
                if (!best.TryGetValue(key, out PoolRow? existing) || RowPriority(row) > RowPriority(existing))
                {
                    best[key] = row;
                }
            }

            if (best.Count == 0) return;
            var deduped = Pool.AllRows.Where(row =>
            {
                string key = NormalizeEmailKey(row.Identifier);
                return key.Length == 0 || ReferenceEquals(best[key], row);
            }).ToList();
            if (deduped.Count == Pool.AllRows.Count) return;
            Pool.AllRows.Clear();
            foreach (PoolRow row in deduped) Pool.AllRows.Add(row);
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
                JsonElement payload = await desktopRead.ReadMailboxPoolAsync(GetChataiMailboxFilePath(), ct);
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
            Pool.AllRows.Add(new PoolRow
            {
                Id = "M" + (index + 1),
                CreatedAt = fileTime,
                CompletedAt = fileTime,
                Identifier = email,
                AccountType = MailboxPoolAccountType(provider),
                RegistrationStatus = JsonString(line, "registration_status"),
                Status = RegistrationStatusPresentation.MailboxStatus(
                    JsonString(line, "registration_status"), MailboxPoolStatus(provider, authMode)),
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
            if (provider is "chatai" or "graph") return "已授权";
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
            if (!string.IsNullOrWhiteSpace(Register.ChataiMailboxFilePath) && File.Exists(Register.ChataiMailboxFilePath))
                return Register.ChataiMailboxFilePath;

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

        private async Task LoadSessionPoolAsync(CancellationToken ct = default)
        {
            if (System.ComponentModel.DesignerProperties.GetIsInDesignMode(this)) return;
            try
            {
                JsonElement payload = await desktopRead.ReadAccountsAsync(ct);
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
            RememberAccountsCursor(payload);
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
                Pool.AllRows.Add(BuildBackendAccountRow(data, databasePath, rawJson));
            }
        }

        /// <summary>
        /// Applies an incremental accounts payload: only changed rows arrive,
        /// but `ids` is the complete current id set so deletions are visible.
        /// </summary>
        private void ApplyAccountsDelta(JsonElement payload)
        {
            if (!payload.TryGetProperty("accounts", out JsonElement accounts)
                || accounts.ValueKind != JsonValueKind.Array
                || !payload.TryGetProperty("ids", out JsonElement idsElement)
                || idsElement.ValueKind != JsonValueKind.Array)
            {
                // Shape drift: fall back to the full apply so the caller still
                // ends up coherent (the backend may have answered with the
                // plain list shape).
                ApplyAccountsPayload(payload);
                return;
            }
            var currentIds = new HashSet<string>(StringComparer.Ordinal);
            foreach (JsonElement id in idsElement.EnumerateArray())
            {
                currentIds.Add(id.ValueKind == JsonValueKind.Number
                    ? id.GetInt64().ToString(CultureInfo.InvariantCulture)
                    : (id.GetString() ?? ""));
            }
            string databasePath = GetDatabasePath();
            var changed = new List<PoolRow>();
            foreach (JsonElement account in accounts.EnumerateArray())
            {
                Dictionary<string, object> data = JsonElementToDictionary(account);
                string rawJson = account.TryGetProperty("session", out JsonElement sessionElement)
                    && sessionElement.ValueKind == JsonValueKind.Object
                    ? sessionElement.GetRawText()
                    : "{}";
                changed.Add(BuildBackendAccountRow(data, databasePath, rawJson));
            }
            Pool.MergeDatabaseRows(changed, currentIds, databasePath);
            RememberAccountsCursor(payload);
        }

        private void RememberAccountsCursor(JsonElement payload)
        {
            if (payload.TryGetProperty("cursor", out JsonElement cursor)
                && cursor.ValueKind == JsonValueKind.Number
                && cursor.TryGetInt64(out long value))
            {
                accountsCursor = value;
                // A full payload is the anti-drift anchor: restart the delta
                // budget from it.
                if (payload.TryGetProperty("full", out JsonElement full) && full.ValueKind == JsonValueKind.True)
                    incrementalAccountRefreshes = 0;
            }
        }

        /// <summary>
        /// One incremental accounts refresh. Returns false when the caller must
        /// do a full refresh instead (no cursor yet, budget exhausted, or the
        /// resident channel refused).
        /// </summary>
        private async Task<bool> TryApplyIncrementalAccountsAsync(CancellationToken ct)
        {
            if (accountsCursor <= 0 || incrementalAccountRefreshes >= MaxIncrementalAccountRefreshes)
                return false;
            try
            {
                JsonElement delta = await desktopRead
                    .ReadAccountsDeltaAsync(accountsCursor, ct)
                    .ConfigureAwait(true);
                ApplyAccountsDelta(delta);
            }
            catch (Exception ex)
            {
                Log("增量刷新账号失败，改用全量刷新：" + SensitiveDataSanitizer.Redact(ex.Message));
                incrementalAccountRefreshes = MaxIncrementalAccountRefreshes;
                return false;
            }
            incrementalAccountRefreshes++;
            Pool.UpdateOverview();
            Pool.RefreshPagedRows();
            StatusBar.Text = Pool.SummaryText;
            return true;
        }

        private PoolRow BuildBackendAccountRow(Dictionary<string, object> data, string databasePath, string rawJson)
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
            // `rawJson` is the full session blob (tens of KB per account) and both
            // GetImportedStatus / GetPaypalAmount parse it from scratch via
            // BackendJson.TextToObject. They used to be called twice each in the
            // initialiser below (GetPaypalAmount at both the PayPalAmount and the
            // PromotionStatus member, DisplayPayPalStatus likewise), i.e. 3 full
            // JSON parses per row where 1 suffices - and this runs once per
            // account on every pool refresh. Hoisted here so each is parsed once.
            string importedStatus = FirstNonEmpty(
                GetString(data, "imported_status"),
                AccountStatusInterpreter.GetImportedStatus(rawJson));
            string paypalAmount = FirstNonEmpty(
                GetString(data, "paypal_amount"),
                AccountStatusInterpreter.GetPaypalAmount(rawJson));
            // Pure function of its arguments, but it was evaluated twice with
            // identical arguments (PayPalStatus member and PromotionStatus member).
            string paypalStatusDisplay = AccountStatusInterpreter.DisplayPayPalStatus(paypalStatus, paypalOk, paypalUrl, paymentMethod);
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
                Status = AccountStatusInterpreter.DisplayAccountStatus(status, paypalOk, accessState, GetString(data, "error"), paypalStatus, refreshStatus, importedStatus),
                RegistrationStatus = FirstNonEmpty(GetString(data, "registration_status"), GetString(data, "registration_state")),
                PayPalStatus = paypalStatusDisplay,
                PayPalAmount = paypalAmount,
                // Python owns the display composition and sends it as
                // `promotion_display`; rows from an older backend carry only
                // `promotion_status`, hence the fallback.
                PromotionStatus = AccountStatusInterpreter.DisplayPromotionStatus(
                    FirstNonEmpty(
                        GetString(data, "promotion_display"),
                        GetString(data, "promotion_status")),
                    paypalStatusDisplay,
                    paypalAmount),
                PromotionState = GetString(data, "promotion_state"),
                RefreshTokenStatus = AccountStatusInterpreter.DisplayRtStatus(refreshStatus),
                TwoFactorStatus = AccountStatusInterpreter.HasTwoFactor(data) ? Loc.T("已设置") : Loc.T("未设置"),
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
            return row;
        }

        internal static string MailboxTypeDisplay(string provider, string email = "")
        {
            string normalized = (provider ?? "").Trim().ToLowerInvariant().Replace("-", "_");
            string domain = (email ?? "").Split('@').LastOrDefault()?.ToLowerInvariant() ?? "";
            return normalized switch
            {
                "remail" when domain is "outlook.com" or "hotmail.com" or "live.com" or "msn.com" => "remail/outlook",
                "remail" when domain == "icloud.com" => "remail/icloud",
                "remail" => "remail",
                "icloud_url" or "icloud" => "icloud",
                "cf_worker" or "cfworker" => "cfworker",
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
