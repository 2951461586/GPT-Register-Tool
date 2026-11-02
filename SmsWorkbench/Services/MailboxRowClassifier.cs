// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch used to stay `annotations`.
#nullable enable

namespace SmsWorkbench
{
    /// <summary>
    /// Which mailbox provider a pool row came from, from the row's own fields.
    /// </summary>
    /// <remarks>
    /// Moved out of <c>MainWindow.Inbox.cs</c> (2026-11-01): the three predicates are
    /// pure, and they are consumed outside the inbox as well -- the registration
    /// request builder takes <see cref="IsCfWorker"/> as its decision, and
    /// <see cref="LocalUnregisteredVerdict"/> needs it to decide whether a row can be
    /// registered without a backend lookup. Keeping them in a partial made that shared
    /// rule look page-local.
    /// </remarks>
    public static class MailboxRowClassifier
    {
        public static bool IsCfWorker(PoolRow row)
        {
            if (row == null) return false;
            return row.MailboxProvider.Equals("cfworker", StringComparison.OrdinalIgnoreCase)
                || row.AccountType.Contains("CFWorker")
                || row.Identifier.EndsWith("@edu.liziai.cloud", StringComparison.OrdinalIgnoreCase)
                || row.Identifier.EndsWith("@liziai.cloud", StringComparison.OrdinalIgnoreCase)
                || row.RawLine.StartsWith("cfworker://", StringComparison.OrdinalIgnoreCase);
        }

        public static bool IsReMail(PoolRow row)
        {
            if (row == null) return false;
            return row.MailboxProvider.Equals("remail", StringComparison.OrdinalIgnoreCase)
                || row.AccountType.Contains("ReMail", StringComparison.OrdinalIgnoreCase);
        }

        public static bool IsSmailr(PoolRow row)
        {
            if (row == null) return false;
            return row.MailboxProvider.Equals("smailr", StringComparison.OrdinalIgnoreCase)
                || row.AccountType.Contains("Smailr", StringComparison.OrdinalIgnoreCase)
                || row.RawLine.StartsWith("smailr://", StringComparison.OrdinalIgnoreCase);
        }
    }
}
