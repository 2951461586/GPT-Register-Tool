// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch used to stay `annotations`.
#nullable enable

namespace SmsWorkbench
{
    /// <summary>
    /// Turns selected pool rows into the temporary mailbox batch file a
    /// registration or one-click-SMS batch consumes.
    /// </summary>
    /// <remarks>
    /// <para>
    /// Window-independent on purpose: the only two things it needs from the
    /// window are the mailbox-line lookup (which reads the resident backend and
    /// the configured pool files, and is shared with the inbox and export
    /// partials) and the CFWorker predicate. Both arrive as delegates, so the
    /// assembly rules -- which rows still need a lookup, which mailbox argument a
    /// batch uses, and that the file is a <b>temporary</b> credential file -- are
    /// testable without a WPF window.
    /// </para>
    /// <para>
    /// The file it writes holds raw mailbox credentials. <see cref="MailboxFileSelection.File"/>
    /// must reach the caller's <c>BackendCommandPlan.TemporaryFiles</c>; the
    /// planner documents why the operator's persistent pool file must never be
    /// declared there.
    /// </para>
    /// </remarks>
    public sealed class RegistrationRequestBuilder
    {
        private readonly Func<PoolRow?, CancellationToken, Task<string>> findMailboxLine;
        private readonly Func<PoolRow, bool> isCfWorker;

        public RegistrationRequestBuilder(
            Func<PoolRow?, CancellationToken, Task<string>> findMailboxLine,
            Func<PoolRow, bool> isCfWorker)
        {
            this.findMailboxLine = findMailboxLine;
            this.isCfWorker = isCfWorker;
        }

        /// <summary>Build the batch file for <paramref name="rows"/>, or null when none has a usable line.</summary>
        public async Task<MailboxFileSelection?> CreateBatchFileAsync(
            IEnumerable<PoolRow> rows,
            CancellationToken ct = default)
        {
            var lines = new List<string>();
            var mailboxArgs = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            foreach (PoolRow row in rows ?? Enumerable.Empty<PoolRow>())
            {
                string line = (row.RawLine ?? "").Trim().TrimStart('\ufeff');
                if (BackendCommandPlanner.MailboxArgumentForLine(line).Length == 0)
                {
                    line = await findMailboxLine(row, ct).ConfigureAwait(true);
                }
                string lineArg = BackendCommandPlanner.MailboxArgumentForLine(line);
                if (lineArg.Length > 0)
                {
                    lines.Add(line.Trim());
                    mailboxArgs.Add(lineArg);
                }
            }
            if (lines.Count == 0) return null;

            // The legacy parser is the compatibility superset for mixed provider selections.
            string mailboxArg = RegisterViewModel.MailboxArgument(mailboxArgs);
            // Unique file name, not a bare second-granularity timestamp: a second
            // click in the same second used to overwrite the file this batch was
            // still reading (and then delete it from under the running backend).
            string mailboxFile = Path.Combine(
                Path.GetTempPath(),
                TransientArtifactCleanup.UniqueCredentialFileName("selected_mailbox_"));
            File.WriteAllLines(mailboxFile, lines, new UTF8Encoding(false));
            return new MailboxFileSelection(mailboxArg, mailboxFile, lines.Count);
        }

        /// <summary>
        /// Build the batch file from the rows that still need registering, and
        /// report how many rows the local filter dropped.
        /// </summary>
        public async Task<PendingMailboxSelection> CreateUnregisteredBatchFileAsync(
            IReadOnlyList<PoolRow> selectedRows,
            CancellationToken ct = default)
        {
            var pending = new List<PoolRow>();
            foreach (PoolRow row in selectedRows)
            {
                if (await IsUnregisteredMailboxRowAsync(row, ct).ConfigureAwait(true)) pending.Add(row);
            }
            return new PendingMailboxSelection(
                await CreateBatchFileAsync(pending, ct).ConfigureAwait(true),
                pending.Count,
                selectedRows.Count);
        }

        private async Task<bool> IsUnregisteredMailboxRowAsync(PoolRow row, CancellationToken ct)
        {
            bool? local = RegisterViewModel.LocalUnregisteredVerdict(row, isCfWorker(row));
            if (local.HasValue) return local.Value;
            // Only reached when every local field is empty, so the backend
            // round-trip is the last resort rather than the common path.
            string line = await findMailboxLine(row, ct).ConfigureAwait(true);
            return !string.IsNullOrWhiteSpace(line);
        }
    }
}
