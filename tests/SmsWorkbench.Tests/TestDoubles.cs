using System.Text.Json.Nodes;

namespace SmsWorkbench.Tests;

internal static class ConfigTestHelpers
{
    /// <summary>
    /// Merge the proxy/runtime/payment shard files back into a single object,
    /// mirroring ConfigStore.ReadMerged, so assertions can inspect the persisted
    /// configuration exactly as the services persist it.
    /// </summary>
    internal static JsonObject ReadMergedConfig(string rootDirectory)
        => ConfigStore.ReadMerged(new TestApplicationPaths(rootDirectory)) ?? new JsonObject();
}

internal sealed class TestApplicationPaths : IApplicationPaths
{
    public TestApplicationPaths(string rootDirectory)
    {
        RootDirectory = rootDirectory;
        ConfigDirectory = rootDirectory;
        BackendScriptPath = Path.Combine(rootDirectory, "chatgpt_phone_reg.py");
    }

    public string RootDirectory { get; }

    public string ConfigDirectory { get; }

    public string BackendScriptPath { get; }
}

internal sealed class StubBackendClient : IBackendClient
{
    public Func<BackendCommand, BackendCommandResult> Handler { get; set; } = _ =>
        new BackendCommandResult(0, "", "", null, false);

    public Func<BackendCommand, CancellationToken, Task<BackendCommandResult>>? AsyncHandler { get; set; }

    public Action<IProgress<BackendOutputLine>?>? ReportProgress { get; set; }

    public BackendCommand? LastCommand { get; private set; }

    public List<BackendCommand> Commands { get; } = new();

    public async Task<BackendCommandResult> RunAsync(
        BackendCommand command,
        IProgress<BackendOutputLine>? progress = null,
        CancellationToken cancellationToken = default)
    {
        LastCommand = command;
        Commands.Add(command);
        cancellationToken.ThrowIfCancellationRequested();
        ReportProgress?.Invoke(progress);
        if (AsyncHandler != null)
            return await AsyncHandler(command, cancellationToken);
        return Handler(command);
    }
}

internal sealed class StubFileLauncher : IFileLauncher
{
    public string OpenedPath { get; private set; } = "";

    public bool Exists(string path) => !string.IsNullOrWhiteSpace(path);

    public void Open(string path) => OpenedPath = path;
}

/// <summary>
/// Update checker that never touches the network.
/// </summary>
/// <remarks>
/// <see cref="MainWindow"/> starts one check on load, so the smoke test needs a
/// stand-in that returns immediately: the real checker would open a socket to
/// GitHub from a unit test run and make the suite depend on the network.
/// </remarks>
internal sealed class StubUpdateChecker : IUpdateChecker
{
    public Func<CancellationToken, bool, Task<UpdateCheckResult>> Handler { get; set; } =
        (_, _) => Task.FromResult(new UpdateCheckResult { LatestVersion = AppVersion.Current });

    public int Calls { get; private set; }

    public bool LastForce { get; private set; }

    public Task<UpdateCheckResult> CheckAsync(CancellationToken cancellationToken, bool force = false)
    {
        Calls++;
        LastForce = force;
        cancellationToken.ThrowIfCancellationRequested();
        return Handler(cancellationToken, force);
    }
}

/// <summary>
/// Tray icon double. <see cref="IsAvailable"/> and <see cref="ExitRequested"/> are
/// settable so a test can walk the close-to-tray truth table without a shell.
/// </summary>
internal sealed class StubTrayIconService : ITrayIconService
{
    public bool IsAvailable { get; set; }

    public bool ExitRequested { get; set; }

    public int HiddenNotifications { get; private set; }

    public event EventHandler? ShowRequested;

    public event EventHandler? QuitRequested;

    public void Initialize()
    {
    }

    public void NotifyHiddenToTray() => HiddenNotifications++;

    public void RaiseShowRequested() => ShowRequested?.Invoke(this, EventArgs.Empty);

    public void RaiseQuitRequested()
    {
        ExitRequested = true;
        QuitRequested?.Invoke(this, EventArgs.Empty);
    }

    public void Dispose()
    {
    }
}

/// <summary>Global hotkey double: records what was asked for, never touches user32.</summary>
internal sealed class StubGlobalHotkeyService : IGlobalHotkeyService
{
    public bool IsRegistered { get; private set; }

    public (System.Windows.Input.ModifierKeys Modifiers, System.Windows.Input.Key Key)? Requested { get; private set; }

    public bool RegisterResult { get; set; }

    public bool Disposed { get; private set; }

    public event EventHandler? Pressed;

    public bool TryRegister(System.Windows.Window window, System.Windows.Input.ModifierKeys modifiers, System.Windows.Input.Key key)
    {
        Requested = (modifiers, key);
        IsRegistered = RegisterResult;
        return RegisterResult;
    }

    public void RaisePressed() => Pressed?.Invoke(this, EventArgs.Empty);

    public void Dispose() => Disposed = true;
}
