// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch used to stay `annotations`.
#nullable enable

namespace SmsWorkbench
{
    /// <summary>One export pass: the accepted items and how many rows were skipped.</summary>
    public sealed record CollectedAccountExport(IReadOnlyList<Dictionary<string, object>> Items, int Skipped);

    /// <summary>
    /// Turns pool rows into exportable account records: the TXT credential line and
    /// the raw session JSON.
    /// </summary>
    /// <remarks>
    /// <para>
    /// Window-independent: the account id and the mailbox line arrive as delegates,
    /// because both are window policy (the id is the digit prefix of a raw pool
    /// line, and the mailbox line comes from the resident backend plus the
    /// configured pool files). Everything that is a rule -- which session node wins,
    /// how a credential line is parsed, what counts as a duplicate -- is here and
    /// testable without a WPF window.
    /// </para>
    /// <para>
    /// The dedup key comes from <see cref="AccountExportJson"/>, the same helper the
    /// converters use, so a converted export cannot disagree with the raw one about
    /// what "the same account" means.
    /// </para>
    /// </remarks>
    public sealed class AccountExportAssembler
    {
        private readonly IDesktopReadClient desktopRead;
        private readonly Func<PoolRow, string> accountIdOf;
        private readonly Func<PoolRow, Task<string>> findMailboxLine;
        private readonly Func<string> defaultClientId;
        private readonly Action<string> log;

        public AccountExportAssembler(
            IDesktopReadClient desktopRead,
            Func<PoolRow, string> accountIdOf,
            Func<PoolRow, Task<string>> findMailboxLine,
            Func<string> defaultClientId,
            Action<string> log)
        {
            this.desktopRead = desktopRead;
            this.accountIdOf = accountIdOf;
            this.findMailboxLine = findMailboxLine;
            this.defaultClientId = defaultClientId;
            this.log = log;
        }

        /// <summary>
        /// The TXT export line, or null when the row cannot be exported. Async because
        /// the last-resort path asks the backend for the mailbox credential line.
        /// </summary>
        public async Task<string?> BuildExportLineAsync(PoolRow row)
        {
            if (row == null) return null;

            string source = await findMailboxLine(row).ConfigureAwait(true);
            if (source.Length == 0 && !string.IsNullOrWhiteSpace(row.RawLine))
            {
                source = row.RawLine;
            }

            if (!MailboxCredentialLineParser.TryParseMailboxExportParts(
                    source,
                    () => !string.IsNullOrWhiteSpace(row.ClientId) ? row.ClientId.Trim() : defaultClientId(),
                    out string email, out string password, out string clientId, out string refreshToken))
            {
                return null;
            }

            if (email.Length == 0 || password.Length == 0 || clientId.Length == 0 || refreshToken.Length == 0)
            {
                return null;
            }

            return email + "----" + password + "----" + clientId + "----" + refreshToken;
        }

        /// <summary>Collect the exportable session JSON, deduplicated by account.</summary>
        public async Task<CollectedAccountExport> CollectJsonAsync(
            IReadOnlyList<PoolRow> rows,
            CancellationToken ct = default)
        {
            var items = new List<Dictionary<string, object>>();
            var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            int skipped = 0;
            foreach (PoolRow row in rows)
            {
                Dictionary<string, object>? item = await BuildJsonAsync(row, ct).ConfigureAwait(true);
                if (item != null)
                {
                    string key = AccountExportJson.DedupKey(item, row.Identifier);
                    if (seen.Add(key))
                    {
                        items.Add(item);
                    }
                }
                else
                {
                    skipped++;
                }
            }
            return new CollectedAccountExport(items, skipped);
        }

        /// <summary>
        /// Returns null when the row has no exportable account data (no session file,
        /// or an empty/unusable payload) - the caller skips it.
        /// </summary>
        private async Task<Dictionary<string, object>?> BuildJsonAsync(PoolRow? row, CancellationToken ct)
        {
            if (row == null) return null;
            var data = new Dictionary<string, object>(StringComparer.OrdinalIgnoreCase);
            if (!await TryLoadAccountDataAsync(row, data, ct).ConfigureAwait(true) || data.Count == 0)
            {
                return null;
            }

            Dictionary<string, object> source = data;
            if (BackendJson.TryGetMap(data, "auth_session", out var authSession) && authSession.Count > 0)
            {
                source = authSession;
            }

            if (AccountExportJson.Clone(source) is not Dictionary<string, object> clean || clean.Count == 0)
            {
                return null;
            }

            AccountExportJson.EnsureEmail(clean, row.Identifier);
            AccountExportJson.EnsureRefreshToken(clean, data);

            return clean;
        }

        /// <summary>
        /// Load one account's raw export JSON into <paramref name="data"/>. Public
        /// because the detail panel's access-token resolution reads the same record;
        /// before the split it called a private window method.
        /// </summary>
        public async Task<bool> TryLoadAccountDataAsync(
            PoolRow row,
            Dictionary<string, object> data,
            CancellationToken ct = default)
        {
            if (row == null) return false;

            string source = (row.SourcePath ?? "").Trim();
            if (!source.EndsWith(".sqlite3", StringComparison.OrdinalIgnoreCase) || !File.Exists(source)) return false;
            try
            {
                JsonElement account = await desktopRead.ReadAccountExportAsync(accountIdOf(row), row.Identifier, ct)
                    .ConfigureAwait(true);
                foreach (KeyValuePair<string, object> item in BackendJson.ElementToDictionary(account))
                {
                    data[item.Key] = item.Value;
                }
                return data.Count > 0;
            }
            catch (Exception ex)
            {
                log(
                    "读取账号导出 backend 失败："
                    + SensitiveDataSanitizer.Redact(row.Identifier)
                    + " "
                    + SensitiveDataSanitizer.Redact(ex.Message));
                return false;
            }
        }
    }
}
