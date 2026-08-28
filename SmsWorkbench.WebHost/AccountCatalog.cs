using System.Text.Json;
using SmsWorkbench;

namespace SmsWorkbench.WebHost;

public sealed record AccountSummaryDto(
    string Id,
    string Email,
    bool Success,
    string Status,
    string RegisterMethod,
    string SessionType,
    string PlanType,
    string AccountType,
    string PromotionStatus,
    string RefreshTokenStatus,
    string AtProbeStatusCode,
    bool AccessTokenPresent,
    bool RefreshTokenPresent,
    bool TotpPresent,
    string WorkspaceStatus,
    string WorkspaceName,
    string RegistrationState,
    string RegistrationCountry,
    string MailboxProvider,
    string MailboxSource,
    string BatchId,
    string CreatedAt,
    string UpdatedAt);

public sealed record AccountPageDto(
    IReadOnlyList<AccountSummaryDto> Items,
    int Page,
    int PageSize,
    int Total);

public interface IAccountCatalog
{
    Task<IReadOnlyList<AccountSummaryDto>> ReadAllAsync(CancellationToken cancellationToken = default);
    Task<AccountSummaryDto?> ReadAsync(string id, CancellationToken cancellationToken = default);
}

public sealed class PythonAccountCatalog : IAccountCatalog
{
    private readonly IBackendClient _backend;

    public PythonAccountCatalog(IBackendClient backend) => _backend = backend;

    public async Task<IReadOnlyList<AccountSummaryDto>> ReadAllAsync(CancellationToken cancellationToken = default)
    {
        JsonElement payload = await RunReadAsync(
            "Read account index", ["--desktop-read", "accounts", "--desktop-ipc"], cancellationToken);
        if (!payload.TryGetProperty("accounts", out JsonElement accounts) || accounts.ValueKind != JsonValueKind.Array)
            return Array.Empty<AccountSummaryDto>();
        return accounts.EnumerateArray()
            .Where(item => item.ValueKind == JsonValueKind.Object)
            .Select(Map)
            .ToArray();
    }

    public async Task<AccountSummaryDto?> ReadAsync(string id, CancellationToken cancellationToken = default)
    {
        JsonElement payload = await RunReadAsync(
            "Read account detail",
            ["--desktop-read", "account", "--desktop-ipc", "--account-id", id],
            cancellationToken);
        if (!payload.TryGetProperty("account", out JsonElement account)
            || account.ValueKind != JsonValueKind.Object
            || !account.EnumerateObject().Any())
            return null;
        return Map(account);
    }

    private async Task<JsonElement> RunReadAsync(
        string name,
        IReadOnlyList<string> arguments,
        CancellationToken cancellationToken)
    {
        BackendCommandResult result = await _backend.RunAsync(
            BackendCommand.Create(name, arguments, 120000),
            cancellationToken: cancellationToken);
        if (result.ExitCode != 0 || !result.Payload.HasValue)
            throw new InvalidOperationException(
                result.TimedOut ? "Account read timed out." : First(result.StandardError, "Account read failed."));
        JsonElement payload = result.Payload.Value;
        if (payload.TryGetProperty("ok", out JsonElement ok) && ok.ValueKind == JsonValueKind.False)
            throw new InvalidOperationException(Text(payload, "error"));
        return payload;
    }

    public static AccountSummaryDto Map(JsonElement item) => new(
        Text(item, "id"),
        Text(item, "email"),
        Bool(item, "success"),
        Text(item, "status"),
        Text(item, "register_method"),
        Text(item, "session_type"),
        Text(item, "plan_type"),
        Text(item, "account_type"),
        Text(item, "promotion_status"),
        Text(item, "refresh_token_status"),
        Text(item, "at_probe_status_code"),
        Bool(item, "access_token_present"),
        Bool(item, "refresh_token_present"),
        Bool(item, "totp_present"),
        Text(item, "workspace_status"),
        Text(item, "workspace_name"),
        Text(item, "registration_state"),
        Text(item, "registration_country"),
        Text(item, "mailbox_provider"),
        Text(item, "mailbox_source"),
        Text(item, "batch_id"),
        Text(item, "created_at"),
        Text(item, "updated_at"));

    private static string Text(JsonElement element, string name)
        => element.TryGetProperty(name, out JsonElement value)
            ? value.ValueKind == JsonValueKind.String ? value.GetString() ?? "" : value.ToString()
            : "";

    private static bool Bool(JsonElement element, string name)
        => element.TryGetProperty(name, out JsonElement value)
            && (value.ValueKind == JsonValueKind.True
                || (value.ValueKind == JsonValueKind.String && bool.TryParse(value.GetString(), out bool parsed) && parsed));

    private static string First(params string[] values)
        => values.FirstOrDefault(value => !string.IsNullOrWhiteSpace(value)) ?? "";
}
