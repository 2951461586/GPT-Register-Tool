using System.Text.Json;
using SmsWorkbench;

namespace SmsWorkbench.WebHost;

public interface IBackendJobManager
{
    bool TryStart(string kind, BackendCommandPlan plan, out BackendJobDto job);
    BackendJobDto? Get(Guid id);
    IReadOnlyList<BackendJobDto> List();
    IReadOnlyList<BackendJobEventDto> EventsAfter(Guid id, long sequence);
    bool Cancel(Guid id);
}

public sealed class BackendJobManager : IBackendJobManager
{
    private const int MaxEventsPerJob = 2000;
    private readonly IBackendClient _backend;
    private readonly object _gate = new();
    private readonly Dictionary<Guid, JobEntry> _jobs = new();
    private Guid? _activeJobId;

    public BackendJobManager(IBackendClient backend) => _backend = backend;

    public bool TryStart(string kind, BackendCommandPlan plan, out BackendJobDto job)
    {
        JobEntry entry;
        lock (_gate)
        {
            if (_activeJobId.HasValue)
            {
                job = default!;
                return false;
            }
            entry = new JobEntry(kind, plan);
            _jobs[entry.Id] = entry;
            _activeJobId = entry.Id;
            entry.Append("state", new { state = "queued" });
            job = entry.Snapshot();
        }
        _ = Task.Run(() => RunAsync(entry));
        return true;
    }

    public BackendJobDto? Get(Guid id)
    {
        lock (_gate)
            return _jobs.TryGetValue(id, out JobEntry? entry) ? entry.Snapshot() : null;
    }

    public IReadOnlyList<BackendJobDto> List()
    {
        lock (_gate)
            return _jobs.Values
                .OrderByDescending(entry => entry.CreatedAt)
                .Take(50)
                .Select(entry => entry.Snapshot())
                .ToArray();
    }

    public IReadOnlyList<BackendJobEventDto> EventsAfter(Guid id, long sequence)
    {
        lock (_gate)
            return _jobs.TryGetValue(id, out JobEntry? entry)
                ? entry.Events.Where(item => item.Sequence > sequence).ToArray()
                : Array.Empty<BackendJobEventDto>();
    }

    public bool Cancel(Guid id)
    {
        lock (_gate)
        {
            if (!_jobs.TryGetValue(id, out JobEntry? entry) || entry.IsTerminal)
                return false;
            entry.Cancellation.Cancel();
            return true;
        }
    }

    private async Task RunAsync(JobEntry entry)
    {
        lock (_gate)
        {
            entry.State = BackendJobState.Running;
            entry.StartedAt = DateTimeOffset.UtcNow;
            entry.Append("state", new { state = "running" });
        }
        var progress = new InlineProgress<BackendOutputLine>(line =>
        {
            lock (_gate)
            {
                if (BackendProgressEventParser.TryParse(line.Text, out BackendProgressEvent value))
                    entry.Append("progress", value);
                else
                    entry.Append("log", new { channel = line.Channel.ToString(), text = WebPythonBackendClient.Redact(line.Text) });
            }
        });
        try
        {
            BackendCommand command = BackendCommand.Create(
                entry.Plan.TaskName,
                entry.Plan.Arguments,
                entry.Plan.TimeoutMilliseconds ?? 900000,
                MergeEnvironment(entry.Plan.Environment));
            BackendCommandResult result = await _backend.RunAsync(command, progress, entry.Cancellation.Token);
            lock (_gate)
            {
                entry.ExitCode = result.ExitCode;
                entry.TimedOut = result.TimedOut;
                entry.Result = result.Payload?.Clone();
                entry.Error = result.ExitCode == 0 && !result.TimedOut
                    ? ""
                    : WebPythonBackendClient.Redact(
                        result.TimedOut ? "Backend job timed out." : First(result.StandardError, result.StandardOutput));
                entry.State = result.ExitCode == 0 && !result.TimedOut
                    ? BackendJobState.Succeeded
                    : BackendJobState.Failed;
                entry.CompletedAt = DateTimeOffset.UtcNow;
                entry.Append("state", new { state = entry.State.ToString().ToLowerInvariant(), error = entry.Error });
            }
        }
        catch (OperationCanceledException)
        {
            lock (_gate)
            {
                entry.State = BackendJobState.Cancelled;
                entry.CompletedAt = DateTimeOffset.UtcNow;
                entry.Error = "Cancelled.";
                entry.Append("state", new { state = "cancelled" });
            }
        }
        catch (Exception exception)
        {
            lock (_gate)
            {
                entry.State = BackendJobState.Failed;
                entry.CompletedAt = DateTimeOffset.UtcNow;
                entry.Error = WebPythonBackendClient.Redact(exception.Message);
                entry.Append("state", new { state = "failed", error = entry.Error });
            }
        }
        finally
        {
            foreach (string path in entry.Plan.TempFiles)
            {
                try { File.Delete(path); } catch { }
            }
            lock (_gate)
            {
                if (_activeJobId == entry.Id)
                    _activeJobId = null;
            }
        }
    }

    private static IReadOnlyDictionary<string, string> MergeEnvironment(IReadOnlyDictionary<string, string> source)
    {
        var result = new Dictionary<string, string>(source, StringComparer.OrdinalIgnoreCase)
        {
            ["SMSWORKBENCH_EVENTS"] = "1",
        };
        return result;
    }

    private static string First(params string[] values)
        => values.FirstOrDefault(value => !string.IsNullOrWhiteSpace(value)) ?? "Backend job failed.";

    private sealed class JobEntry
    {
        private long _eventSequence;

        public JobEntry(string kind, BackendCommandPlan plan)
        {
            Kind = kind;
            Plan = plan;
        }

        public Guid Id { get; } = Guid.NewGuid();
        public string Kind { get; }
        public BackendCommandPlan Plan { get; }
        public BackendJobState State { get; set; } = BackendJobState.Queued;
        public DateTimeOffset CreatedAt { get; } = DateTimeOffset.UtcNow;
        public DateTimeOffset? StartedAt { get; set; }
        public DateTimeOffset? CompletedAt { get; set; }
        public int? ExitCode { get; set; }
        public bool TimedOut { get; set; }
        public JsonElement? Result { get; set; }
        public string Error { get; set; } = "";
        public CancellationTokenSource Cancellation { get; } = new();
        public List<BackendJobEventDto> Events { get; } = new();
        public bool IsTerminal => State is BackendJobState.Succeeded or BackendJobState.Failed or BackendJobState.Cancelled;

        public void Append(string type, object data)
        {
            Events.Add(new BackendJobEventDto(++_eventSequence, type, DateTimeOffset.UtcNow, data));
            if (Events.Count > MaxEventsPerJob)
                Events.RemoveRange(0, Events.Count - MaxEventsPerJob);
        }

        public BackendJobDto Snapshot() => new(
            Id, Kind, State, CreatedAt, StartedAt, CompletedAt, ExitCode, TimedOut, Result, Error);
    }

    private sealed class InlineProgress<T>(Action<T> report) : IProgress<T>
    {
        public void Report(T value) => report(value);
    }
}
