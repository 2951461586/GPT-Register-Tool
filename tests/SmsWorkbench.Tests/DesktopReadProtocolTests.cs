using System.Diagnostics;
using System.Text.Json;
using System.Text.Json.Nodes;
using Serilog.Core;
using SmsWorkbench;

namespace SmsWorkbench.Tests;

/// <summary>
/// Regression guards for the resident desktop-read channel contract:
/// version negotiation, structured error codes, and the pending-response leak.
///
/// These drive a real subprocess so the handshake and the timeout paths are
/// exercised end to end rather than against a hand-rolled fake.
/// </summary>
public sealed class DesktopReadProtocolTests
{
    private sealed class StubPaths : IApplicationPaths
    {
        public StubPaths(string root, string script)
        {
            RootDirectory = root;
            BackendScriptPath = script;
        }

        public string RootDirectory { get; }

        public string BackendScriptPath { get; }
    }

    private sealed class StubSettings : ISettingsService
    {
        private readonly string _python;

        public StubSettings(string python) => _python = python;

        public string ConfigPath => "";

        public IReadOnlyList<SettingsCategoryViewModel> Load() => Array.Empty<SettingsCategoryViewModel>();

        public SettingsSaveResult Save(IEnumerable<SettingsCategoryViewModel> categories) => new(true, null);

        public string GetString(string path, string fallback = "") =>
            path == "runtime.python_path" ? _python : fallback;

        public IReadOnlyList<string> GetStringList(string path) => Array.Empty<string>();

        public void UpdateConfig(Action<JsonObject> mutate)
        {
        }
    }

    /// <summary>Records one-shot invocations so a fallback is observable.</summary>
    private sealed class RecordingBackendClient : IBackendClient
    {
        private readonly string _payload;

        public RecordingBackendClient(string payload) => _payload = payload;

        public List<string> Invocations { get; } = new();

        public Task<BackendCommandResult> RunAsync(
            BackendCommand command,
            IProgress<BackendOutputLine>? progress = null,
            CancellationToken cancellationToken = default)
        {
            lock (Invocations)
                Invocations.Add(string.Join(' ', command.Arguments));
            return Task.FromResult(new BackendCommandResult(
                0,
                _payload,
                "",
                JsonDocument.Parse(_payload).RootElement.Clone(),
                false));
        }
    }

    /// <param name="reportedVersion">Value the fake backend returns from <c>hello</c>.</param>
    /// <param name="answerRealOps">
    /// When false the backend answers <c>hello</c> and then goes silent, which
    /// is how the timeout paths are reached.
    /// </param>
    // $$$ (three) because the script body contains `}}`; with fewer the
    // compiler would read that as the end of an interpolation hole.
    private static string BuildScript(int reportedVersion, bool answerRealOps) => $$$"""
        import sys, json

        ANSWER = {{{ (answerRealOps ? "True" : "False") }}}
        VERSION = {{{reportedVersion}}}

        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
            except Exception:
                continue
            op = req.get("op")
            if op == "hello":
                out = {"id": req.get("id"), "ok": True,
                       "payload": {"protocol": VERSION, "ops": ["accounts"]}}
            elif op == "ping":
                out = {"id": req.get("id"), "ok": True, "payload": {"pong": True}}
            elif not ANSWER:
                continue  # wedge on purpose
            else:
                out = {"id": req.get("id"), "ok": True, "payload": {"op": op}}
            sys.stdout.write(json.dumps(out) + "\n")
            sys.stdout.flush()
        """;

    private static (DesktopReadClient Client, StubPaths Paths, string Root, RecordingBackendClient Backend) Build(
        string script, string oneShotPayload = """{"ok":true,"accounts":[]}""")
    {
        string root = Path.Combine(Path.GetTempPath(), "smswb-proto-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(root);
        string path = Path.Combine(root, "serve.py");
        File.WriteAllText(path, script);
        string python = Environment.GetEnvironmentVariable("SMSWORKBENCH_TEST_PYTHON") ?? "python";
        var backend = new RecordingBackendClient(oneShotPayload);
        var client = new DesktopReadClient(
            new BackendTaskCoordinator(backend),
            new StubPaths(root, path),
            new StubSettings(python),
            Logger.None);
        return (client, new StubPaths(root, path), root, backend);
    }

    private static bool PythonAvailable()
    {
        string python = Environment.GetEnvironmentVariable("SMSWORKBENCH_TEST_PYTHON") ?? "python";
        try
        {
            var psi = new ProcessStartInfo(python, "--version")
            {
                RedirectStandardError = true,
                RedirectStandardOutput = true,
                UseShellExecute = false,
                CreateNoWindow = true,
            };
            using Process? process = Process.Start(psi);
            if (process is null) return false;
            process.WaitForExit(5000);
            return process.HasExited && process.ExitCode == 0;
        }
        catch
        {
            return false;
        }
    }

    // ── Error codes ────────────────────────────────────────────────────

    [Theory]
    [InlineData(DesktopReadErrorCode.None, "none")]
    [InlineData(DesktopReadErrorCode.BadRequest, "bad_request")]
    [InlineData(DesktopReadErrorCode.UnknownOperation, "unknown_operation")]
    [InlineData(DesktopReadErrorCode.BackendError, "backend_error")]
    [InlineData(DesktopReadErrorCode.WatchdogTimeout, "watchdog_timeout")]
    [InlineData(DesktopReadErrorCode.Internal, "internal")]
    [InlineData(DesktopReadErrorCode.Timeout, "timeout")]
    [InlineData(DesktopReadErrorCode.Cancelled, "cancelled")]
    [InlineData(DesktopReadErrorCode.ProtocolMismatch, "protocol_mismatch")]
    [InlineData(DesktopReadErrorCode.ChannelUnavailable, "channel_unavailable")]
    public void ErrorCodesSurviveAWireRoundTrip(DesktopReadErrorCode code, string wire)
    {
        Assert.Equal(wire, DesktopReadErrorCodes.ToWire(code));
        Assert.Equal(code, DesktopReadErrorCodes.Parse(wire));
    }

    [Fact]
    public void UnknownWireCodesDegradeToNoneRatherThanThrowing()
    {
        Assert.Equal(DesktopReadErrorCode.None, DesktopReadErrorCodes.Parse("something_new_from_a_later_release"));
        Assert.Equal(DesktopReadErrorCode.None, DesktopReadErrorCodes.Parse(null));
        Assert.Equal(DesktopReadErrorCode.None, DesktopReadErrorCodes.Parse("   "));
    }

    [Fact]
    public void HeartbeatCeilingIsShorterThanTheRequestCeiling()
    {
        // If the probe were as slow as a request, discovering a wedge would
        // cost as much as suffering one.
        Assert.True(DesktopReadProtocol.HeartbeatTimeout < DesktopReadProtocol.RequestTimeout);
        Assert.True(DesktopReadProtocol.HandshakeTimeout < DesktopReadProtocol.RequestTimeout);
    }

    // ── Version negotiation ────────────────────────────────────────────

    [Fact]
    public async Task MismatchedProtocolVersionFallsBackToOneShotReads()
    {
        if (!PythonAvailable()) return;

        var (client, _, root, backend) = Build(BuildScript(DesktopReadProtocol.Version + 99, answerRealOps: true));
        try
        {
            using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(40));
            JsonElement payload = await client.ReadAccountsAsync(cts.Token);

            Assert.True(payload.TryGetProperty("accounts", out _), "one-shot payload was not used");
            lock (backend.Invocations)
                Assert.Contains(backend.Invocations, args => args.Contains("--desktop-read"));
        }
        finally
        {
            client.Dispose();
            TryDelete(root);
        }
    }

