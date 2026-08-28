using System.Net;
using System.Security.Cryptography;
using Microsoft.AspNetCore.Http.Json;
using SmsWorkbench;
using SmsWorkbench.WebHost;

WebApplicationBuilder builder = WebApplication.CreateBuilder(args);
int port = int.TryParse(Environment.GetEnvironmentVariable("SMSWORKBENCH_WEB_PORT"), out int configuredPort)
    ? Math.Clamp(configuredPort, 1024, 65535)
    : 5137;
builder.WebHost.ConfigureKestrel(options => options.Listen(IPAddress.Loopback, port));
builder.Services.Configure<JsonOptions>(options =>
{
    options.SerializerOptions.PropertyNamingPolicy = System.Text.Json.JsonNamingPolicy.CamelCase;
    options.SerializerOptions.DictionaryKeyPolicy = System.Text.Json.JsonNamingPolicy.CamelCase;
});
builder.Services.AddSingleton(new RepositoryPaths(builder.Environment.ContentRootPath));
builder.Services.AddSingleton<ServerCommandDefaults>();
builder.Services.AddSingleton<IBackendClient, WebPythonBackendClient>();
builder.Services.AddSingleton<IAccountCatalog, PythonAccountCatalog>();
builder.Services.AddSingleton<BackendJobCommandFactory>();
builder.Services.AddSingleton<IBackendJobManager, BackendJobManager>();

WebApplication app = builder.Build();
string sessionToken = Convert.ToHexString(RandomNumberGenerator.GetBytes(32));
const string sessionCookie = "smsworkbench_session";

app.Use(async (context, next) =>
{
    string host = context.Request.Host.Host;
    if (!IPAddress.TryParse(host, out IPAddress? address) || !IPAddress.IsLoopback(address))
    {
        context.Response.StatusCode = StatusCodes.Status400BadRequest;
        return;
    }
    if (!context.Request.Path.StartsWithSegments("/api"))
    {
        context.Response.Cookies.Append(sessionCookie, sessionToken, new CookieOptions
        {
            HttpOnly = true,
            SameSite = SameSiteMode.Strict,
            Secure = false,
            IsEssential = true,
        });
        await next();
        return;
    }
    if (!context.Request.Cookies.TryGetValue(sessionCookie, out string? supplied)
        || !CryptographicOperations.FixedTimeEquals(
            System.Text.Encoding.UTF8.GetBytes(supplied),
            System.Text.Encoding.UTF8.GetBytes(sessionToken)))
    {
        context.Response.StatusCode = StatusCodes.Status401Unauthorized;
        return;
    }
    if (!HttpMethods.IsGet(context.Request.Method)
        && context.Request.Headers.Origin.Count > 0
        && !string.Equals(
            context.Request.Headers.Origin.ToString(),
            $"{context.Request.Scheme}://{context.Request.Host}",
            StringComparison.OrdinalIgnoreCase))
    {
        context.Response.StatusCode = StatusCodes.Status403Forbidden;
        return;
    }
    await next();
});

app.UseDefaultFiles();
app.UseStaticFiles();

app.MapGet("/api/meta", () => Results.Ok(new
{
    name = "GPT Register Tool Web Workbench",
    version = "1",
    paymentEnabled = false,
}));

app.MapGet("/api/accounts", async (
    string? q,
    string? status,
    string? planType,
    string? promotionStatus,
    int? page,
    int? pageSize,
    IAccountCatalog accounts,
    CancellationToken cancellationToken) =>
{
    IEnumerable<AccountSummaryDto> query = await accounts.ReadAllAsync(cancellationToken);
    if (!string.IsNullOrWhiteSpace(q))
        query = query.Where(item => item.Email.Contains(q.Trim(), StringComparison.OrdinalIgnoreCase)
            || item.Id.Contains(q.Trim(), StringComparison.OrdinalIgnoreCase));
    if (!string.IsNullOrWhiteSpace(status))
        query = query.Where(item => string.Equals(item.Status, status.Trim(), StringComparison.OrdinalIgnoreCase));
    if (!string.IsNullOrWhiteSpace(planType))
        query = query.Where(item => string.Equals(item.PlanType, planType.Trim(), StringComparison.OrdinalIgnoreCase));
    if (!string.IsNullOrWhiteSpace(promotionStatus))
        query = query.Where(item => item.PromotionStatus.Contains(
            promotionStatus.Trim(), StringComparison.OrdinalIgnoreCase));
    AccountSummaryDto[] filtered = query
        .OrderByDescending(item => item.UpdatedAt)
        .ThenBy(item => item.Email, StringComparer.OrdinalIgnoreCase)
        .ToArray();
    int resolvedPageSize = Math.Clamp(pageSize ?? 50, 10, 200);
    int resolvedPage = Math.Max(page ?? 1, 1);
    return Results.Ok(new AccountPageDto(
        filtered.Skip((resolvedPage - 1) * resolvedPageSize).Take(resolvedPageSize).ToArray(),
        resolvedPage,
        resolvedPageSize,
        filtered.Length));
});

