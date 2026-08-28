using System.Text.Json;

namespace SmsWorkbench.WebHost.Tests;

public sealed class BackendJobManagerTests
{
    [Fact]
    public async Task StartRunsToCompletionAndStoresEvents()
    {
        var client = new StubBackendClient(
            new BackendCommandResult(0, "ok", "", JsonDocument.Parse("{\"ok\":true}").RootElement.Clone(), false));
        var manager = new BackendJobManager(client);
        var plan = new BackendCommandPlan("test", new[] { "--count", "1" });

        Assert.True(manager.TryStart("registration", plan, out BackendJobDto? job));
        Assert.Equal(BackendJobState.Queued, job.State);

        await WaitForTerminalAsync(manager, job.Id);
        BackendJobDto? final = manager.Get(job.Id);
        Assert.NotNull(final);
        Assert.Equal(BackendJobState.Succeeded, final!.State);
        Assert.Equal(0, final.ExitCode);
        Assert.True(final.Result.HasValue);

        IReadOnlyList<BackendJobEventDto> events = manager.EventsAfter(job.Id, 0);
        Assert.NotEmpty(events);
        Assert.Contains(events, e => e.Type == "state");
    }

    [Fact]
    public async Task SecondStartReturnsConflict()
    {
        var client = new StubBackendClient(
            new BackendCommandResult(0, "", "", null, false), delayMs: 500);
        var manager = new BackendJobManager(client);
        var plan = new BackendCommandPlan("test", new[] { "--count", "1" });

        Assert.True(manager.TryStart("registration", plan, out _));
        Assert.False(manager.TryStart("registration", plan, out _));

        await WaitForTerminalAsync(manager, manager.List().First().Id);
    }

    [Fact]
    public async Task CancelTerminatesRunningJob()
    {
        var client = new StubBackendClient(
            new BackendCommandResult(0, "", "", null, false), delayMs: 5000);
        var manager = new BackendJobManager(client);
        var plan = new BackendCommandPlan("test", new[] { "--count", "1" });

        Assert.True(manager.TryStart("registration", plan, out BackendJobDto? job));
        await Task.Delay(100);
        Assert.True(manager.Cancel(job.Id));

        await WaitForTerminalAsync(manager, job.Id);
        BackendJobDto? final = manager.Get(job.Id);
        Assert.NotNull(final);
        Assert.Equal(BackendJobState.Cancelled, final!.State);
    }

    [Fact]
    public async Task NonZeroExitYieldsFailedState()
    {
        var client = new StubBackendClient(
            new BackendCommandResult(3, "output", "error", null, false));
        var manager = new BackendJobManager(client);
        var plan = new BackendCommandPlan("test", new[] { "--count", "1" });

        Assert.True(manager.TryStart("registration", plan, out BackendJobDto? job));
        await WaitForTerminalAsync(manager, job.Id);
        BackendJobDto? final = manager.Get(job.Id);
        Assert.NotNull(final);
        Assert.Equal(BackendJobState.Failed, final!.State);
        Assert.Equal(3, final.ExitCode);
    }

    [Fact]
    public async Task ProgressEventsAreCapturedBeforeTerminal()
    {
        string envelope = BackendProgressEventParser.Prefix +
            """{"schema":"smsworkbench.ipc.v2","version":2,"type":"event","run_id":"r1","sequence":1,"timestamp_ms":0,"terminal":false,"payload":{"stage":"probing","status":"running","detail":"HTTP 200"}}""";
        var client = new StubBackendClient(
            new BackendCommandResult(0, envelope, "", null, false),
            outputLines: new[] { envelope });
        var manager = new BackendJobManager(client);
        var plan = new BackendCommandPlan("test", new[] { "--count", "1" });

        Assert.True(manager.TryStart("test", plan, out BackendJobDto? job));
        await WaitForTerminalAsync(manager, job.Id);
        IReadOnlyList<BackendJobEventDto> events = manager.EventsAfter(job.Id, 0);
        Assert.Contains(events, e => e.Type == "progress");
    }

    private static async Task WaitForTerminalAsync(IBackendJobManager manager, Guid jobId)
    {
        for (int i = 0; i < 100; i++)
        {
            BackendJobDto? job = manager.Get(jobId);
            if (job is null) return;
            if (job.State is BackendJobState.Succeeded or BackendJobState.Failed or BackendJobState.Cancelled)
                return;
            await Task.Delay(50);
        }
    }

    private sealed class StubBackendClient : IBackendClient
    {
        private readonly BackendCommandResult _result;
        private readonly int _delayMs;
        private readonly string[]? _outputLines;

        public StubBackendClient(BackendCommandResult result, int delayMs = 0, string[]? outputLines = null)
        {
            _result = result;
            _delayMs = delayMs;
            _outputLines = outputLines;
        }

        public async Task<BackendCommandResult> RunAsync(
            BackendCommand command,
            IProgress<BackendOutputLine>? progress = null,
            CancellationToken cancellationToken = default)
        {
            if (_outputLines is not null)
            {
                await Task.Delay(10, cancellationToken);
                foreach (string line in _outputLines)
                    progress?.Report(new BackendOutputLine(BackendOutputChannel.StandardOutput, line));
            }
            if (_delayMs > 0)
            {
                try { await Task.Delay(_delayMs, cancellationToken); }
                catch (OperationCanceledException) { throw; }
            }
            return _result;
        }
    }
}
