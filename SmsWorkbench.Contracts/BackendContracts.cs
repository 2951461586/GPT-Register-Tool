using System.Text.Json;

namespace SmsWorkbench
{
    public enum BackendOutputChannel
    {
        StandardOutput,
        StandardError
    }

    public sealed record BackendOutputLine(BackendOutputChannel Channel, string Text);

    public sealed record BackendCommand(
        string Name,
        IReadOnlyList<string> Arguments,
        TimeSpan Timeout,
        IReadOnlyDictionary<string, string> EnvironmentVariables)
    {
        public static BackendCommand Create(
            string name,
            IEnumerable<string> arguments,
            int timeoutMilliseconds = 120000,
            IReadOnlyDictionary<string, string> environmentVariables = null)
        {
            return new BackendCommand(
                name,
                (arguments ?? Array.Empty<string>()).ToArray(),
                TimeSpan.FromMilliseconds(Math.Max(1000, timeoutMilliseconds)),
                environmentVariables ?? new Dictionary<string, string>());
        }
    }

    public sealed record BackendCommandResult(
        int ExitCode,
        string StandardOutput,
        string StandardError,
        JsonElement? Payload,
        bool TimedOut)
    {
        public bool HasPayload => Payload.HasValue;
    }

    public interface IBackendClient
    {
        Task<BackendCommandResult> RunAsync(
            BackendCommand command,
            IProgress<BackendOutputLine> progress = null,
            CancellationToken cancellationToken = default);
    }
}
