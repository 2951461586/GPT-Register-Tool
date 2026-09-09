// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch stays `annotations`.
#nullable enable

namespace SmsWorkbench
{
    /// <summary>
    /// Turns raw backend stdout into the operator-facing log panel story.
    ///
    /// stdout is the desktop IPC channel: result envelopes, per-run prints and
    /// pretty-printed JSON reports all share it. Machine consumers parse the
    /// raw stream (BackendJsonProtocol, the scan-result dialog) upstream of
    /// this presenter, so folding here is display-only. What the panel keeps:
    /// staged `[*]`/`[!]` lines, batch banners translated into stage headers,
    /// one folded line per JSON block instead of dozens of raw JSON lines, and
    /// nothing from the known hot-path noise sources (see
    /// <see cref="NoiseLinePrefixes"/>).
    ///
    /// Why the noise rule is a denylist and not an allowlist: the obvious
    /// allowlist -- keep only `[*]`/`[!]`/`[-]` lines and anything containing
    /// CJK -- also drops lines the operator genuinely needs, e.g. Python's
    /// `Skipped: phone number already registered, not saving to database`,
    /// which carries no marker and no Chinese but explains a missing row. The
    /// noisy emitters, by contrast, are a fixed and enumerable set.
    /// </summary>
    public static class BackendLogPresenter
    {
        private const string EnvelopePrefix = "@@SMSWORKBENCH_V2@@";

        /// <summary>Whole-line prefixes the panel never shows.
        ///
        /// Every entry fires per account per stage, so 26 accounts of
        /// `Existing account ...` step tracing is ~150 lines that carry
        /// nothing an operator can act on. Matched as a prefix and never as a
        /// substring: `Email OTP validate: ... 403 {...}` has to survive,
        /// because for some deactivated accounts that line is the only place
        /// the reason is spelled out.</summary>
        private static readonly string[] NoiseLinePrefixes =
        {
            "Protocol diagnostic[",
            "Existing account ",
            "Redirect[",
            "Signup username continue:",
            "Email verification page",
            "code:",
            "[*] Protocol auth session refreshed",
            "[*] Waiting for protocol auth session",
            "[*] Auth session refreshed",
            "[*] Waiting for auth session",
            "[*] Codex OAuth protocol stage",
            "[*] Passwordless OTP send",
            // Same emitter as the Passwordless pair above, different noun:
            // one fires per resend, the other per "still waiting" poll.
            "[*] Email OTP resend",
        };

        /// <summary>Whether a line comes from a known hot-path noise source.
        ///
        /// Leading progress dots are stripped before matching: dots are written
        /// with `end=""`, so a dot run can still glue itself to the head of a
        /// line when a worker flushes mid-poll. Stripping keeps the denylist
        /// effective even if the Python-side dot suppression is bypassed.
        /// </summary>
        public static bool IsNoiseLine(string trimmed)
        {
            if (trimmed.Length == 0)
                return false;
            string stripped = trimmed.TrimStart('.').TrimStart();
            if (stripped.Length == 0)
                return true;
            foreach (string prefix in NoiseLinePrefixes)
            {
                if (stripped.StartsWith(prefix, StringComparison.Ordinal))
                    return true;
            }
            return false;
        }

        /// <summary>Friendly task-start line; full CLI args stay in the task
        /// grid and the file log, not in the panel.
        ///
        /// Rendered as a banner bar so a new run is visually separable from the
        /// previous one. It is intentionally not a pure `====` bar — those are
        /// dropped by <see cref="IsBannerBar"/>.</summary>
        public static string TaskStartLine(string taskName)
            => $"=========== 启动：{taskName} ==========";

