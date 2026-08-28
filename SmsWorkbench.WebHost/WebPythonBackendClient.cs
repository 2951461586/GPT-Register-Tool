using System.ComponentModel;
using System.Diagnostics;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using SmsWorkbench;

namespace SmsWorkbench.WebHost;

public sealed class WebPythonBackendClient : IBackendClient
{
    private const int MaxCapturedOutputChars = 2_000_000;
    private static readonly Regex SensitiveAssignment = new(
        @"(?i)\b(access_token|refresh_token|id_token|authorization|cookie|password|secret|api[_-]?key)\b(\s*[:=]\s*)([^\s,;]+)",
        RegexOptions.CultureInvariant | RegexOptions.Compiled);
    private readonly RepositoryPaths _paths;
    private readonly ServerCommandDefaults _defaults;
    private readonly ILogger<WebPythonBackendClient> _logger;

    public WebPythonBackendClient(
        RepositoryPaths paths,
        ServerCommandDefaults defaults,
        ILogger<WebPythonBackendClient> logger)
    {
        _paths = paths;
        _defaults = defaults;
        _logger = logger;
    }

    public async Task<BackendCommandResult> RunAsync(
        BackendCommand command,
        IProgress<BackendOutputLine>? progress = null,
        CancellationToken cancellationToken = default)
    {
        if (!File.Exists(_paths.BackendScriptPath))
            throw new FileNotFoundException("Backend script not found", _paths.BackendScriptPath);
        using var timeout = new CancellationTokenSource(command.Timeout);
        using var linked = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken, timeout.Token);
        using var process = new Process
        {
            StartInfo = CreateStartInfo(command),
            EnableRaisingEvents = true,
        };
        var stdout = new StringBuilder();
        var stderr = new StringBuilder();
        try
        {
            if (!process.Start())
                throw new InvalidOperationException("Python backend process did not start.");
        }
        catch (Win32Exception exception)
        {
            throw new InvalidOperationException("Unable to start the configured Python interpreter.", exception);
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
        string output = stdout.ToString().Trim();
        string error = Redact(stderr.ToString().Trim());
        int exitCode = process.HasExited ? process.ExitCode : -1;
        JsonElement? payload = BackendJsonProtocol.ExtractPayload(output);
        _logger.LogInformation(
            "Backend job {CommandName} exited with code {ExitCode}; payload={HasPayload}; timedOut={TimedOut}",
            command.Name, exitCode, payload.HasValue, timedOut);
        return new BackendCommandResult(exitCode, output, error, payload, timedOut);
    }

    private ProcessStartInfo CreateStartInfo(BackendCommand command)
    {
        var info = new ProcessStartInfo
        {
            FileName = _defaults.Load().PythonExecutable,
            WorkingDirectory = _paths.RootDirectory,
            UseShellExecute = false,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            CreateNoWindow = true,
            StandardOutputEncoding = Encoding.UTF8,
            StandardErrorEncoding = Encoding.UTF8,
        };
        info.ArgumentList.Add(_paths.BackendScriptPath);
        foreach (string argument in command.Arguments)
            info.ArgumentList.Add(argument ?? "");
        foreach ((string name, string value) in command.EnvironmentVariables)
            info.Environment[name] = value ?? "";
        return info;
    }

    private static async Task PumpAsync(
        StreamReader reader,
        StringBuilder capture,
        BackendOutputChannel channel,
        IProgress<BackendOutputLine>? progress)
    {
        while (await reader.ReadLineAsync().ConfigureAwait(false) is string line)
        {
            if (capture.Length < MaxCapturedOutputChars)
            {
                int remaining = MaxCapturedOutputChars - capture.Length;
                capture.AppendLine(line.Length <= remaining ? line : line[..remaining]);
            }
            string safe = line.StartsWith(BackendProgressEventParser.Prefix, StringComparison.Ordinal)
                ? line
                : Redact(line);
            progress?.Report(new BackendOutputLine(channel, safe));
        }
    }

    internal static string Redact(string value)
        => SensitiveAssignment.Replace(value ?? "", "$1$2[REDACTED]");

    private static void KillProcessTree(Process process)
    {
        try
        {
            if (!process.HasExited)
                process.Kill(entireProcessTree: true);
        }
        catch
        {
        }
    }
}
