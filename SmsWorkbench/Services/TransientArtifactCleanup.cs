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
