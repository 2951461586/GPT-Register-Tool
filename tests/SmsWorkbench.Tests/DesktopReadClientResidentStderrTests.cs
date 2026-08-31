using System.Diagnostics;
using System.Text.Json;
using System.Text.Json.Nodes;
using Serilog;
using Serilog.Core;
using SmsWorkbench;

namespace SmsWorkbench.Tests;

/// <summary>
/// Regression guard for the resident desktop-read channel's stderr pump.
///
/// The resident process redirects stderr, so it MUST be drained concurrently:
/// an unread pipe fills its ~4-8 KB buffer and the Python side then blocks on
/// write, wedging every pending request until the 120s timeout. These tests
/// start a real subprocess that floods stderr before serving stdout, which
/// fails the request under the old (un-drained) behaviour.
/// </summary>
public sealed class DesktopReadClientResidentStderrTests
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

    /// <summary>Flood stderr, then echo each request back on stdout.</summary>
    private const string ServeScript = """
        import sys, json

        chunk = "E" * 4096
        for _ in range(64):  # ~256 KB, far past the pipe buffer
            sys.stderr.write(chunk + "\n")
        sys.stderr.flush()

        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
            except Exception:
                continue
            req_id = req.get("id")
            sys.stdout.write(json.dumps({"id": req_id, "ok": True, "payload": {"echo": req.get("command", "accounts")}}) + "\n")
            sys.stdout.flush()
        """;

    [Fact]
    public async Task ResidentChannelDrainsStderrSoRequestsDoNotBlock()
    {
        string python = Environment.GetEnvironmentVariable("SMSWORKBENCH_TEST_PYTHON") ?? "python";
        if (!PythonAvailable(python))
            return; // No interpreter on this machine: treat as skipped.

        string root = Path.Combine(Path.GetTempPath(), "smswb-resident-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(root);
        string script = Path.Combine(root, "serve.py");
        File.WriteAllText(script, ServeScript);

        var paths = new StubPaths(root, script);
        var settings = new StubSettings(python);
        var client = new DesktopReadClient(new BackendTaskCoordinator(new StubBackendClient()), paths, settings, Logger.None);
        try
        {
            using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(30));
            Task<JsonElement> request = client.ReadAccountsAsync(cts.Token);
            Task winner = await Task.WhenAny(request, Task.Delay(TimeSpan.FromSeconds(25), cts.Token));
            if (winner != request)
                Assert.Fail("resident channel blocked: stderr pipe was not drained (requests would wedge)");

            JsonElement payload = await request;
            Assert.True(payload.TryGetProperty("echo", out JsonElement echo));
            Assert.Equal("accounts", echo.GetString());
        }
        finally
        {
            client.Dispose();
            try
            {
                Directory.Delete(root, true);
            }
            catch
            {
                // Best-effort cleanup; temp dir may be locked briefly.
            }
        }
    }

    private static bool PythonAvailable(string python)
    {
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
            if (process is null)
                return false;
            process.WaitForExit(5000);
            return process.HasExited && process.ExitCode == 0;
        }
        catch
        {
            return false;
        }
    }
}
