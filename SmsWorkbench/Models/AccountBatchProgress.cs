// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch stays `annotations`.
#nullable enable

namespace SmsWorkbench
{
    public sealed class AccountBatchProgressTracker
    {
        private readonly HashSet<string> completedAccounts = new(StringComparer.OrdinalIgnoreCase);

        public AccountBatchProgressTracker(string domain, int total)
        {
            Domain = domain ?? "";
            Total = Math.Max(0, total);
        }

        public string Domain { get; }
        public int Total { get; private set; }
        public int Completed => completedAccounts.Count;

        public bool Update(BackendProgressEvent progress)
        {
            if (progress == null || !string.Equals(progress.Domain, Domain, StringComparison.OrdinalIgnoreCase))
                return false;
            if (progress.Total > 0)
                Total = progress.Total;
            if (!progress.Terminal || string.IsNullOrWhiteSpace(progress.AccountRef))
                return false;
            return completedAccounts.Add(progress.AccountRef.Trim());
        }
    }
}
