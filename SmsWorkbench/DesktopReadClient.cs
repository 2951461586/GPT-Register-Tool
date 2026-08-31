using Serilog;
using System.Diagnostics;
using System.Text;
using System.Text.Json;

namespace SmsWorkbench
{
    public interface IDesktopReadClient
    {
        Task<JsonElement> ReadPoolsAsync(string selectedFile = "", CancellationToken cancellationToken = default);
        Task<JsonElement> ReadAccountsAsync(CancellationToken cancellationToken = default);
        Task<JsonElement> ReadMailboxPoolAsync(string selectedFile = "", CancellationToken cancellationToken = default);
        Task<JsonElement> ReadAccountAsync(string accountId, string email = "", CancellationToken cancellationToken = default);
        Task<JsonElement> ReadAccountExportAsync(string accountId, string email = "", CancellationToken cancellationToken = default);
        Task<string> ReadMailboxLineAsync(string accountId, string email = "", CancellationToken cancellationToken = default);
        Task<string> ReadPaymentUrlAsync(string accountId, string email = "", CancellationToken cancellationToken = default);
    }

    /// <summary>
    /// Desktop-read transport. Short reads go through a resident Python process
    /// (<c>--desktop-serve</c>, one JSONL request per line) so each call skips
    /// the ~0.6-1s interpreter/import cold start; any resident failure falls
    /// back to the previous one-shot <c>--desktop-read --desktop-ipc</c> path
    /// through the task coordinator, which long-running tasks still use.
    /// </summary>
    public sealed class DesktopReadClient : IDesktopReadClient, IDisposable
    {
        private static readonly string[] ReadAccountsArguments = ["--desktop-read", "accounts", "--desktop-ipc"];
        private readonly IBackendTaskCoordinator _backend;
        private readonly IApplicationPaths _paths;
        private readonly ISettingsService _settings;
        private readonly Serilog.ILogger _logger;
        private ResidentChannel _resident;

        public DesktopReadClient(
            IBackendTaskCoordinator backend,
            IApplicationPaths paths,
            ISettingsService settings,
            Serilog.ILogger logger)
        {
            _backend = backend;
            _paths = paths;
            _settings = settings;
            _logger = logger;
        }

        /// <summary>Coordinator-only construction: one-shot reads, no resident channel.</summary>
        public DesktopReadClient(IBackendTaskCoordinator backend)
            : this(backend, null!, null!, Serilog.Core.Logger.None)
        {
        }

        public async Task<JsonElement> ReadPoolsAsync(string selectedFile = "", CancellationToken cancellationToken = default)
        {
            try
            {
                return await ResidentRequestAsync(BuildResidentRequest("pools", "", "", selectedFile), cancellationToken)
                    .ConfigureAwait(false);
            }
            catch (ResidentChannelException)
            {
                // Fallback: run the two one-shot reads sequentially.
                JsonElement mailbox = await ReadMailboxPoolAsync(selectedFile, cancellationToken).ConfigureAwait(false);
                JsonElement accounts = await ReadAccountsAsync(cancellationToken).ConfigureAwait(false);
                using MemoryStream merged = new();
                using (Utf8JsonWriter writer = new(merged))
                {
                    writer.WriteStartObject();
                    foreach (JsonProperty property in accounts.EnumerateObject())
                        property.WriteTo(writer);
                    foreach (JsonProperty property in mailbox.EnumerateObject())
                        if (property.Name != "ok")
                            property.WriteTo(writer);
                    writer.WriteEndObject();
                }
                return JsonDocument.Parse(merged.ToArray()).RootElement.Clone();
            }
        }

        public async Task<JsonElement> ReadAccountsAsync(CancellationToken cancellationToken = default)
        {
            try
            {
                return await ResidentRequestAsync(BuildResidentRequest("accounts"), cancellationToken)
                    .ConfigureAwait(false);
            }
            catch (ResidentChannelException)
            {
                return await RunOneShotAsync("Read account index", ReadAccountsArguments, cancellationToken)
                    .ConfigureAwait(false);
            }
        }

        public async Task<JsonElement> ReadMailboxPoolAsync(string selectedFile = "", CancellationToken cancellationToken = default)
        {
            try
            {
                return await ResidentRequestAsync(BuildResidentRequest("mailbox-pool", "", "", selectedFile), cancellationToken)
                    .ConfigureAwait(false);
            }
            catch (ResidentChannelException)
            {
                var args = new List<string> { "--desktop-read", "mailbox-pool", "--desktop-ipc" };
                if (!string.IsNullOrWhiteSpace(selectedFile)) args.AddRange(["--chatai-mailbox-file", selectedFile]);
                return await RunOneShotAsync("Read mailbox pool", args, cancellationToken).ConfigureAwait(false);
            }
        }

