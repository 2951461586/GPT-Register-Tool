using CommunityToolkit.Mvvm.ComponentModel;
using System.Collections.ObjectModel;

namespace SmsWorkbench
{
    public sealed partial class StageMatrixCell : ObservableObject
    {
        public StageMatrixCell(string stage) => Stage = stage;

        public string Stage { get; }
        [ObservableProperty] private string status = "pending";
        [ObservableProperty] private string detail = "";
        [ObservableProperty] private string country = "";
        [ObservableProperty] private string attemptText = "";
    }

    public sealed partial class StageMatrixRun : ObservableObject
    {
        public StageMatrixRun(string key, string domain, string runId, string accountRef, string method, IEnumerable<string> stages)
        {
            Key = key;
            Domain = domain;
            RunId = runId;
            AccountRef = string.IsNullOrWhiteSpace(accountRef) ? "等待账号" : accountRef;
            Method = method;
            StartedAt = DateTimeOffset.Now;
            foreach (string stage in stages)
                Cells.Add(new StageMatrixCell(stage));
        }

        public string Key { get; }
        public string Domain { get; }
        public string RunId { get; }
        public DateTimeOffset StartedAt { get; }
        public ObservableCollection<StageMatrixCell> Cells { get; } = new();
        [ObservableProperty] private string accountRef;
        [ObservableProperty] private string method;
        [ObservableProperty] private string currentStage = "等待";
        [ObservableProperty] private string status = "running";
        [ObservableProperty] private string elapsed = "0s";

        public string DomainLabel => Domain == "registration" ? "注册" : "支付";
    }

    public sealed partial class StageMatrixViewModel : ObservableObject
    {
        private const int MaxRuns = 200;
        private readonly IStageMatrixStore _store;
        private readonly Dictionary<string, int> _lastSequences = new(StringComparer.Ordinal);
        private static readonly string[] RegistrationStages =
        {
            "started", "mailbox_ready", "sentinel", "identity_ready", "auth_flow",
            "user_register", "email_otp_send", "email_otp_wait", "email_otp_validate",
            "create_account", "auth_session", "access_token_probe", "totp_enroll", "finalize", "completed"
        };

        private static readonly string[] PaymentStages =
        {
            "routing", "auth_gate", "checkout", "promotion", "stripe_init", "payment_method",
            "confirm", "approve", "poll", "redirect", "provider", "artifact", "completed"
        };

        public ObservableCollection<StageMatrixRun> Runs { get; } = new();
        [ObservableProperty] private string summary = "等待后端阶段事件";

        public StageMatrixViewModel(IStageMatrixStore store = null)
        {
            _store = store;
            foreach (BackendProgressEvent progress in store?.Load() ?? Array.Empty<BackendProgressEvent>())
                ApplyCore(progress, persist: false);
        }

        public void Apply(BackendProgressEvent progress)
            => ApplyCore(progress, persist: true);

        private void ApplyCore(BackendProgressEvent progress, bool persist)
        {
            ArgumentNullException.ThrowIfNull(progress);
            string domain = string.IsNullOrWhiteSpace(progress.Domain) ? "backend" : progress.Domain;
            string key = progress.RunId.Length > 0
                ? $"{domain}:{progress.RunId}"
                : $"{domain}:{progress.AccountRef}";
            if (progress.Sequence > 0
                && _lastSequences.TryGetValue(key, out int lastSequence)
                && progress.Sequence <= lastSequence)
                return;
            if (progress.Sequence > 0)
                _lastSequences[key] = progress.Sequence;
            StageMatrixRun run = Runs.FirstOrDefault(item => item.Key == key);
            if (run == null)
            {
                run = new StageMatrixRun(
                    key,
                    domain,
                    progress.RunId,
                    progress.AccountRef,
                    progress.Method,
                    domain == "registration" ? RegistrationStages : PaymentStages);
                Runs.Add(run);
                while (Runs.Count > MaxRuns)
                    Runs.RemoveAt(0);
            }
            if (progress.AccountRef.Length > 0)
                run.AccountRef = progress.AccountRef;
            if (progress.Method.Length > 0)
                run.Method = progress.Method;

            StageMatrixCell cell = run.Cells.FirstOrDefault(item => item.Stage == progress.Stage);
            if (cell == null)
            {
                cell = new StageMatrixCell(progress.Stage);
                run.Cells.Add(cell);
            }
            cell.Status = NormalizeStatus(progress.Status);
            cell.Detail = progress.Detail;
            cell.Country = progress.Country;
            cell.AttemptText = progress.Attempt > 0
                ? progress.MaxAttempts > 0 ? $"{progress.Attempt}/{progress.MaxAttempts}" : progress.Attempt.ToString(CultureInfo.InvariantCulture)
                : "";
            run.CurrentStage = progress.Stage;
            bool terminalCompleted = progress.Stage == "completed"
                || (cell.Status == "completed" && progress.Stage is "finalize" or "artifact");
            run.Status = cell.Status is "failed" or "cancelled"
                ? cell.Status
                : terminalCompleted ? "completed" : "running";
            run.Elapsed = FormatElapsed(DateTimeOffset.Now - run.StartedAt);
            Summary = $"运行 {Runs.Count(item => item.Status == "running")}  完成 {Runs.Count(item => item.Status == "completed")}  失败 {Runs.Count(item => item.Status == "failed")}";
            if (persist)
                _store?.Append(progress);
        }

        public void Reset()
        {
            Runs.Clear();
            _lastSequences.Clear();
            Summary = "等待后端阶段事件";
        }

        public void ClearHistory()
        {
            Reset();
            _store?.Clear();
        }

        private static string NormalizeStatus(string value)
        {
            string status = (value ?? "").Trim().ToLowerInvariant();
            return status switch
            {
                "success" or "ok" or "complete" or "completed" => "completed",
                "fail" or "failed" or "error" => "failed",
                "cancel" or "cancelled" => "cancelled",
                _ => "running",
            };
        }

        private static string FormatElapsed(TimeSpan value)
            => value.TotalMinutes >= 1
                ? $"{(int)value.TotalMinutes}m {value.Seconds}s"
                : $"{Math.Max(0, (int)value.TotalSeconds)}s";
    }
}
