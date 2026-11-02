// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch used to stay `annotations`.
#nullable enable

namespace SmsWorkbench
{
    /// <summary>
    /// Backend-inbox helpers that do not need a window: the failure text shown when
    /// the inbox command produced no result, and the OTP extraction used by the mail
    /// preview.
    /// </summary>
    /// <remarks>
    /// Split from <c>MainWindow.Inbox.cs</c> (2026-11-01). The dialog stays in the
    /// window (its buttons drive window commands); these two rules are pure and are
    /// what a test can pin without a UI.
    /// </remarks>
    public static class InboxReader
    {
        /// <summary>
        /// Operator-facing reason for an inbox command that produced no result: stderr
        /// when present, otherwise the redacted stdout, truncated so a wall of backend
        /// output cannot fill a dialog.
        /// </summary>
        public static string FailureMessage(BackendCommandResult result)
        {
            string message = string.IsNullOrWhiteSpace(result.StandardError)
                ? SensitiveDataSanitizer.Redact(result.StandardOutput)
                : result.StandardError;
            message = (message ?? "").Trim();
            if (message.Length > 800) message = string.Concat(message.AsSpan(0, 800), "...");
            return message.Length > 0
                ? message
                : $"backend exited with code {result.ExitCode}, but produced no IPC result";
        }

        /// <summary>The first 5-8 digit run that is not part of a longer number.</summary>
        public static string ExtractCode(string text)
        {
            Match match = Regex.Match(text ?? "", @"(?<!\d)\d{5,8}(?!\d)");
            return match.Success ? match.Value : "";
        }
    }
}