        /// <summary>
        /// Map one raw backend line to its panel form. Returns null when the
        /// line carries no operator value (protocol envelopes, banner bars).
        /// </summary>
        public static string? FormatLine(string? rawLine)
        {
            string line = rawLine ?? "";
            string trimmed = line.Trim();
            if (trimmed.Length == 0)
                return "";

            // Progress dots are written with `end=""`, so a dot run can glue
            // itself to the head of whichever line another worker flushes
            // next. Strip that run *before* any line-anchored test: a leading
            // dot run is precisely what used to defeat them, and it is how a
            // structured envelope escaped as raw JSON into the panel.
            string body = trimmed.TrimStart('.').TrimStart();
            if (body.Length == 0)
                return null;

            // A prefixed line that reaches the presenter failed event parsing
            // — it is the terminal result envelope (whole payload inline).
            if (body.StartsWith(EnvelopePrefix, StringComparison.Ordinal))
                return "[*] 任务返回结构化结果（详情见结果弹窗与任务列表）";

            if (IsNoiseLine(body))
                return null;

            if (IsBannerBar(body))
                return null;

            Match batchHeader = Regex.Match(
                body, @"^ChatGPT Email Batch Registration - (\d+) accounts?$");
            if (batchHeader.Success)
                return $"── 批量注册开始 · 共 {batchHeader.Groups[1].Value} 个账号 ──";

            Match accountHeader = Regex.Match(body, @"^Account (\d+)/(\d+)$");
            if (accountHeader.Success)
                return $"── 账号 {accountHeader.Groups[1].Value}/{accountHeader.Groups[2].Value} ──";

            // Strip the Python-side indent. `print(f"  Protocol ...")` exists
            // to make terminal output readable; in the panel the `[HH:mm:ss]`
            // stamp is prepended *after* this point (MainWindow.Helpers) so
            // the indent here is pure decoration. Keeping it meant bodies
            // started at two different columns and the wrapped continuation of
            // a long line -- which the TextBox flushes to the left edge --
            // lined up with neither. Cost: the nesting that `Phone Pool
            // Status` sub-lines used to express is gone; the panel trades it
            // for one shared left edge.
            //
            // Safe for pretty-printed JSON: those lines are captured by
            // BackendLogFolder before they ever reach here.
            return body;
        }

        private static readonly string[] ScanLikeDomains = { "account_scan", "account_promotion", "one_click_sms" };

        /// <summary>
        /// Panel line for one structured progress event, or null when the event
        /// carries no operator value for the log panel.
        ///
        /// The liveness, promotion and one-click SMS backends emit structured
        /// batch/account events. The task runner consumes them for progress
        /// tracking and this method renders the operator-facing subset without
        /// leaking raw envelopes into the panel.
        /// </summary>
        public static string? ProgressEventLine(BackendProgressEvent? progressEvent)
        {
            if (progressEvent == null) return null;
            string domain = progressEvent.Domain ?? "";
            if (Array.FindIndex(ScanLikeDomains,
                    d => string.Equals(d, domain, StringComparison.OrdinalIgnoreCase)) < 0)
                return null;

            string label = string.Equals(domain, "account_promotion", StringComparison.OrdinalIgnoreCase)
                ? "账号优惠检测"
                : string.Equals(domain, "one_click_sms", StringComparison.OrdinalIgnoreCase)
                    ? "一键接码"
                    : "账号测活";

            switch (progressEvent.Stage)
            {
                case "batch_started":
                    return progressEvent.Total > 0
                        ? $"── {label}开始 · 共 {progressEvent.Total} 个账号 ──"
                        : $"── {label}开始 ──";
                case "batch_completed":
                    {
                        string detail = (progressEvent.Detail ?? "").Trim();
                        return detail.Length > 0
                            ? $"── {label}结束 · {detail} ──"
                            : $"── {label}结束 ──";
                    }
                default:
                    // One-click SMS has no other operator-facing stream: retain
                    // account stages and terminal outcomes while keeping the
                    // high-volume scan/promotion rows folded as before.
                    if (!string.Equals(domain, "one_click_sms", StringComparison.OrdinalIgnoreCase))
                        return null;
                    string account = progressEvent.AccountRef ?? "";
                    string suffix = progressEvent.Detail?.Trim() ?? "";
                    if (suffix.Length == 0 && progressEvent.FailureClass.Length > 0)
                        suffix = progressEvent.FailureClass;
                    if (account.Length == 0)
                        return suffix.Length > 0 ? $"一键接码 · {suffix}" : null;
                    string state = progressEvent.Status switch
                    {
                        "success" or "completed" => "成功",
                        "failed" or "error" => "失败",
                        "cancelled" => "已取消",
                        _ => "进行中",
                    };
                    return suffix.Length > 0
                        ? $"一键接码 · {account} · {progressEvent.Stage} · {state} · {suffix}"
                        : $"一键接码 · {account} · {progressEvent.Stage} · {state}";
            }
        }

