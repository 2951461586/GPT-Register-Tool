// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch used to stay `annotations`.
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
        private readonly AccountRefreshCursor accountCursor = new();

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
            AccountRefreshPlan plan = AccountRefreshPlanReader.Read(payload);
            accountCursor.Record(plan);
            // Per-refresh values hoisted out of the row loop; each used to
            // re-read and re-parse config.json once (or twice) per account.
            string databasePath = GetDatabasePath();
            foreach (JsonElement account in plan.Accounts)
            {
                Pool.AllRows.Add(BuildRowFromPayload(account, databasePath));
            }
        }

        /// <summary>
        /// Applies an incremental accounts payload: only changed rows arrive, but
        /// `ids` is the complete current id set so deletions are visible. The shape
        /// rules live in <see cref="AccountRefreshPlanReader"/>; a drifted payload
        /// (plain list shape) degrades to a full apply so the caller stays coherent.
        /// </summary>
        private void ApplyAccountsDelta(JsonElement payload)
        {
            AccountRefreshPlan plan = AccountRefreshPlanReader.Read(payload);
            if (!plan.IsDelta)
            {
                ApplyAccountsPayload(payload);
                return;
            }

            string databasePath = GetDatabasePath();
            var changed = plan.Accounts.Select(account => BuildRowFromPayload(account, databasePath)).ToList();
            Pool.MergeDatabaseRows(changed, plan.CurrentIds!, databasePath);
            accountCursor.Record(plan);
        }

        private PoolRow BuildRowFromPayload(JsonElement account, string databasePath)
        {
            Dictionary<string, object> data = JsonElementToDictionary(account);
            string rawJson = account.TryGetProperty("session", out JsonElement sessionElement)
                && sessionElement.ValueKind == JsonValueKind.Object
                ? sessionElement.GetRawText()
                : "{}";
            return AccountRowFactory.BuildBackendAccountRow(data, databasePath, rawJson);
        }

        /// <summary>
        /// One incremental accounts refresh. Returns false when the caller must
        /// do a full refresh instead (no cursor yet, budget exhausted, or the
        /// resident channel refused).
        /// </summary>
        private async Task<bool> TryApplyIncrementalAccountsAsync(CancellationToken ct)
        {
            if (!accountCursor.CanRefreshIncrementally(MaxIncrementalAccountRefreshes))
                return false;
            try
            {
                JsonElement delta = await desktopRead
                    .ReadAccountsDeltaAsync(accountCursor.Cursor, ct)
                    .ConfigureAwait(true);
                ApplyAccountsDelta(delta);
            }
            catch (Exception ex)
            {
                Log("增量刷新账号失败，改用全量刷新：" + SensitiveDataSanitizer.Redact(ex.Message));
                accountCursor.ForceFull(MaxIncrementalAccountRefreshes);
                return false;
            }
            accountCursor.RecordIncremental();
            Pool.UpdateOverview();
            Pool.RefreshPagedRows();
            StatusBar.Text = Pool.SummaryText;
            return true;
        }
    }
}
