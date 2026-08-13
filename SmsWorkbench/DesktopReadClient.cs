using System.Text.Json;

namespace SmsWorkbench
{
    public interface IDesktopReadClient
    {
        Task<JsonElement> ReadAccountsAsync(CancellationToken cancellationToken = default);
        Task<JsonElement> ReadMailboxPoolAsync(string selectedFile = "", CancellationToken cancellationToken = default);
        Task<JsonElement> ReadAccountAsync(string accountId, string email = "", CancellationToken cancellationToken = default);
        Task<JsonElement> ReadAccountExportAsync(string accountId, string email = "", CancellationToken cancellationToken = default);
        Task<string> ReadMailboxLineAsync(string accountId, string email = "", CancellationToken cancellationToken = default);
        Task<string> ReadPaymentUrlAsync(string accountId, string email = "", CancellationToken cancellationToken = default);
    }

    public sealed class DesktopReadClient : IDesktopReadClient
    {
        private static readonly string[] ReadAccountsArguments = ["--desktop-read", "accounts", "--desktop-ipc"];
        private readonly IBackendTaskCoordinator _backend;

        public DesktopReadClient(IBackendTaskCoordinator backend) => _backend = backend;

        public Task<JsonElement> ReadAccountsAsync(CancellationToken cancellationToken = default) =>
            RunAsync("Read account index", ReadAccountsArguments, cancellationToken);

        public Task<JsonElement> ReadMailboxPoolAsync(string selectedFile = "", CancellationToken cancellationToken = default)
        {
            var args = new List<string> { "--desktop-read", "mailbox-pool", "--desktop-ipc" };
            if (!string.IsNullOrWhiteSpace(selectedFile)) args.AddRange(["--chatai-mailbox-file", selectedFile]);
            return RunAsync("Read mailbox pool", args, cancellationToken);
        }

        public Task<JsonElement> ReadAccountAsync(string accountId, string email = "", CancellationToken cancellationToken = default)
        {
            var args = BuildArguments("account", accountId, email);
            return RunAsync("Read account detail", args, cancellationToken);
        }

        public async Task<JsonElement> ReadAccountExportAsync(string accountId, string email = "", CancellationToken cancellationToken = default)
        {
            string content = await ReadTemporaryTextAsync(
                "Read account export", "account-file", "smsworkbench_account_",
                accountId, email, cancellationToken).ConfigureAwait(false);
            using JsonDocument document = JsonDocument.Parse(content);
            return document.RootElement.Clone();
        }

        public Task<string> ReadMailboxLineAsync(string accountId, string email = "", CancellationToken cancellationToken = default) =>
            ReadTemporaryTextAsync(
                "Read mailbox credential", "mailbox-file", "smsworkbench_mailbox_",
                accountId, email, cancellationToken);

        public Task<string> ReadPaymentUrlAsync(string accountId, string email = "", CancellationToken cancellationToken = default) =>
            ReadTemporaryTextAsync(
                "Read payment URL", "payment-url-file", "smsworkbench_payment_url_",
                accountId, email, cancellationToken);

        private async Task<string> ReadTemporaryTextAsync(
            string commandName,
            string operation,
            string expectedPrefix,
            string accountId,
            string email,
            CancellationToken cancellationToken)
        {
            JsonElement payload = await RunAsync(
                commandName, BuildArguments(operation, accountId, email), cancellationToken).ConfigureAwait(false);
            string path = payload.TryGetProperty("path", out JsonElement value) ? value.GetString() ?? "" : "";
            string fullPath = ValidateTemporaryPath(path, expectedPrefix);
            try
            {
                return await File.ReadAllTextAsync(fullPath, cancellationToken).ConfigureAwait(false);
            }
            finally
            {
                try { File.Delete(fullPath); } catch { }
            }
        }

        private static List<string> BuildArguments(string operation, string accountId, string email)
        {
            var args = new List<string> { "--desktop-read", operation, "--desktop-ipc" };
            if (!string.IsNullOrWhiteSpace(accountId)) args.AddRange(["--account-id", accountId]);
            if (!string.IsNullOrWhiteSpace(email)) args.AddRange(["--email", email]);
            return args;
        }

        private static string ValidateTemporaryPath(string path, string expectedPrefix)
        {
            if (string.IsNullOrWhiteSpace(path))
                throw new InvalidOperationException("Desktop read backend returned no temporary file");
            string fullPath = Path.GetFullPath(path);
            string tempRoot = Path.GetFullPath(Path.GetTempPath());
            if (!fullPath.StartsWith(tempRoot, StringComparison.OrdinalIgnoreCase)
                || !Path.GetFileName(fullPath).StartsWith(expectedPrefix, StringComparison.Ordinal))
                throw new InvalidOperationException("Desktop read backend returned an invalid temporary file path");
            return fullPath;
        }

        private async Task<JsonElement> RunAsync(string name, IEnumerable<string> args, CancellationToken cancellationToken)
        {
            BackendCommandResult result = await _backend.RunAsync(
                BackendCommand.Create(name, args, 120000), cancellationToken: cancellationToken).ConfigureAwait(false);
            if (!result.Payload.HasValue) throw new InvalidOperationException("Desktop read backend returned no payload");
            JsonElement payload = result.Payload.Value;
            if (payload.TryGetProperty("ok", out JsonElement ok) && !ok.GetBoolean())
                throw new InvalidOperationException(
                    payload.TryGetProperty("error", out JsonElement error) ? error.GetString() : "Desktop read failed");
            return payload;
        }
    }
}
