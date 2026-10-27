// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch stays `annotations`.
#nullable enable

using System.Text.Json;

using SmsWorkbench.Localization;

namespace SmsWorkbench
{
    /// <summary>
    /// Runs the three read-only proxy-pool commands and returns the backend's
    /// JSON payload. Argument vocabulary lives in <see cref="BackendCommandPlanner"/>
    /// so the CLI contract stays testable without a window.
    /// </summary>
    public interface IProxyPoolService
    {
        Task<JsonElement> ListAsync(string lane, CancellationToken cancellationToken);

        Task<JsonElement> TestAsync(ProxyPoolRequest request, CancellationToken cancellationToken);

        Task<JsonElement> ScreenAsync(ProxyPoolRequest request, CancellationToken cancellationToken);
    }

    public sealed class ProxyPoolService : IProxyPoolService
    {
        // A pool of 30 entries at 6 s per entry and 8 workers finishes well
        // inside this; the cap exists so a hung exit cannot freeze the window.
        private const int TimeoutMilliseconds = 180_000;

        private readonly IBackendTaskCoordinator _backendTasks;

        public ProxyPoolService(IBackendTaskCoordinator backendTasks)
        {
            _backendTasks = backendTasks;
        }

        public Task<JsonElement> ListAsync(string lane, CancellationToken cancellationToken)
            => RunAsync(BackendCommandPlanner.CreateProxyPoolList(lane), cancellationToken);

        public Task<JsonElement> TestAsync(ProxyPoolRequest request, CancellationToken cancellationToken)
            => RunAsync(
                BackendCommandPlanner.CreateProxyPoolTest(
                    request.PoolText,
                    request.Lane,
                    request.Country,
                    request.Workers,
                    request.TimeoutSeconds),
                cancellationToken);

        public Task<JsonElement> ScreenAsync(ProxyPoolRequest request, CancellationToken cancellationToken)
            => RunAsync(
                BackendCommandPlanner.CreateProxyPoolScreen(
                    request.PoolText,
                    request.Lane,
                    request.Country,
                    request.QualityMin,
                    request.Workers,
                    request.TimeoutSeconds),
                cancellationToken);

        private async Task<JsonElement> RunAsync(BackendCommandPlan plan, CancellationToken cancellationToken)
        {
            BackendCommandResult result = await _backendTasks.RunAsync(
                BackendCommand.Create(plan.TaskName, plan.Arguments, TimeoutMilliseconds),
                cancellationToken: cancellationToken).ConfigureAwait(false);

            if (result.TimedOut)
                throw new TimeoutException(Loc.T("代理池命令超时（180s）"));
            if (result.Payload.HasValue)
                return result.Payload.Value;
            if (!string.IsNullOrWhiteSpace(result.StandardError))
                throw new InvalidOperationException(result.StandardError);
            throw new InvalidOperationException(Loc.T("后端未返回代理池结果。"));
        }
    }
}
