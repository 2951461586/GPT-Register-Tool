using System.Text.Json;
using SmsWorkbench;

namespace SmsWorkbench.Tests;

public sealed class BackendTaskCoordinatorTests
{
    private static readonly string[] SensitiveCommandArguments =
    {
        "--proxy", "http://user:pass@example:80", "--count", "2"
    };

    [Fact]
    public async Task CoordinatorRejectsConcurrentRunsAndClearsState()
    {
        var client = new BlockingBackendClient();
        using var coordinator = new BackendTaskCoordinator(client);
        Task<BackendCommandResult> running = coordinator.RunAsync(BackendCommand.Create("first", Array.Empty<string>()));
        await client.Started.Task;

        Assert.True(coordinator.IsRunning);
        await Assert.ThrowsAsync<BackendTaskAlreadyRunningException>(() =>
            coordinator.RunAsync(BackendCommand.Create("second", Array.Empty<string>())));

        client.Complete.SetResult();
        await running;
        Assert.False(coordinator.IsRunning);
    }

    [Fact]
    public async Task ResultErrorsAreRedactedBySharedPolicy()
    {
        var client = new ImmediateBackendClient(new BackendCommandResult(
            1,
            "",
            "access_token=eyJabcdefgh.ijklmnop.qrstuvwx",
            null,
            false));
        using var coordinator = new BackendTaskCoordinator(client);

        InvalidOperationException error = await Assert.ThrowsAsync<InvalidOperationException>(() =>
            coordinator.RunForResultAsync(BackendCommand.Create("failure", Array.Empty<string>())));
        Assert.DoesNotContain("eyJabcdefgh", error.Message, StringComparison.Ordinal);
        Assert.Contains("[REDACTED]", error.Message, StringComparison.Ordinal);
    }

    [Fact]
    public void CommandArgumentsUseEmbeddedSharedPolicy()
    {
        string value = SensitiveDataSanitizer.RedactArguments(SensitiveCommandArguments);

        Assert.DoesNotContain("user:pass", value, StringComparison.Ordinal);
        Assert.Contains("--proxy [REDACTED]", value, StringComparison.Ordinal);
        Assert.Contains("--count 2", value, StringComparison.Ordinal);
    }

    private sealed class BlockingBackendClient : IBackendClient
    {
        public TaskCompletionSource Started { get; } = new(TaskCreationOptions.RunContinuationsAsynchronously);
        public TaskCompletionSource Complete { get; } = new(TaskCreationOptions.RunContinuationsAsynchronously);

        public async Task<BackendCommandResult> RunAsync(
            BackendCommand command,
            IProgress<BackendOutputLine> progress = null!,
            CancellationToken cancellationToken = default)
        {
            Started.SetResult();
            await Complete.Task.WaitAsync(cancellationToken);
            return new BackendCommandResult(0, "ok", "", null, false);
        }
    }

    private sealed class ImmediateBackendClient : IBackendClient
    {
        private readonly BackendCommandResult _result;
        public ImmediateBackendClient(BackendCommandResult result) => _result = result;

        public Task<BackendCommandResult> RunAsync(
            BackendCommand command,
            IProgress<BackendOutputLine> progress = null!,
            CancellationToken cancellationToken = default) => Task.FromResult(_result);
    }
}