        /// <summary>Whether the line is a pure `====`/`####` banner bar.</summary>
        public static bool IsBannerBar(string trimmed)
            => trimmed.Length >= 4
                && (trimmed.All(c => c == '=') || trimmed.All(c => c == '#'));

        /// <summary>Whether a line looks like pretty-printed JSON and must be
        /// folded instead of displayed. `[*]`-style markers keep their leading
        /// bracket, so a bare JSON array opener is only matched at exact
        /// line start after trim.</summary>
        public static bool LooksLikeJson(string raw)
        {
            string trimmed = (raw ?? "").Trim();
            if (trimmed.StartsWith("{", StringComparison.Ordinal))
                return true;
            if (trimmed.StartsWith("[", StringComparison.Ordinal))
                return !trimmed.StartsWith("[*]", StringComparison.Ordinal)
                    && !trimmed.StartsWith("[!]", StringComparison.Ordinal)
                    && !trimmed.StartsWith("[-]", StringComparison.Ordinal);
            return trimmed.StartsWith("\"", StringComparison.Ordinal)
                || trimmed.StartsWith("}", StringComparison.Ordinal)
                || trimmed.StartsWith("]", StringComparison.Ordinal);
        }
    }

    /// <summary>
    /// Stateful line folder for one backend task run. Pretty-printed JSON
    /// blocks (multi-line) are buffered until they parse, then collapsed into
    /// a single summary line; everything else flows through FormatLine.
    /// </summary>
    public sealed class BackendLogFolder
    {
        private const int MaxBlockLines = 500;
        private readonly List<string> block = new();

        /// <summary>Feed one raw backend line; returns 0..n panel lines.</summary>
        public IReadOnlyList<string> Feed(string? rawLine)
        {
            string line = rawLine ?? "";
            string trimmed = line.Trim();

            if (block.Count > 0)
            {
                if (BackendLogPresenter.LooksLikeJson(trimmed) && block.Count < MaxBlockLines)
                {
                    block.Add(line);
                    if (TryParseBlock(out JsonDocument? parsed))
                        return FlushBlock(parsed);
                    return Array.Empty<string>();
                }
                // A non-JSON line ends an unterminated block: fold what was
                // buffered, then fall through to normal handling.
                TryParseBlock(out JsonDocument? dangling);
                List<string> prefix = FlushBlock(dangling);
                List<string> output = new(prefix);
                output.AddRange(Feed(line));
                return output;
            }

            if (BackendLogPresenter.LooksLikeJson(trimmed))
            {
                block.Add(line);
                if (TryParseBlock(out JsonDocument? parsed))
                    return FlushBlock(parsed);
                return Array.Empty<string>();
            }

            string? formatted = BackendLogPresenter.FormatLine(line);
            return formatted == null
                ? Array.Empty<string>()
                : new[] { formatted };
        }

        private bool TryParseBlock(out JsonDocument? parsed)
        {
            string candidate = string.Join("\n", block);
            try
            {
                parsed = JsonDocument.Parse(candidate);
                return true;
            }
            catch (JsonException)
            {
                parsed = null;
                return false;
            }
        }