        public async Task<JsonElement> ReadAccountAsync(string accountId, string email = "", CancellationToken cancellationToken = default)
        {
            try
            {
                return await ResidentRequestAsync(BuildResidentRequest("account", accountId, email), cancellationToken)
                    .ConfigureAwait(false);
            }
            catch (ResidentChannelException)
            {
                return await RunOneShotAsync(
                    "Read account detail", BuildOneShotArguments("account", accountId, email), cancellationToken)
                    .ConfigureAwait(false);
            }
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

        public void Dispose()
        {
            _resident?.Dispose();
            _resident = null;
        }

        private async Task<string> ReadTemporaryTextAsync(
            string commandName,
            string operation,
            string expectedPrefix,
            string accountId,
            string email,
            CancellationToken cancellationToken)
        {
            JsonElement payload;
            try
            {
                payload = await ResidentRequestAsync(
                    BuildResidentRequest(operation, accountId, email), cancellationToken).ConfigureAwait(false);
            }
            catch (ResidentChannelException)
            {
                payload = await RunOneShotAsync(
                    commandName, BuildOneShotArguments(operation, accountId, email), cancellationToken)
                    .ConfigureAwait(false);
            }
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

        private static Dictionary<string, object> BuildResidentRequest(
            string op, string accountId = "", string email = "", string selectedFile = "")
        {
            var request = new Dictionary<string, object> { ["op"] = op };
            if (!string.IsNullOrWhiteSpace(accountId)) request["account_id"] = accountId;
            if (!string.IsNullOrWhiteSpace(email)) request["email"] = email;
            if (!string.IsNullOrWhiteSpace(selectedFile)) request["extra_files"] = new[] { selectedFile };
            return request;
        }

        private static List<string> BuildOneShotArguments(string operation, string accountId, string email)
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

        private async Task<JsonElement> RunOneShotAsync(string name, IEnumerable<string> args, CancellationToken cancellationToken)
        {
            BackendCommandResult result = await _backend.RunAsync(
                BackendCommand.Create(name, args, 120000), cancellationToken: cancellationToken).ConfigureAwait(false);
            return ExtractPayload(result);
        }

        private static JsonElement ExtractPayload(BackendCommandResult result)
        {
            if (!result.Payload.HasValue) throw new InvalidOperationException("Desktop read backend returned no payload");
            JsonElement payload = result.Payload.Value;
            if (payload.TryGetProperty("ok", out JsonElement ok) && !ok.GetBoolean())
                throw new InvalidOperationException(
                    payload.TryGetProperty("error", out JsonElement error) ? error.GetString() : "Desktop read failed");
            return payload;
        }

        // ── Resident channel ─────────────────────────────────────────────

        private sealed class ResidentChannelException : Exception
        {
            public ResidentChannelException(string message) : base(message) { }
        }

        private async Task<JsonElement> ResidentRequestAsync(
            Dictionary<string, object> request, CancellationToken cancellationToken)
        {
            ResidentChannel channel = GetOrStartResident();
            if (channel == null)
                throw new ResidentChannelException("resident channel unavailable");
            JsonElement payload = await channel.RequestAsync(request, cancellationToken).ConfigureAwait(false);
            return ExtractPayloadFromResponse(payload);
        }

        private static JsonElement ExtractPayloadFromResponse(JsonElement response)
        {
            if (response.TryGetProperty("ok", out JsonElement ok) && ok.GetBoolean()
                && response.TryGetProperty("payload", out JsonElement payload))
            {
                return payload;
            }
            string error = response.TryGetProperty("error", out JsonElement errorElement)
                ? errorElement.GetString() ?? "resident request failed"
                : "resident request failed";
            throw new InvalidOperationException(error);
        }

        private ResidentChannel GetOrStartResident()
        {
            if (_paths == null || _settings == null)
                return null; // coordinator-only construction cannot host a resident process
            if (_resident != null && _resident.IsAlive)
                return _resident;
            _resident?.Dispose();
            try
            {
                _resident = ResidentChannel.Start(_paths, _settings, _logger);
                _logger.Information("Resident desktop-read channel started");
                return _resident;
            }
            catch (Exception ex)
            {
                _logger.Warning("Resident desktop-read channel unavailable: {Message}; falling back to one-shot reads", ex.Message);
                _resident = null;
                return null;
            }
        }

        private sealed class ResidentChannel : IDisposable
        {
            private readonly Process _process;
            private readonly object _gate = new();
            private readonly Dictionary<int, TaskCompletionSource<JsonElement>> _pending = new();
            private readonly Task _readLoop;
            private int _nextId;
            private bool _closed;

            private ResidentChannel(Process process, Serilog.ILogger logger)
            {
                _process = process;
                // stderr is redirected, so it has to be drained concurrently.
                // An unread pipe fills its ~4-8 KB buffer, after which the
                // Python side blocks on write and every request hangs until the
                // 120s timeout fires — the window then looks "empty" even
                // though the process is alive. Only stdout ending closes the
                // channel, so stderr does not participate in FailAllPending.
                Task stdoutLoop = Task.Run(() => ReadLoopAsync(logger));
                Task stderrLoop = Task.Run(() => DrainStandardErrorAsync(logger));
                _readLoop = Task.WhenAll(stdoutLoop, stderrLoop);
            }

            public bool IsAlive => !_closed && !_process.HasExited;

            public static ResidentChannel Start(IApplicationPaths paths, ISettingsService settings, Serilog.ILogger logger)
            {
                if (!File.Exists(paths.BackendScriptPath))
                    throw new FileNotFoundException("Backend script not found", paths.BackendScriptPath);
                var startInfo = new ProcessStartInfo
                {
                    FileName = settings.GetString("runtime.python_path", "python"),
                    WorkingDirectory = paths.RootDirectory,
                    UseShellExecute = false,
                    RedirectStandardInput = true,
                    RedirectStandardOutput = true,
                    RedirectStandardError = true,
                    CreateNoWindow = true,
                    StandardOutputEncoding = Encoding.UTF8,
                    StandardErrorEncoding = Encoding.UTF8,
                };
                startInfo.ArgumentList.Add(paths.BackendScriptPath);
                startInfo.ArgumentList.Add("--desktop-serve");
                Process process = Process.Start(startInfo)
                    ?? throw new InvalidOperationException("resident python process did not start");
                return new ResidentChannel(process, logger);
            }

            public async Task<JsonElement> RequestAsync(
                Dictionary<string, object> request, CancellationToken cancellationToken)
            {
                int id;
                TaskCompletionSource<JsonElement> completion = new(TaskCreationOptions.RunContinuationsAsynchronously);
                lock (_gate)
                {
                    if (_closed || _process.HasExited)
                        throw new ResidentChannelException("resident process is not running");
                    id = ++_nextId;
                    request["id"] = id;
                    _pending[id] = completion;
                }
                try
                {
                    string line = JsonSerializer.Serialize(request);
                    await _process.StandardInput.WriteLineAsync(line.AsMemory(), cancellationToken).ConfigureAwait(false);
                    await _process.StandardInput.FlushAsync(cancellationToken).ConfigureAwait(false);
                }
                catch (Exception)
                {
                    Complete(id, default);
                    throw new ResidentChannelException("failed to write to resident process");
                }

                using CancellationTokenSource timeout = new(TimeSpan.FromSeconds(120));
                using CancellationTokenSource linked = CancellationTokenSource.CreateLinkedTokenSource(
                    cancellationToken, timeout.Token);
                try
                {
                    return await completion.Task.WaitAsync(linked.Token).ConfigureAwait(false);
                }
                catch (OperationCanceledException) when (timeout.IsCancellationRequested)
                {
                    throw new ResidentChannelException("resident request timed out");
                }
                catch (OperationCanceledException)
                {
                    throw new ResidentChannelException("resident request cancelled");
                }
            }

            private async Task ReadLoopAsync(Serilog.ILogger logger)
            {
                try
                {
                    while (await _process.StandardOutput.ReadLineAsync().ConfigureAwait(false) is string line)
                    {
                        JsonElement response;
                        try
                        {
                            response = JsonDocument.Parse(line).RootElement;
                        }
                        catch (JsonException)
                        {
                            continue; // non-JSON noise must not wedge pending requests
                        }
                        if (response.TryGetProperty("id", out JsonElement idElement) && idElement.TryGetInt32(out int id))
                            Complete(id, response.Clone());
                    }
                }
                catch (Exception)
                {
                    // stdout closed: fall through to fail-all below
                }
                finally
                {
                    FailAllPending("resident process exited");
                    _closed = true;
                    logger.Information("Resident desktop-read channel closed");
                }
            }

            private async Task DrainStandardErrorAsync(Serilog.ILogger logger)
            {
                const int logBudget = 50;
                int emitted = 0;
                int suppressed = 0;
                try
                {
                    while (await _process.StandardError.ReadLineAsync().ConfigureAwait(false) is string line)
                    {
                        if (line.Length == 0)
                            continue;
                        if (emitted < logBudget)
                        {
                            emitted++;
                            logger.Warning(
                                "Resident backend stderr: {Line}",
                                SensitiveDataSanitizer.Redact(line));
                        }
                        else
                        {
                            // A chatty backend must not bury the log, but the
                            // pipe still has to keep being drained.
                            suppressed++;
                        }
                    }
                }
                catch (Exception)
                {
                    // stderr closed or the process was killed: nothing to drain.
                }
                if (suppressed > 0)
                    logger.Warning("Resident backend stderr: {Count} further lines suppressed", suppressed);
            }

            private void Complete(int id, JsonElement payload)
            {
                TaskCompletionSource<JsonElement> completion;
                lock (_gate)
                {
                    if (!_pending.Remove(id, out completion))
                        return;
                }
                if (payload.ValueKind != JsonValueKind.Undefined)
                    completion.TrySetResult(payload);
                else
                    completion.TrySetException(new ResidentChannelException("resident response missing payload"));
            }

            private void FailAllPending(string reason)
            {
                lock (_gate)
                {
                    foreach (TaskCompletionSource<JsonElement> completion in _pending.Values)
                        completion.TrySetException(new ResidentChannelException(reason));
                    _pending.Clear();
                }
            }

            public void Dispose()
            {
                _closed = true;
                FailAllPending("resident channel disposed");
                try
                {
                    if (!_process.HasExited)
                        _process.Kill(entireProcessTree: true);
                }
                catch
                {
                }
                _process.Dispose();
            }
        }
    }
}
