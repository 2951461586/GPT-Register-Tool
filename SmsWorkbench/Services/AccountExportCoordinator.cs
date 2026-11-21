namespace SmsWorkbench.Services
{
    public sealed record AccountExportRunOutcome(
        string? OutputPath,
        int ExportedCount,
        int SkippedCount,
        string NormalizedFormat = "",
        string? ErrorMessage = null,
        bool MissingOutputFile = false);

    /// <summary>
    /// Window-independent coordinator for account TXT, raw JSON, and converted JSON
    /// exports plus default OAuth client-ID resolution.
    /// </summary>
    public sealed class AccountExportCoordinator
    {
        public static string DefaultMicrosoftOAuthClientId =>
            "9e5f94bc-e8a4-4e73-b8be-63364c29d753";

        public static string ResolveDefaultMailboxClientId(ISettingsService settingsService)
        {
            string configured = settingsService.GetString("email_registration.oauth_client_id").Trim();
            return configured.Length > 0 ? configured : DefaultMicrosoftOAuthClientId;
        }

        public async Task<AccountExportRunOutcome> ExportTxtAsync(
            AccountExportAssembler accountExport,
            IReadOnlyList<PoolRow> rows,
            string rootDir,
            Action<string> log)
        {
            var lines = new List<string>();
            var seen = new HashSet<string>(StringComparer.Ordinal);
            int skipped = 0;
            foreach (PoolRow row in rows)
            {
                string? line = await accountExport.BuildExportLineAsync(row).ConfigureAwait(true);
                if (line is null)
                {
                    skipped++;
                }
                else if (seen.Add(line))
                {
                    lines.Add(line);
                }
            }

            if (lines.Count == 0)
            {
                return new AccountExportRunOutcome(null, 0, skipped, "txt");
            }

            string outputDir = Path.Combine(rootDir, "runtime");
            Directory.CreateDirectory(outputDir);
            string outputPath = Path.Combine(outputDir, "account-" + DateTime.Now.ToString("yyyyMMdd_HHmmss") + ".txt");
            File.WriteAllLines(outputPath, lines, new UTF8Encoding(false));
            log("One-click export wrote " + lines.Count + " account(s), skipped " + skipped + ": " + outputPath);
            return new AccountExportRunOutcome(outputPath, lines.Count, skipped, "txt");
        }

        public async Task<AccountExportRunOutcome> ExportJsonAsync(
            AccountExportAssembler accountExport,
            IReadOnlyList<PoolRow> rows,
            string rootDir,
            Action<string> log,
            CancellationToken ct = default)
        {
            var collected = await accountExport.CollectJsonAsync(rows, ct).ConfigureAwait(true);
            if (collected.Items.Count == 0)
            {
                return new AccountExportRunOutcome(null, 0, collected.Skipped, "json");
            }

            string outputDir = Path.Combine(rootDir, "runtime", "account_json");
            Directory.CreateDirectory(outputDir);
            string outputPath = Path.Combine(outputDir, "account-" + DateTime.Now.ToString("yyyyMMdd_HHmmss") + ".json");
            object payload = collected.Items.Count == 1 ? collected.Items[0] : collected.Items;
            var options = new JsonSerializerOptions { WriteIndented = true };
            await Task.Run(
                () => File.WriteAllText(outputPath, JsonSerializer.Serialize(payload, options), new UTF8Encoding(false)),
                ct).ConfigureAwait(true);
            log("One-click JSON export wrote " + collected.Items.Count + " account(s), skipped " + collected.Skipped + ": " + outputPath);
            return new AccountExportRunOutcome(outputPath, collected.Items.Count, collected.Skipped, "json");
        }

        public async Task<AccountExportRunOutcome> ExportConvertedJsonAsync(
            AccountExportAssembler accountExport,
            IReadOnlyList<PoolRow> rows,
            string format,
            string rootDir,
            Func<string, List<string>, CancellationToken, Task<string>> runBackendWithResultAsync,
            Action<string> log,
            CancellationToken ct = default)
        {
            var collected = await accountExport.CollectJsonAsync(rows, ct).ConfigureAwait(true);
            string normalized = (format ?? "cpa").Trim().ToLowerInvariant();
            if (collected.Items.Count == 0)
            {
                return new AccountExportRunOutcome(null, 0, collected.Skipped, normalized);
            }

            IReadOnlyList<Dictionary<string, object>> items = collected.Items;
            int skipped = collected.Skipped;

            string outputDir = Path.Combine(rootDir, "runtime", "account_json");
            Directory.CreateDirectory(outputDir);
            string stamp = DateTime.Now.ToString("yyyyMMdd_HHmmss");
            string sourcePath = Path.Combine(Path.GetTempPath(), "account_export_source_" + stamp + ".json");
            string outputPath = Path.Combine(outputDir, "account-" + normalized + "-" + stamp + ".json");
            object payload = items.Count == 1 ? items[0] : items;
            var options = new JsonSerializerOptions { WriteIndented = true };
            await Task.Run(
                () => File.WriteAllText(sourcePath, JsonSerializer.Serialize(payload, options), new UTF8Encoding(false)),
                ct).ConfigureAwait(true);

            try
            {
                var plan = BackendCommandPlanner.CreateSessionConversion(sourcePath, normalized, outputPath);
                await runBackendWithResultAsync(plan.TaskName, plan.Arguments.ToList(), ct).ConfigureAwait(true);
            }
            catch (Exception ex)
            {
                log("账号格式转换失败：" + ex.Message);
                return new AccountExportRunOutcome(null, items.Count, skipped, normalized, ErrorMessage: ex.Message);
            }
            finally
            {
                try { if (File.Exists(sourcePath)) File.Delete(sourcePath); } catch { }
            }

            if (!File.Exists(outputPath) || new FileInfo(outputPath).Length == 0)
            {
                return new AccountExportRunOutcome(null, items.Count, skipped, normalized, MissingOutputFile: true);
            }

            log("One-click converted export wrote " + items.Count + " account(s), skipped " + skipped + ", format=" + normalized + ": " + outputPath);
            return new AccountExportRunOutcome(outputPath, items.Count, skipped, normalized);
        }
    }
}