    [Fact]
    public async Task MatchingProtocolVersionUsesTheResidentChannel()
    {
        if (!PythonAvailable()) return;

        var (client, _, root, backend) = Build(BuildScript(DesktopReadProtocol.Version, answerRealOps: true));
        try
        {
            using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(40));
            JsonElement payload = await client.ReadAccountsAsync(cts.Token);

            Assert.True(payload.TryGetProperty("op", out JsonElement op));
            Assert.Equal("accounts", op.GetString());
            lock (backend.Invocations)
                Assert.Empty(backend.Invocations); // no cold start was needed
        }
        finally
        {
            client.Dispose();
            TryDelete(root);
        }
    }

    // ── Pending-response leak ──────────────────────────────────────────

    /// <summary>
    /// The leak this guards: a request that timed out or was cancelled stayed
    /// in the channel's pending dictionary forever, because the dictionary is
    /// only drained when a response actually arrives. Every abandoned request
    /// rooted its TaskCompletionSource until the channel was disposed, which
    /// in a long-lived workbench is never — so the count grew without bound.
    /// </summary>
    [Fact]
    public async Task AbandonedRequestsDoNotAccumulateInThePendingMap()
    {
        if (!PythonAvailable()) return;

        var (client, _, root, backend) = Build(BuildScript(DesktopReadProtocol.Version, answerRealOps: false));
        client.ResidentRequestTimeout = TimeSpan.FromMilliseconds(400);
        try
        {
            using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(60));

            // Each of these hits the resident channel, times out, and is caught
            // by the public method, which then falls back to a one-shot read.
            // The fallback count is therefore the number of abandoned requests.
            await client.ReadAccountsAsync(cts.Token);
            await client.ReadAccountsAsync(cts.Token);
            await client.ReadMailboxPoolAsync("", cts.Token);

            lock (backend.Invocations)
                Assert.Equal(3, backend.Invocations.Count); // 3 timeouts really happened
            Assert.Equal(0, client.PendingResidentRequests);
        }
        finally
        {
            client.Dispose();
            TryDelete(root);
        }
    }

    [Fact]
    public async Task CancelledRequestsDoNotAccumulateInThePendingMap()
    {
        if (!PythonAvailable()) return;

        var (client, _, root, backend) = Build(BuildScript(DesktopReadProtocol.Version, answerRealOps: false));
        client.ResidentRequestTimeout = TimeSpan.FromSeconds(30);
        try
        {
            using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(60));
            using var requestCts = new CancellationTokenSource();

            Task<JsonElement> pending = client.ReadAccountsAsync(requestCts.Token);

            // Wait until the request is genuinely in flight. Cancelling during
            // the handshake instead disposes the whole channel, which makes
            // the assertion below vacuously true (a null channel reports 0) —
            // this loop is what turns the test into a real guard.
            for (int i = 0; i < 200 && client.PendingResidentRequests == 0; i++)
                await Task.Delay(50, cts.Token);
            Assert.Equal(1, client.PendingResidentRequests); // in flight, not yet answered

            requestCts.Cancel();
            await pending; // cancellation is absorbed into the one-shot fallback

            lock (backend.Invocations)
                Assert.Single(backend.Invocations); // one abandoned request
            Assert.Equal(0, client.PendingResidentRequests);
        }
        finally
        {
            client.Dispose();
            TryDelete(root);
        }
    }

    private static void TryDelete(string root)
    {
        try
        {
            Directory.Delete(root, true);
        }
        catch
        {
            // Best-effort; the temp dir may be locked briefly.
        }
    }
}
