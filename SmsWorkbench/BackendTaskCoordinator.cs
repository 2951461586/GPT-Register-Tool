using System;
using System.Threading;
using System.Threading.Tasks;

namespace SmsWorkbench
{
    public sealed class BackendTaskCoordinator : IBackendTaskCoordinator, IDisposable
    {
        private readonly IBackendClient _client;
        private readonly object _gate = new();
        private CancellationTokenSource _activeCancellation;

        public BackendTaskCoordinator(IBackendClient client)
        {
            _client = client ?? throw new ArgumentNullException(nameof(client));
        }

        public bool IsRunning
        {
            get
            {
                lock (_gate)
                    return _activeCancellation != null;
            }
        }

        public async Task<BackendCommandResult> RunAsync(
            BackendCommand command,
            IProgress<BackendOutputLine> progress = null,
            CancellationToken cancellationToken = default)
        {
            CancellationTokenSource owned;
            lock (_gate)
            {
                if (_activeCancellation != null)
                    throw new BackendTaskAlreadyRunningException();
                owned = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
                _activeCancellation = owned;
            }

            try
            {
                return await _client.RunAsync(command, progress, owned.Token).ConfigureAwait(false);
            }
            finally
            {
                lock (_gate)
                {
                    if (ReferenceEquals(_activeCancellation, owned))
                        _activeCancellation = null;
                }
                owned.Dispose();
            }
        }

        public async Task<string> RunForResultAsync(
            BackendCommand command,
            CancellationToken cancellationToken = default)
        {
            BackendCommandResult result = await RunAsync(command, cancellationToken: cancellationToken).ConfigureAwait(false);
            if (result.Payload.HasValue)
                return result.Payload.Value.GetRawText();
            if (result.TimedOut)
                throw new TimeoutException($"Backend execution timed out ({command.Timeout.TotalSeconds:0}s)");
            if (!string.IsNullOrEmpty(result.StandardError))
                throw new InvalidOperationException(SensitiveDataSanitizer.Redact(result.StandardError));
            return SensitiveDataSanitizer.Redact(result.StandardOutput);
        }

        public bool Cancel()
        {
            lock (_gate)
            {
                if (_activeCancellation == null)
                    return false;
                _activeCancellation.Cancel();
                return true;
            }
        }

        public void Dispose()
        {
            lock (_gate)
            {
                _activeCancellation?.Cancel();
                _activeCancellation?.Dispose();
                _activeCancellation = null;
            }
        }
    }
}
