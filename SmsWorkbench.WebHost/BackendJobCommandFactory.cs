using SmsWorkbench;

namespace SmsWorkbench.WebHost;

public sealed class BackendJobCommandFactory
{
    private readonly ServerCommandDefaults _defaults;
    private readonly IAccountCatalog _accounts;

    public BackendJobCommandFactory(ServerCommandDefaults defaults, IAccountCatalog accounts)
    {
        _defaults = defaults;
        _accounts = accounts;
    }

    public BackendCommandPlan CreateRegistration(RegistrationJobRequest request)
    {
        int count = Bound(request.Count, 1, 100);
        int workers = Bound(request.Workers, 1, 16);
        CommandDefaults defaults = _defaults.Load();
        BackendCommandPlan plan = (request.Source ?? "").Trim().ToLowerInvariant() switch
        {
            "pool" => BackendCommandPlanner.CreatePoolRegistration(count, defaults.ProxyPool, workers),
            "phone" => BackendCommandPlanner.CreatePhoneRegistration(
                count, defaults.ProxyPool, request.Disable2Fa, request.CheckPromotion),
            "cfworker" when defaults.CfWorkerDomain.Length > 0 => BackendCommandPlanner.CreateCfWorkerRegistration(
                defaults.CfWorkerDomain, count, workers, defaults.ProxyPool, request.Disable2Fa, request.CheckPromotion),
            "remail" => BackendCommandPlanner.CreateRemailTargetRegistration(
                count, workers, defaults.ProxyPool, request.Disable2Fa, request.CheckPromotion),
            "smailr" => BackendCommandPlanner.CreateSmailrRegistration(
                defaults.SmailrDomain, count, workers, defaults.ProxyPool, request.Disable2Fa, request.CheckPromotion),
            "cfworker" => throw new InvalidOperationException("CFWorker domain is not configured."),
            _ => throw new ArgumentException("Unsupported registration source.", nameof(request)),
        };
        return WithDesktopIpc(plan);
    }

    public async Task<BackendCommandPlan> CreateHealthAsync(
        AccountHealthJobRequest request,
        CancellationToken cancellationToken)
    {
        IReadOnlyList<string> emails = await ResolveEmailsAsync(request.AccountIds, cancellationToken);
        return WithDesktopIpc(BackendCommandPlanner.CreateAccountScan(
            emails,
            "",
            Bound(request.Workers, 1, 16),
            request.AutoRelogin,
            _defaults.Load().ProxyPool));
    }

    public async Task<BackendCommandPlan> CreatePromotionAsync(
        PromotionJobRequest request,
        CancellationToken cancellationToken)
    {
        IReadOnlyList<string> emails = await ResolveEmailsAsync(request.AccountIds, cancellationToken);
        return WithDesktopIpc(BackendCommandPlanner.CreatePromotionCheck(
            emails,
            Bound(request.Workers, 1, 16),
            _defaults.Load().ProxyPool));
    }

    public async Task<BackendCommandPlan> CreateQuotaUsageAsync(
        string accountId,
        QuotaUsageJobRequest request,
        CancellationToken cancellationToken)
    {
        AccountSummaryDto account = await _accounts.ReadAsync(accountId, cancellationToken)
            ?? throw new KeyNotFoundException("Account not found.");
        return WithDesktopIpc(BackendCommandPlanner.CreateQuotaUsageProbe(
            account.Email,
            Bound(request.RefreshTimeoutSeconds, 5, 300),
            _defaults.Load().ProxyPool));
    }

    private async Task<IReadOnlyList<string>> ResolveEmailsAsync(
        IReadOnlyList<string>? ids,
        CancellationToken cancellationToken)
    {
        var requested = new HashSet<string>(
            (ids ?? Array.Empty<string>()).Where(value => !string.IsNullOrWhiteSpace(value)),
            StringComparer.OrdinalIgnoreCase);
        if (requested.Count == 0)
            throw new ArgumentException("At least one account is required.", nameof(ids));
        IReadOnlyList<AccountSummaryDto> accounts = await _accounts.ReadAllAsync(cancellationToken);
        string[] emails = accounts
            .Where(account => requested.Contains(account.Id))
            .Select(account => account.Email)
            .Where(email => email.Length > 0)
            .Distinct(StringComparer.OrdinalIgnoreCase)
            .ToArray();
        if (emails.Length != requested.Count)
            throw new KeyNotFoundException("One or more accounts were not found.");
        return emails;
    }

    private static int Bound(int value, int minimum, int maximum)
        => Math.Clamp(value, minimum, maximum);

    private static BackendCommandPlan WithDesktopIpc(BackendCommandPlan plan)
        => plan with { Arguments = plan.Arguments.Concat(["--desktop-ipc"]).ToArray() };
}
