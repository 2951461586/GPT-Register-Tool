// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch stays `annotations`.
#nullable enable

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
        private readonly SemaphoreSlim _residentGate = new(1, 1);
        private ResidentChannel? _resident;

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
            // `_residentGate` is deliberately not disposed: SemaphoreSlim only
            // allocates an OS handle if AvailableWaitHandle is touched, and
            // disposing it here would turn a request that is mid-flight during
            // shutdown into an ObjectDisposedException.
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

        /// <summary>
        /// A failure of the resident transport. Callers catch this to fall back
        /// to one-shot reads; a payload that the backend successfully produced
        /// but marked <c>ok:false</c> is <em>not</em> this exception, because
        /// re-running it through a cold start would fail identically.
        /// </summary>
        private sealed class ResidentChannelException : Exception
        {
            public DesktopReadErrorCode Code { get; }

            public ResidentChannelException(
                string message,
                DesktopReadErrorCode code = DesktopReadErrorCode.ChannelUnavailable)
                : base(message)
            {
                Code = code;
            }
        }

        /// <summary>
        /// Per-request ceiling on the resident channel. Overridable so the
        /// timeout paths can be exercised without a 120-second test.
        /// </summary>
        internal TimeSpan ResidentRequestTimeout { get; set; } = DesktopReadProtocol.RequestTimeout;

        /// <summary>
        /// Responses the resident channel is still waiting for. Diagnostics
        /// seam: a value that stays non-zero after requests have been given up
        /// on is the <c>_pending</c> leak.
        /// </summary>
        internal int PendingResidentRequests => _resident?.PendingCount ?? 0;

        private async Task<JsonElement> ResidentRequestAsync(
            Dictionary<string, object> request, CancellationToken cancellationToken)
        {
            ResidentChannel channel =
                await GetOrStartResidentAsync(cancellationToken).ConfigureAwait(false)
                ?? throw new ResidentChannelException(
                    "resident channel unavailable", DesktopReadErrorCode.ChannelUnavailable);
            JsonElement payload = await channel.RequestAsync(
                request, ResidentRequestTimeout, cancellationToken).ConfigureAwait(false);
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
            DesktopReadErrorCode code = response.TryGetProperty(DesktopReadProtocol.CodeField, out JsonElement codeElement)
                ? DesktopReadErrorCodes.Parse(codeElement.GetString())
                : DesktopReadErrorCode.None;

            // `watchdog_timeout` means the backend killed a handler that never
            // returned; the process is exiting, so the correct response is to
            // fall back and let the next call start a fresh one. Every other
            // code is a real answer from a healthy backend — re-running it
            // through a 0.6-1s cold start would produce the identical error.
            if (code == DesktopReadErrorCode.WatchdogTimeout)
                throw new ResidentChannelException(error, code);

            throw new InvalidOperationException(
                code == DesktopReadErrorCode.None ? error : $"[{DesktopReadErrorCodes.ToWire(code)}] {error}");
        }

        private async Task<ResidentChannel?> GetOrStartResidentAsync(CancellationToken cancellationToken)
        {
            if (_paths == null || _settings == null)
                return null; // coordinator-only construction cannot host a resident process

            // Serialized: starting a channel now awaits a handshake, which is a
            // far wider window than the old synchronous Process.Start, so two
            // concurrent first calls would otherwise each spawn a backend.
            await _residentGate.WaitAsync(cancellationToken).ConfigureAwait(false);
            try
            {
                if (_resident is { IsAlive: true } existing)
                {
                    // Alive is not the same as responsive. A handler blocked on
                    // IO keeps HasExited false, so without this probe every
                    // request after the wedge pays the full 120s timeout.
                    if (existing.ShouldProbeBeforeReuse())
                    {
                        try
                        {
                            await existing.PingAsync(cancellationToken).ConfigureAwait(false);
                            return existing;
                        }
                        catch (ResidentChannelException ex)
                        {
                            _logger.Warning(
                                "Resident desktop-read channel is alive but unresponsive ({Code}); restarting it",
                                DesktopReadErrorCodes.ToWire(ex.Code));
                        }
                    }
                    else
                    {
                        return existing;
                    }
                }

                _resident?.Dispose();
                _resident = await ResidentChannel.StartAsync(_paths, _settings, _logger, cancellationToken)
                    .ConfigureAwait(false);
                _logger.Information(
                    "Resident desktop-read channel started (protocol {Version})", DesktopReadProtocol.Version);
                return _resident;
            }
            catch (Exception ex)
            {
                _logger.Warning(
                    "Resident desktop-read channel unavailable: {Message}; falling back to one-shot reads", ex.Message);
                _resident?.Dispose();
                _resident = null;
                return null;
            }
            finally
            {
                _residentGate.Release();
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
            private long _lastResponseTicks = Environment.TickCount64;

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

            /// <summary>
            /// True when nothing has been in flight for a while, i.e. the next
            /// request would be the first to notice a wedge. Deliberately
            /// excludes the case where requests are outstanding: those are
            /// already being timed out by their own callers.
            /// </summary>
            public bool ShouldProbeBeforeReuse()
            {
                lock (_gate)
                {
                    if (_pending.Count > 0) return false;
                    return Environment.TickCount64 - Volatile.Read(ref _lastResponseTicks)
                        > (long)DesktopReadProtocol.HeartbeatIdleThreshold.TotalMilliseconds;
                }
            }

            public Task<JsonElement> PingAsync(CancellationToken cancellationToken) =>
                RequestAsync(
                    new Dictionary<string, object> { ["op"] = DesktopReadProtocol.OpPing },
                    DesktopReadProtocol.HeartbeatTimeout,
                    cancellationToken);

            /// <summary>Outstanding responses. Diagnostics seam for the leak test.</summary>
            internal int PendingCount
            {
                get
                {
                    lock (_gate)
                        return _pending.Count;
                }
            }

            /// <summary>
            /// Refuse a backend that does not speak this protocol version. The
            /// payloads are transport-compatible by design, but "compatible"
            /// is exactly how a stale backend silently serves half-understood
            /// data; the one-shot path is slower and always correct.
            /// </summary>
            private async Task HandshakeAsync(Serilog.ILogger logger, CancellationToken cancellationToken)
            {
                JsonElement response = await RequestAsync(
                    new Dictionary<string, object> { ["op"] = DesktopReadProtocol.OpHello },
                    DesktopReadProtocol.HandshakeTimeout,
                    cancellationToken).ConfigureAwait(false);

                JsonElement payload = response.TryGetProperty("payload", out JsonElement value)
                    ? value
                    : default;
                int version = payload.ValueKind == JsonValueKind.Object
                    && payload.TryGetProperty("protocol", out JsonElement reported)
                    && reported.TryGetInt32(out int parsed)
                        ? parsed
                        : 0;

                if (version == DesktopReadProtocol.Version) return;
                logger.Warning(
                    "Resident backend speaks desktop-read protocol {Backend} but this client speaks {Client}",
                    version, DesktopReadProtocol.Version);
                throw new ResidentChannelException(
                    $"desktop-read protocol mismatch: backend {version}, client {DesktopReadProtocol.Version}",
                    DesktopReadErrorCode.ProtocolMismatch);
            }

            public static async Task<ResidentChannel> StartAsync(
                IApplicationPaths paths, ISettingsService settings, Serilog.ILogger logger,
                CancellationToken cancellationToken = default)
            {
                if (!File.Exists(paths.BackendScriptPath))
                    throw new FileNotFoundException("Backend script not found", paths.BackendScriptPath);
                var startInfo = new ProcessStartInfo
                {
                    FileName = PythonPathResolver.Resolve(
                        paths,
                        settings.GetString("runtime.python_path", "python")),
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
                ResidentChannel channel = new(process, logger);
                try
                {
                    await channel.HandshakeAsync(logger, cancellationToken).ConfigureAwait(false);
                    return channel;
                }
                catch
                {
                    // Leave no half-started channel behind: the process keeps
                    // running and holding the backend script open otherwise.
                    channel.Dispose();
                    throw;
                }
            }

            public Task<JsonElement> RequestAsync(
                Dictionary<string, object> request, CancellationToken cancellationToken) =>
                RequestAsync(request, DesktopReadProtocol.RequestTimeout, cancellationToken);

            public async Task<JsonElement> RequestAsync(
                Dictionary<string, object> request, TimeSpan timeout, CancellationToken cancellationToken)
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
                    Abandon(id);
                    throw new ResidentChannelException("failed to write to resident process");
                }

                using CancellationTokenSource timeoutSource = new(timeout);
                using CancellationTokenSource linked = CancellationTokenSource.CreateLinkedTokenSource(
                    cancellationToken, timeoutSource.Token);
                try
                {
                    return await completion.Task.WaitAsync(linked.Token).ConfigureAwait(false);
                }
                catch (OperationCanceledException) when (timeoutSource.IsCancellationRequested)
                {
                    Abandon(id);
                    throw new ResidentChannelException(
                        $"resident request {id} timed out after {timeout.TotalSeconds:0}s",
                        DesktopReadErrorCode.Timeout);
                }
                catch (OperationCanceledException)
                {
                    Abandon(id);
                    throw new ResidentChannelException(
                        $"resident request {id} cancelled", DesktopReadErrorCode.Cancelled);
                }
            }

            /// <summary>
            /// Give up on a request and hand its slot back.
            /// </summary>
            /// <remarks>
            /// This is the leak fix. <see cref="_pending"/> is only ever
            /// drained by <see cref="Complete"/>, which runs when a response
            /// arrives — and the two paths that reach here are precisely the
            /// ones where no response is coming. Every timed-out or cancelled
            /// request therefore left its
            /// <see cref="TaskCompletionSource{TResult}"/> rooted in the
            /// dictionary (together with the caller's continuation chain) until
            /// the channel was disposed, which for a long-lived workbench is
            /// never. At one timeout per slow read this grew without bound.
            /// </remarks>
        private void Abandon(int id)
        {
            TaskCompletionSource<JsonElement>? completion;
                lock (_gate)
                {
                    if (!_pending.Remove(id, out completion))
                        return; // the response won the race; Complete already resolved it
                }
                // Nobody is awaiting this any more. Cancel rather than leave it
                // dangling: TrySetCanceled releases a late reader and, unlike
                // TrySetException, produces no unobserved-exception noise.
                completion.TrySetCanceled();
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
                            // Parse and immediately Clone() so the element is owned
                            // independently of the document, which we then Dispose.
                            // Leaving the document undisposed leaked its buffer.
                            using JsonDocument doc = JsonDocument.Parse(line);
                            response = doc.RootElement.Clone();
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
            TaskCompletionSource<JsonElement>? completion;
            lock (_gate)
            {
                if (!_pending.Remove(id, out completion))
                    return;
                // Any completed round trip is proof the process is
                    // responsive, which is what ShouldProbeBeforeReuse measures.
                    Volatile.Write(ref _lastResponseTicks, Environment.TickCount64);
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
