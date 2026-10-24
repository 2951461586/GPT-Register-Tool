// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch stays `annotations`.
#nullable enable

using Serilog;
using System.ComponentModel;
using System.Diagnostics;

namespace SmsWorkbench
{
    public sealed class PythonBackendClient : IBackendClient
    {
        private const int MaxCapturedOutputChars = BackendOutputCapture.MaxTailChars;
        private readonly IApplicationPaths _paths;
        private readonly ISettingsService _settings;
        private readonly Serilog.ILogger _logger;

        public PythonBackendClient(IApplicationPaths paths, ISettingsService settings, Serilog.ILogger logger)
        {
            _paths = paths;
            _settings = settings;
            _logger = logger;
        }

        /// <summary>Python interpreter used for backend commands (settings: runtime.python_path).</summary>
        public string PythonExecutable => PythonPathResolver.Resolve(
            _paths,
            _settings.GetString("runtime.python_path", "python"));

        public async Task<BackendCommandResult> RunAsync(
            BackendCommand command,
            IProgress<BackendOutputLine>? progress = null,
            CancellationToken cancellationToken = default)
        {
            if (!File.Exists(_paths.BackendScriptPath))
                throw new FileNotFoundException("Backend script not found", _paths.BackendScriptPath);

            using var timeout = new CancellationTokenSource(command.Timeout);
            using var linked = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken, timeout.Token);
            using var process = new Process { StartInfo = CreateStartInfo(command), EnableRaisingEvents = true };
            string commandId = command.EnvironmentVariables.TryGetValue("SMS_TOOL_COMMAND_ID", out string? configuredCommandId)
                && !string.IsNullOrWhiteSpace(configuredCommandId)
                ? configuredCommandId.Trim()
                : Guid.NewGuid().ToString("N");
            process.StartInfo.Environment["SMS_TOOL_COMMAND_ID"] = commandId;
            var stdout = new BackendOutputCapture();
            var stderr = new BackendOutputCapture();

            _logger.Information("Starting backend command {CommandName} via {Python}; command_id={CommandId}", command.Name, PythonExecutable, commandId);
            try
            {
                if (!process.Start())
                    throw new InvalidOperationException("Python backend process did not start.");
            }
            catch (Win32Exception ex)
            {
                throw new InvalidOperationException(
                    $"无法启动 Python 解释器 “{PythonExecutable}”: {ex.Message}。" +
                    "请安装 Python 3.10+ 并加入 PATH,或在 设置 → 数据与文件 → 运行环境 里配置解释器完整路径。", ex);
            }
            catch (System.IO.FileNotFoundException ex)
            {
                throw new InvalidOperationException(
                    $"找不到 Python 解释器 “{PythonExecutable}”。" +
                    "请安装 Python 3.10+ 并加入 PATH,或在 设置 → 数据与文件 → 运行环境 里配置解释器完整路径。", ex);
            }

            Task stdoutTask = PumpAsync(process.StandardOutput, stdout, BackendOutputChannel.StandardOutput, progress);
            Task stderrTask = PumpAsync(process.StandardError, stderr, BackendOutputChannel.StandardError, progress);
            bool timedOut = false;

            try
            {
                await process.WaitForExitAsync(linked.Token).ConfigureAwait(false);
            }
            catch (OperationCanceledException) when (timeout.IsCancellationRequested && !cancellationToken.IsCancellationRequested)
            {
                timedOut = true;
                KillProcessTree(process);
            }
            catch (OperationCanceledException)
            {
                KillProcessTree(process);
                throw;
            }