        private List<string> FlushBlock(JsonDocument? parsed)
        {
            block.Clear();
            string summary = BackendJsonSummary.Summarize(parsed);
            parsed?.Dispose();
            return new List<string> { summary };
        }
    }

    /// <summary>One-line operator summaries for folded JSON payloads.</summary>
    public static class BackendJsonSummary
    {
        public static string Summarize(JsonDocument? parsed)
        {
            if (parsed == null)
                return "[*] 后端返回了无法解析的多行输出（已在日志中折叠）";
            JsonElement root = parsed.RootElement;
            if (root.ValueKind != JsonValueKind.Object)
                return "[*] 后端返回了结构化结果（已在日志中折叠）";

            bool hasResults = root.TryGetProperty("results", out JsonElement results)
                && results.ValueKind == JsonValueKind.Array;
            if (hasResults)
            {
                int total = IntField(root, "total", results.GetArrayLength());
                int ok = 0;
                int deactivated = 0;
                var failures = new List<string>();
                foreach (JsonElement item in results.EnumerateArray())
                {
                    if (item.ValueKind != JsonValueKind.Object)
                        continue;
                    bool itemOk = item.TryGetProperty("ok", out JsonElement okElement)
                        && okElement.ValueKind == JsonValueKind.True;
                    if (itemOk)
                        ok++;
                    string email = item.TryGetProperty("email", out JsonElement emailElement)
                        ? emailElement.GetString() ?? ""
                        : "";
                    string reason = ReasonOf(item);
                    if (!itemOk)
                        failures.Add($"{email}: {reason}");
                    if (reason.Contains("account_deactivated", StringComparison.OrdinalIgnoreCase))
                        deactivated++;
                }
                var summary = new StringBuilder("[*] 汇总：");
                summary.Append($"成功 {ok}/{total}");
                if (deactivated > 0)
                    summary.Append($" · 注销 {deactivated}");
                if (root.TryGetProperty("trial_eligible", out JsonElement trial)
                    && trial.ValueKind == JsonValueKind.Number
                    && trial.TryGetInt32(out int trialCount)
                    && trialCount > 0)
                    summary.Append($" · 可试优惠 {trialCount}");
                summary.Append($"（{failures.Count} 项失败明细已折叠）");
                return summary.ToString();
            }

            if (root.TryGetProperty("ok", out JsonElement okFlag)
                && okFlag.ValueKind == JsonValueKind.False)
            {
                string error = root.TryGetProperty("error", out JsonElement errorElement)
                    ? errorElement.ToString() ?? ""
                    : "";
                return $"[!] 后端返回失败：{Trim(error, 160)}";
            }

            return "[*] 后端返回了结构化结果（已在日志中折叠）";
        }

        private static string ReasonOf(JsonElement item)
        {
            foreach (string container in new[] { "probe", "relogin" })
            {
                if (item.TryGetProperty(container, out JsonElement nested)
                    && nested.ValueKind == JsonValueKind.Object
                    && nested.TryGetProperty("status", out JsonElement status))
                {
                    string value = status.GetString() ?? "";
                    if (value.Length > 0)
                        return value;
                }
            }
            if (item.TryGetProperty("error", out JsonElement error))
            {
                string value = error.ValueKind == JsonValueKind.String
                    ? error.GetString() ?? ""
                    : error.ToString();
                if (value.Length > 0)
                    return Trim(value, 80);
            }
            return "failed";
        }

        private static int IntField(JsonElement root, string name, int fallback)
            => root.TryGetProperty(name, out JsonElement value)
                && value.ValueKind == JsonValueKind.Number
                && value.TryGetInt32(out int number)
                ? number
                : fallback;

        private static string Trim(string value, int max)
        {
            string text = value.Replace("\n", " ").Trim();
            return text.Length <= max ? text : text[..max] + "…";
        }
    }
}
