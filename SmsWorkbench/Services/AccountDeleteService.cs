// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch used to stay `annotations`.
#nullable enable

namespace SmsWorkbench
{
    /// <summary>Outcome of one batch delete: how many rows the backend could not remove.</summary>
    public sealed record AccountDeleteResult(int Requested, int Failed);

    /// <summary>
    /// Runs the batch-delete backend command and cleans up the plan's temp files.
    /// </summary>
    /// <remarks>
    /// Split from <c>MainWindow.Tasks.cs</c> (2026-11-01). The window keeps what is
    /// genuinely its own -- asking for confirmation, reporting the outcome and
    /// refreshing the pool afterwards -- while the invocation, the failure count and
    /// the temp-file cleanup live here. No WPF reference, so it is unit-testable with
    /// a stub coordinator.
    /// </remarks>
    public sealed class AccountDeleteService
    {
        private readonly IBackendTaskCoordinator tasks;
        private readonly Func<string, bool> deleteFile;
        private readonly Action<string> log;

        /// <param name="deleteFile">
        /// The window's file-delete helper: it logs a sanitized failure and is shared
        /// with the context menu, the inbox and the temp-file cleanup, so it stays in
        /// one place rather than being copied here.
        /// </param>
        public AccountDeleteService(
            IBackendTaskCoordinator tasks,
            Func<string, bool> deleteFile,
            Action<string> log)
        {
            this.tasks = tasks;
            this.deleteFile = deleteFile;
            this.log = log;
        }

        /// <summary>
        /// Delete <paramref name="emails"/>. Returns the backend's failure count; the
        /// caller decides how to report it. Temp files the planner created are removed
        /// here, on every exit path.
        /// </summary>
        public async Task<AccountDeleteResult> DeleteAsync(IReadOnlyList<string> emails, CancellationToken ct = default)
        {
            if (emails.Count == 0) return new AccountDeleteResult(0, 0);

            BackendCommandPlan? plan = null;
            try
            {
                plan = BackendCommandPlanner.CreateBatchDeleteAccounts(
                    emails.ToArray(),
                    workers: Math.Min(8, Math.Max(1, emails.Count)));
                BackendCommandResult backend = await tasks.RunAsync(
                    BackendCommand.Create(plan.TaskName, plan.Arguments.ToList(), plan.TimeoutMilliseconds ?? 120000),
                    cancellationToken: ct).ConfigureAwait(true);
                return new AccountDeleteResult(emails.Count, CountFailures(backend, emails.Count));
            }
            finally
            {
                if (plan != null)
                {
                    foreach (string path in plan.TempFiles)
                        deleteFile(path);
                }
            }
        }

        /// <summary>
        /// A non-zero exit or a payload without <c>ok</c> counts as "everything
        /// failed", which is what the caller reports; a partial backend answer is
        /// taken at its word.
        /// </summary>
        public static int CountFailures(BackendCommandResult backend, int expected)
        {
            if (backend.ExitCode != 0 || !backend.Payload.HasValue)
                return expected;
            JsonElement payload = backend.Payload.Value;
            if (payload.TryGetProperty("failed", out JsonElement failed) && failed.ValueKind == JsonValueKind.Number)
                return Math.Max(0, failed.GetInt32());
            return payload.TryGetProperty("ok", out JsonElement ok) && ok.ValueKind == JsonValueKind.True
                ? 0
                : expected;
        }
    }
}
