using SmsWorkbench;

namespace SmsWorkbench.Tests;

internal sealed class TestApplicationPaths : IApplicationPaths
{
    public TestApplicationPaths(string rootDirectory)
    {
        RootDirectory = rootDirectory;
        BackendScriptPath = Path.Combine(rootDirectory, "chatgpt_phone_reg.py");
    }

    public string RootDirectory { get; }

    public string BackendScriptPath { get; }
}

internal sealed class StubBackendClient : IBackendClient
{
    public Func<BackendCommand, BackendCommandResult> Handler { get; set; } = _ =>
        new BackendCommandResult(0, "", "", null, false);

    public BackendCommand? LastCommand { get; private set; }

    public Task<BackendCommandResult> RunAsync(
        BackendCommand command,
        IProgress<BackendOutputLine>? progress = null,
        CancellationToken cancellationToken = default)
    {
        LastCommand = command;
        cancellationToken.ThrowIfCancellationRequested();
        return Task.FromResult(Handler(command));
    }
}

internal sealed class StubFileLauncher : IFileLauncher
{
    public string OpenedPath { get; private set; } = "";

    public bool Exists(string path) => !string.IsNullOrWhiteSpace(path);

    public void Open(string path) => OpenedPath = path;
}
