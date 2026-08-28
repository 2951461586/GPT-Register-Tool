namespace SmsWorkbench.WebHost.Tests;

public sealed class RegistrationCommandFactoryTests
{
    private static BackendJobCommandFactory CreateFactory(
        string pythonPath = "python",
        string? cfworkerDomain = null,
        string? smailrDomain = null)
    {
        string temp = Path.Combine(Path.GetTempPath(), "grt_test_" + Guid.NewGuid().ToString("N")[..8]);
        Directory.CreateDirectory(temp);
        string configPath = Path.Combine(temp, "config.json");
        File.WriteAllText(configPath, $$"""
            {
              "runtime": { "python_path": "{{pythonPath}}" },
              "proxy": { "registration": "http://proxy:8080", "pool": [] },
              "email_registration": {
                "cfworker_domain": "{{cfworkerDomain ?? ""}}",
                "smailr": { "default_domain": "{{smailrDomain ?? "smailr.com"}}" }
              }
            }
            """);
        var paths = new RepositoryPaths(temp);
        var defaults = new ServerCommandDefaults(paths);
        var accounts = new StubAccountCatalog();
        return new BackendJobCommandFactory(defaults, accounts);
    }

    [Theory]
    [InlineData("pool", "--count", "5", "--no-phone-reuse")]
    [InlineData("phone", "--phone-register", "--count", "5")]
    [InlineData("remail", "--target-at200", "5", "--buy-remail-mailbox")]
    [InlineData("smailr", "--buy-smailr-mailbox", "--smailr-domain", "smailr.com")]
    public void RegistrationSourceMapsToExpectedCliFlags(string source, params string[] expectedFlags)
    {
        BackendJobCommandFactory factory = CreateFactory();
        var request = new RegistrationJobRequest(source, Count: 5, Workers: 2);
        BackendCommandPlan plan = factory.CreateRegistration(request);
        string args = string.Join(" ", plan.Arguments);
        foreach (string flag in expectedFlags)
            Assert.Contains(flag, args);
        Assert.Contains("--desktop-ipc", args);
    }

    [Fact]
    public void CfWorkerRegistrationRequiresConfiguredDomain()
    {
        BackendJobCommandFactory factory = CreateFactory(cfworkerDomain: null);
        Assert.Throws<InvalidOperationException>(() =>
            factory.CreateRegistration(new RegistrationJobRequest("cfworker", Count: 1)));
    }

    [Fact]
    public void CfWorkerRegistrationUsesConfiguredDomain()
    {
        BackendJobCommandFactory factory = CreateFactory(cfworkerDomain: "example.com");
        BackendCommandPlan plan = factory.CreateRegistration(
            new RegistrationJobRequest("cfworker", Count: 2, Workers: 2));
        Assert.Contains("--cfworker-domain", string.Join(" ", plan.Arguments));
        Assert.Contains("example.com", string.Join(" ", plan.Arguments));
    }

    [Fact]
    public void UnsupportedSourceThrows()
    {
        BackendJobCommandFactory factory = CreateFactory();
        Assert.Throws<ArgumentException>(() =>
            factory.CreateRegistration(new RegistrationJobRequest("unknown")));
    }

    [Fact]
    public void PoolRegistrationIncludesProxyFromConfig()
    {
        BackendJobCommandFactory factory = CreateFactory();
        BackendCommandPlan plan = factory.CreateRegistration(
            new RegistrationJobRequest("pool", Count: 1, Workers: 1));
        Assert.Contains("--proxy", string.Join(" ", plan.Arguments));
        Assert.Contains("http://proxy:8080", string.Join(" ", plan.Arguments));
    }

    [Fact]
    public void CountIsBoundedToMaximum()
    {
        BackendJobCommandFactory factory = CreateFactory();
        BackendCommandPlan plan = factory.CreateRegistration(
            new RegistrationJobRequest("pool", Count: 500, Workers: 1));
        string args = string.Join(" ", plan.Arguments);
        int countIndex = Array.IndexOf(plan.Arguments.ToArray(), "--count");
        Assert.Equal("100", plan.Arguments[countIndex + 1]);
    }

    private sealed class StubAccountCatalog : IAccountCatalog
    {
        public Task<IReadOnlyList<AccountSummaryDto>> ReadAllAsync(CancellationToken cancellationToken = default)
            => Task.FromResult<IReadOnlyList<AccountSummaryDto>>(Array.Empty<AccountSummaryDto>());

        public Task<AccountSummaryDto?> ReadAsync(string id, CancellationToken cancellationToken = default)
            => Task.FromResult<AccountSummaryDto?>(null);
    }
}
