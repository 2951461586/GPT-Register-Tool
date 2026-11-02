// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch used to stay `annotations`.
#nullable enable

namespace SmsWorkbench
{
    /// <summary>
    /// The data behind the account-detail panel: the access token, the JSON file the
    /// row came from, and the "key: value" detail text.
    /// </summary>
    /// <remarks>
    /// <para>
    /// Split from <c>MainWindow.Detail.cs</c> (2026-11-01). The panel itself stayed in
    /// the window: its buttons call window commands (open path, copy, check alive,
    /// open PayPal), and moving it would have traded a dialog for a bag of delegates.
    /// Everything that is *data* moved here.
    /// </para>
    /// <para>
    /// No WPF reference, so the masking rules (which fields are secrets) and the
    /// "write the row's session to runtime/account_json" fallback are unit-testable.
    /// </para>
    /// </remarks>
    public sealed class AccountDetailAssembler
    {
        private readonly IDesktopReadClient desktopRead;
        private readonly Func<PoolRow, string> accountIdOf;
        private readonly Func<string> runtimeDirectory;
        private readonly Func<string, string> mask;
        private readonly Action<string> log;

        public AccountDetailAssembler(
            IDesktopReadClient desktopRead,
            Func<PoolRow, string> accountIdOf,
            Func<string> runtimeDirectory,
            Func<string, string> mask,
            Action<string> log)
        {
            this.desktopRead = desktopRead;
            this.accountIdOf = accountIdOf;
            this.runtimeDirectory = runtimeDirectory;
            this.mask = mask;
            this.log = log;
        }

        /// <summary>The account's access token, or "" when the record has none.</summary>
        public async Task<string> ResolveAccessTokenAsync(PoolRow row, CancellationToken ct = default)
        {
            var data = new Dictionary<string, object>(StringComparer.OrdinalIgnoreCase);
            if (!await LoadRowAsync(row, data, ct).ConfigureAwait(true) || data.Count == 0)
            {
                return "";
            }
            return BackendJson.FirstNonEmpty(
                BackendJson.GetString(data, "access_token"),
                BackendJson.GetString(data, "accessToken"),
                BackendJson.NestedString(data, "auth_session", "access_token"),
                BackendJson.NestedString(data, "auth_session", "accessToken"),
                BackendJson.NestedString(data, "session", "access_token"),
                BackendJson.NestedString(data, "session", "accessToken"));
        }

        /// <summary>
        /// The JSON file that best represents this row: its notes, its source file, the
        /// backend's <c>json_path</c>, or a freshly written copy of the session.
        /// </summary>
        public async Task<string> ResolveJsonPathAsync(PoolRow row, CancellationToken ct = default)
        {
            if (row == null) return "";
            string notes = (row.Notes ?? "").Trim();
            if (File.Exists(notes) && notes.EndsWith(".json", StringComparison.OrdinalIgnoreCase)) return notes;
            string source = (row.SourcePath ?? "").Trim();
            if (File.Exists(source) && source.EndsWith(".json", StringComparison.OrdinalIgnoreCase)) return source;
            if (!File.Exists(source) || !source.EndsWith(".sqlite3", StringComparison.OrdinalIgnoreCase)) return "";

            try
            {
                JsonElement payload = await desktopRead
                    .ReadAccountAsync(accountIdOf(row), row.Identifier, ct)
                    .ConfigureAwait(true);
                if (!payload.TryGetProperty("account", out JsonElement account)) return "";
                Dictionary<string, object> data = BackendJson.ElementToDictionary(account);
                string jsonPath = BackendJson.GetString(data, "json_path");
                if (File.Exists(jsonPath) && jsonPath.EndsWith(".json", StringComparison.OrdinalIgnoreCase)) return jsonPath;
                string rawJson = data.TryGetValue("session", out object? session) ? JsonSerializer.Serialize(session) : "";
                if (string.IsNullOrWhiteSpace(rawJson)) return "";
                string email = BackendJson.GetString(data, "email");
                string safeEmail = Regex.Replace((email ?? "unknown").Trim(), @"[^a-zA-Z0-9_.@+-]+", "_");
                string dir = Path.Combine(runtimeDirectory(), "account_json");
                Directory.CreateDirectory(dir);
                string outPath = Path.Combine(dir, "account_" + safeEmail + ".json");
                File.WriteAllText(outPath, PrettyJson(rawJson), new UTF8Encoding(false));
                return outPath;
            }
            catch (Exception ex)
            {
                log("打开账号JSON失败：" + ex.Message);
                return "";
            }
        }

        /// <summary>The detail panel's text: the row's own fields, then the stored account record.</summary>
        public async Task<string> BuildDetailAsync(PoolRow row, CancellationToken ct = default)
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
                    JsonElement payload = await desktopRead
                        .ReadAccountAsync(accountIdOf(row), row.Identifier, ct)
                        .ConfigureAwait(true);
                    if (payload.TryGetProperty("account", out JsonElement account))
                    {
                        foreach (KeyValuePair<string, object> item in BackendJson.ElementToDictionary(account))
                        {
                            lines.Add(item.Key + ": " + MaskSensitiveField(item.Key, Convert.ToString(item.Value) ?? ""));
                        }
                    }
                    return string.Join(Environment.NewLine, lines);
                }
            }
            catch (Exception ex)
            {
                lines.Add("detail_error: " + ex.Message);
            }
            return string.Join(Environment.NewLine, lines);
        }

        private async Task<bool> LoadRowAsync(PoolRow row, Dictionary<string, object> data, CancellationToken ct)
        {
            if (row == null) return false;
            string source = (row.SourcePath ?? "").Trim();
            if (!source.EndsWith(".sqlite3", StringComparison.OrdinalIgnoreCase) || !File.Exists(source)) return false;
            try
            {
                JsonElement account = await desktopRead
                    .ReadAccountExportAsync(accountIdOf(row), row.Identifier, ct)
                    .ConfigureAwait(true);
                foreach (KeyValuePair<string, object> item in BackendJson.ElementToDictionary(account))
                {
                    data[item.Key] = item.Value;
                }
                return data.Count > 0;
            }
            catch (Exception ex)
            {
                log("读取账号导出 backend 失败：" + row.Identifier + " " + ex.Message);
                return false;
            }
        }

        private static string PrettyJson(string rawJson)
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

        private string MaskSensitiveField(string key, string value)
        {
            string lower = (key ?? "").ToLowerInvariant();
            if (lower.Contains("token") || lower.Contains("cookie") || lower.Contains("password") || lower.Contains("api_key"))
            {
                return mask(value);
            }
            return value ?? "";
        }
    }
}