app.MapGet("/api/accounts/stats", async (
    IAccountCatalog accounts,
    CancellationToken cancellationToken) =>
{
    IReadOnlyList<AccountSummaryDto> all = await accounts.ReadAllAsync(cancellationToken);
    int total = all.Count;
    int trial = all.Count(item => item.PromotionStatus.Contains("试用", StringComparison.OrdinalIgnoreCase)
        || item.PlanType.Contains("trial", StringComparison.OrdinalIgnoreCase));
    int registered = all.Count(item => item.Success || string.Equals(item.Status, "active", StringComparison.OrdinalIgnoreCase));
    int attention = all.Count(item =>
        !item.Success
        || string.Equals(item.Status, "failed", StringComparison.OrdinalIgnoreCase)
        || (!item.AccessTokenPresent && !string.IsNullOrEmpty(item.Email)));
    return Results.Ok(new { total, trial, registered, attention });
});

app.MapGet("/api/accounts/{id}", async (
    string id,
    IAccountCatalog accounts,
    CancellationToken cancellationToken) =>
{
    AccountSummaryDto? account = await accounts.ReadAsync(id, cancellationToken);
    return account is null ? Results.NotFound() : Results.Ok(account);
});

app.MapGet("/api/jobs", (IBackendJobManager jobs) => Results.Ok(jobs.List()));
app.MapGet("/api/jobs/{id:guid}", (Guid id, IBackendJobManager jobs) =>
    jobs.Get(id) is { } job ? Results.Ok(job) : Results.NotFound());

app.MapPost("/api/jobs/registrations", (
    RegistrationJobRequest request,
    BackendJobCommandFactory factory,
    IBackendJobManager jobs) =>
{
    try
    {
        BackendCommandPlan plan = factory.CreateRegistration(request);
        return jobs.TryStart("registration", plan, out BackendJobDto job)
            ? Results.Accepted($"/api/jobs/{job.Id}", job)
            : Results.Conflict(new { error = "A backend job is already running." });
    }
    catch (Exception exception) when (exception is ArgumentException or InvalidOperationException)
    {
        return Results.BadRequest(new { error = exception.Message });
    }
});

app.MapPost("/api/jobs/account-health", async (
    AccountHealthJobRequest request,
    BackendJobCommandFactory factory,
    IBackendJobManager jobs,
    CancellationToken cancellationToken) =>
{
    try
    {
        BackendCommandPlan plan = await factory.CreateHealthAsync(request, cancellationToken);
        return jobs.TryStart("account-health", plan, out BackendJobDto job)
            ? Results.Accepted($"/api/jobs/{job.Id}", job)
            : Results.Conflict(new { error = "A backend job is already running." });
    }
    catch (Exception exception) when (exception is ArgumentException or KeyNotFoundException)
    {
        return Results.BadRequest(new { error = exception.Message });
    }
});

app.MapPost("/api/jobs/account-promotions", async (
    PromotionJobRequest request,
    BackendJobCommandFactory factory,
    IBackendJobManager jobs,
    CancellationToken cancellationToken) =>
{
    try
    {
        BackendCommandPlan plan = await factory.CreatePromotionAsync(request, cancellationToken);
        return jobs.TryStart("account-promotions", plan, out BackendJobDto job)
            ? Results.Accepted($"/api/jobs/{job.Id}", job)
            : Results.Conflict(new { error = "A backend job is already running." });
    }
    catch (Exception exception) when (exception is ArgumentException or KeyNotFoundException)
    {
        return Results.BadRequest(new { error = exception.Message });
    }
});

app.MapPost("/api/jobs/accounts/{id}/quota-usage", async (
    string id,
    QuotaUsageJobRequest request,
    BackendJobCommandFactory factory,
    IBackendJobManager jobs,
    CancellationToken cancellationToken) =>
{
    try
    {
        BackendCommandPlan plan = await factory.CreateQuotaUsageAsync(id, request, cancellationToken);
        return jobs.TryStart("quota-usage", plan, out BackendJobDto job)
            ? Results.Accepted($"/api/jobs/{job.Id}", job)
            : Results.Conflict(new { error = "A backend job is already running." });
    }
    catch (KeyNotFoundException exception)
    {
        return Results.NotFound(new { error = exception.Message });
    }
});

app.MapPost("/api/jobs/{id:guid}/cancel", (Guid id, IBackendJobManager jobs) =>
    jobs.Cancel(id) ? Results.Accepted() : Results.NotFound());

app.MapGet("/api/jobs/{id:guid}/events", async (
    Guid id,
    HttpContext context,
    IBackendJobManager jobs,
    CancellationToken cancellationToken) =>
{
    if (jobs.Get(id) is null)
    {
        context.Response.StatusCode = StatusCodes.Status404NotFound;
        return;
    }
    context.Response.Headers.ContentType = "text/event-stream";
    context.Response.Headers.CacheControl = "no-cache";
    long sequence = long.TryParse(context.Request.Headers["Last-Event-ID"], out long parsed) ? parsed : 0;
    while (!cancellationToken.IsCancellationRequested)
    {
        IReadOnlyList<BackendJobEventDto> events = jobs.EventsAfter(id, sequence);
        foreach (BackendJobEventDto item in events)
        {
            sequence = item.Sequence;
            string json = System.Text.Json.JsonSerializer.Serialize(item);
            await context.Response.WriteAsync($"id: {item.Sequence}\nevent: {item.Type}\ndata: {json}\n\n", cancellationToken);
        }
        if (events.Count > 0)
            await context.Response.Body.FlushAsync(cancellationToken);
        BackendJobDto? job = jobs.Get(id);
        if (job is null || job.State is BackendJobState.Succeeded or BackendJobState.Failed or BackendJobState.Cancelled)
            break;
        await Task.Delay(350, cancellationToken);
    }
});

app.MapFallbackToFile("index.html");
app.Run();

public partial class Program;
