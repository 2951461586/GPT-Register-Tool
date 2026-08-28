using System.Text.Json;

namespace SmsWorkbench.WebHost;

public enum BackendJobState
{
    Queued,
    Running,
    Succeeded,
    Failed,
    Cancelled,
}

public sealed record BackendJobDto(
    Guid Id,
    string Kind,
    BackendJobState State,
    DateTimeOffset CreatedAt,
    DateTimeOffset? StartedAt,
    DateTimeOffset? CompletedAt,
    int? ExitCode,
    bool TimedOut,
    JsonElement? Result,
    string Error);

public sealed record BackendJobEventDto(
    long Sequence,
    string Type,
    DateTimeOffset Timestamp,
    object Data);

public sealed record RegistrationJobRequest(
    string Source,
    int Count = 1,
    int Workers = 4,
    bool Disable2Fa = false,
    bool CheckPromotion = false);

public sealed record AccountHealthJobRequest(
    IReadOnlyList<string> AccountIds,
    int Workers = 4,
    bool AutoRelogin = false);

public sealed record PromotionJobRequest(
    IReadOnlyList<string> AccountIds,
    int Workers = 4);

public sealed record QuotaUsageJobRequest(int RefreshTimeoutSeconds = 60);
