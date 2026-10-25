// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch stays `annotations`.
#nullable enable

namespace SmsWorkbench
{
    /// <summary>
    /// Removes leftover credential files from <c>runtime/tmp</c>.
    ///
    /// The retired single-account protocol payment window wrote a manual
    /// <c>access_token</c> to <c>runtime/tmp/protocol_payment_at_&lt;guid&gt;.json</c>
    /// and deleted it on the normal exit path. A crash or a killed process left
    /// the file behind, so startup and shutdown both sweep the pattern. The
    /// writer is gone (2026-10-24); the sweep stays for installs that already
    /// carry a leftover file.
    ///
    /// Only that exact name pattern is touched: <c>runtime/tmp</c> is shared, and
    /// a wider sweep would turn a cleanup into data loss.
    /// </summary>
    public static class TransientArtifactCleanup
    {
        internal const string PaymentAccessTokenPattern = "protocol_payment_at_*.json";

        /// <summary>
        /// File name for one run's temporary credential file (selected-mailbox
        /// batch file, one-click SMS email list).
        ///
        /// <para>
        /// A plain <c>yyyyMMdd_HHmmss</c> timestamp was reused when a second
        /// action started in the same second: the new file overwrote the batch
        /// file the in-flight backend was still reading, and the overwrite was
        /// then deleted from under the running batch. A random suffix removes the
        /// collision without depending on clock resolution.
        /// </para>
        /// </summary>
        internal static string UniqueCredentialFileName(string prefix)
            => prefix + Guid.NewGuid().ToString("N") + ".txt";

        /// <summary>
        /// Runs <paramref name="body"/> and deletes <paramref name="paths"/> on
        /// <b>every</b> exit path: normal return, an early return inside the body,
        /// cancellation and a throw.
        ///
        /// <para>
        /// Callers write their temporary files <em>before</em> invoking the body
        /// (a selected-mailbox batch file holds raw mailbox credentials), so a
        /// cleanup placed inside the body leaks on exactly the paths that return
        /// before it is reached. This wrapper exists so that ordering bug has one
        /// owner and a unit-testable contract instead of living in WPF code.
        /// </para>
        ///
        /// <para>
        /// The continuation deliberately keeps the caller's context
        /// (<c>ConfigureAwait(true)</c>): the desktop's deletion delegate logs a
        /// failure through UI-bound state, and the body itself resumes on the
        /// dispatcher, so cleanup must not be the one hop that lands on a pool
        /// thread.
        /// </para>
        ///
        /// <param name="delete">
        /// Deletion strategy; defaults to <see cref="DeleteFiles"/>. The desktop
        /// passes its logging implementation so a locked file is still reported.
        /// </param>
        /// </summary>
        internal static async Task RunWithCleanupAsync(
            IReadOnlyList<string>? paths,
            Func<Task> body,
            Action<IReadOnlyList<string>>? delete = null)
        {
            ArgumentNullException.ThrowIfNull(body);
            try
            {
                await body().ConfigureAwait(true);
            }
            finally
            {
                (delete ?? DeleteFiles)(paths ?? Array.Empty<string>());
            }
        }

        /// <summary>
        /// Best-effort delete of <paramref name="paths"/>; missing, blank and
        /// locked entries are skipped rather than thrown, because this runs on
        /// the UI thread after a task that may already have failed.
        /// </summary>
        internal static void DeleteFiles(IReadOnlyList<string> paths)
        {
            foreach (string path in paths)
            {
                if (string.IsNullOrWhiteSpace(path)) continue;
                try
                {
                    File.Delete(path);
                }
                catch (IOException)
                {
                    // Locked by the backend that is still shutting down.
                }
                catch (UnauthorizedAccessException)
                {
                    // Read-only install.
                }
            }
        }

        /// <summary>
        /// Deletes matching files under <c>&lt;rootDirectory&gt;/runtime/tmp</c>.
        /// Best effort: an unreadable or locked file is skipped, never thrown,
        /// because this runs on the UI thread during startup/shutdown.
        /// </summary>
        /// <returns>The number of files actually deleted.</returns>
        public static int DeletePaymentAccessTokenFiles(string rootDirectory)
        {
            if (string.IsNullOrWhiteSpace(rootDirectory))
            {
                return 0;
            }

            string directory = Path.Combine(rootDirectory, "runtime", "tmp");
            if (!Directory.Exists(directory))
            {
                return 0;
            }

            int deleted = 0;
            foreach (string path in Directory.EnumerateFiles(directory, PaymentAccessTokenPattern, SearchOption.TopDirectoryOnly))
            {
                try
                {
                    File.Delete(path);
                    deleted++;
                }
                catch (IOException)
                {
                    // Locked by another process; the next startup retries.
                }
                catch (UnauthorizedAccessException)
                {
                    // Read-only install; the next startup retries.
                }
            }

            return deleted;
        }
    }
}