            await Task.WhenAll(stdoutTask, stderrTask).ConfigureAwait(false);
            int exitCode = process.HasExited ? process.ExitCode : -1;
            string output = stdout.Text;
            string error = SensitiveDataSanitizer.Redact(stderr.Text);
            // The structural capture wins: it is the only copy that survives a
            // large envelope (the diagnostic tail is capped at 256 K, and a
            // ReMail inbox / bulk account result easily exceeds that). Re-parsing
            // the tail and *overwriting* this value made every large IPC result
            // look like "exited 0 with no payload" (measured 2026-10-24 on
            // 查看收件箱 / 账号测活 / 账号优惠检测).
            JsonElement? payload = stdout.Payload;
            if (!payload.HasValue)
            {
                try
                {
                    payload = BackendJsonProtocol.ExtractPayload(
                        output,
                        message => _logger.Warning(message));
                    // Some Python dependencies write warnings to stderr.  A
                    // desktop IPC envelope must remain discoverable even when a
                    // warning was emitted on that channel, so retry extraction on
                    // the combined sanitized streams before declaring no payload.
                    if (!payload.HasValue && !string.IsNullOrWhiteSpace(error))
                        payload = BackendJsonProtocol.ExtractPayload(
                            output + Environment.NewLine + error,
                            message => _logger.Warning(message));
                }
                catch (JsonException exception)
                {
                    // A malformed backend envelope is a *response parse* failure, not
                    // a startup failure (startup failures are the process-did-not-start
                    // InvalidOperationExceptions above). Classify it distinctly so the
                    // caller surfaces "响应解析失败" instead of blaming the interpreter.
                    _logger.Warning(
                        exception,
                        "Backend response parse failed for {CommandName}; returning no payload instead of classifying as startup failure",
                        command.Name);
                    payload = null;
                }
            }
            _logger.Write(
                ExitLogLevel(exitCode, payload.HasValue, timedOut),
                "Backend command {CommandName} exited with code {ExitCode}; payload={HasPayload}; timedOut={TimedOut}; command_id={CommandId}",
                command.Name,
                exitCode,
                payload.HasValue,
                timedOut,
                commandId);
            return new BackendCommandResult(exitCode, output, error, payload, timedOut);
        }

        public static Serilog.Events.LogEventLevel ExitLogLevel(int exitCode, bool hasPayload, bool timedOut)
        {
            if (timedOut || exitCode < 0)
                return Serilog.Events.LogEventLevel.Error;
            if (exitCode != 0 || !hasPayload)
                return Serilog.Events.LogEventLevel.Warning;
            return Serilog.Events.LogEventLevel.Information;
        }

        private ProcessStartInfo CreateStartInfo(BackendCommand command)
        {
            var startInfo = new ProcessStartInfo
            {
                FileName = PythonExecutable,
                WorkingDirectory = _paths.RootDirectory,
                UseShellExecute = false,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                CreateNoWindow = true,
                StandardOutputEncoding = Encoding.UTF8,
                StandardErrorEncoding = Encoding.UTF8
            };
            startInfo.ArgumentList.Add(_paths.BackendScriptPath);
            foreach (string argument in command.Arguments)
                startInfo.ArgumentList.Add(argument ?? "");
            startInfo.Environment[ApplicationPaths.ConfigDirectoryEnvironmentVariable] = _paths.ConfigDirectory;
            foreach (KeyValuePair<string, string> variable in command.EnvironmentVariables)
                startInfo.Environment[variable.Key] = variable.Value ?? "";
            return startInfo;
        }

        private static async Task PumpAsync(
            StreamReader reader,
            BackendOutputCapture capture,
            BackendOutputChannel channel,
            IProgress<BackendOutputLine>? progress)
        {
            while (await reader.ReadLineAsync().ConfigureAwait(false) is string line)
            {
                // An IPC envelope is captured structurally the moment its line
                // arrives; keeping the raw 623 KB line as well is what made the
                // old capture cost up to 8 MB per command.
                if (!capture.TryCaptureEnvelope(line))
                    capture.Append(line);
                // Parseable desktop events are already sanitized by the Python
                // IPC boundary. Redacting the serialized envelope again would
                // replace fields such as payment_method with "[REDACTED]" and
                // make the live stage matrix lose its method identity.
                string displayLine = line.StartsWith(BackendProgressEventParser.Prefix, StringComparison.Ordinal)
                    ? line
                    : SensitiveDataSanitizer.Redact(line);
                progress?.Report(new BackendOutputLine(channel, displayLine));
            }
        }

        private void KillProcessTree(Process process)
        {
            try
            {
                if (!process.HasExited)
                {
                    process.Kill(entireProcessTree: true);
                    if (!process.WaitForExit(5000))
                        _logger.Warning("Backend process {ProcessId} did not exit after termination request", process.Id);
                }
            }
            catch (Exception exception)
            {
                _logger.Warning(exception, "Failed to terminate backend process tree");
            }
        }
    }
}
