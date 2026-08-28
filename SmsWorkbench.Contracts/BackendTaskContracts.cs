namespace SmsWorkbench
{
    public sealed class BackendTaskAlreadyRunningException : InvalidOperationException
    {
        public BackendTaskAlreadyRunningException() : base("A backend task is already running.") { }
    }

    public interface IBackendTaskCoordinator
    {
        bool IsRunning { get; }
        Task<BackendCommandResult> RunAsync(
            BackendCommand command,
            IProgress<BackendOutputLine> progress = null,
            CancellationToken cancellationToken = default);
        Task<string> RunForResultAsync(BackendCommand command, CancellationToken cancellationToken = default);
        bool Cancel();
    }
}
