using System.Text.Json;

namespace SmsWorkbench.Tests;

/// <summary>
/// Proxy credentials must never reach the backend's command line. The planner keeps
/// emitting the documented flags (that is the CLI contract and the plan stays pure),
/// and the launcher moves those values into environment variables immediately before
/// the process starts, because on Windows every same-user process can read another
/// process's command line.
///
/// <para>
/// The payment lane is covered too: <c>--checkout-proxy-pool</c> /
/// <c>--approve-proxy-pool</c> carry whole pools of <c>scheme://user:pass@host:port</c>
/// entries. Leaving them on argv kept exactly the leak this transport closes, and a
/// mixed IPWO pool is large enough to approach the 32K Windows command-line limit.
/// </para>
/// </summary>
public sealed class ProxyArgumentTransportTests
{
    private const string SecretProxy = "http://user:s3cret@us.ipwo.net:7878";
    private const string SecretPool = SecretProxy + "\nhttp://other:pw@eu.ipwo.net:7878";

    [Fact]
    public void The_transported_names_match_the_shared_cross_language_fixture()
    {
        // sms_tool/cli.py reads the same fixture, so renaming one side without the
        // other fails here instead of silently dropping the operator's route.
        string path = Path.Combine(AppContext.BaseDirectory, "proxy_env_transport.json");
        using JsonDocument document = JsonDocument.Parse(File.ReadAllText(path));
        JsonElement root = document.RootElement;

        Assert.Equal(ProxyArgumentTransport.ProxyFlag, root.GetProperty("proxy_flag").GetString());
        Assert.Equal(ProxyArgumentTransport.ProxyPoolFlag, root.GetProperty("proxy_pool_flag").GetString());
        Assert.Equal(ProxyArgumentTransport.ProxyVariable, root.GetProperty("proxy_variable").GetString());
        Assert.Equal(ProxyArgumentTransport.ProxyPoolVariable, root.GetProperty("proxy_pool_variable").GetString());
        Assert.Equal(ProxyArgumentTransport.CheckoutPoolFlag, root.GetProperty("checkout_pool_flag").GetString());
        Assert.Equal(ProxyArgumentTransport.CheckoutPoolVariable, root.GetProperty("checkout_pool_variable").GetString());
        Assert.Equal(ProxyArgumentTransport.ApprovePoolFlag, root.GetProperty("approve_pool_flag").GetString());
        Assert.Equal(ProxyArgumentTransport.ApprovePoolVariable, root.GetProperty("approve_pool_variable").GetString());
    }

    [Fact]
    public void Every_route_is_a_flag_the_desktop_actually_plans()
    {
        // The list is the single place a new proxy flag must be registered. This
        // pins the four the planner/services emit today so a removed route is caught
        // by a test rather than by a credential appearing on a command line.
        Assert.Equal(
            new[] { "--proxy", "--proxy-pool", "--checkout-proxy-pool", "--approve-proxy-pool" },
            ProxyArgumentTransport.Routes.Keys.ToArray());
        Assert.Equal(4, ProxyArgumentTransport.Routes.Values.Distinct(StringComparer.Ordinal).Count());
    }

    [Fact]
    public void The_payment_pools_leave_argv_and_become_environment_values()
    {
        // Measured 2026-10-26: the payment batch built
        // "--checkout-proxy-pool <newline pool>" straight onto the command line, so
        // the whole IPWO pool (credentials included) was readable from Task Manager.
        var split = ProxyArgumentTransport.Extract(
            new[]
            {
                "--extract-payment-link",
                "--checkout-proxy-pool", SecretPool,
                "--approve-proxy-pool", SecretPool,
                "--workers", "4",
            });

        Assert.Equal(new[] { "--extract-payment-link", "--workers", "4" }, split.Arguments);
        Assert.DoesNotContain(SecretPool, split.Arguments);
        Assert.Equal(SecretPool, split.Environment[ProxyArgumentTransport.CheckoutPoolVariable]);
        Assert.Equal(SecretPool, split.Environment[ProxyArgumentTransport.ApprovePoolVariable]);
    }

    [Fact]
    public void The_scalar_proxy_leaves_argv_and_becomes_an_environment_value()
    {
        var split = ProxyArgumentTransport.Extract(new[] { "--desktop-ipc", "--proxy", SecretProxy, "--count", "3" });

        Assert.DoesNotContain(SecretProxy, split.Arguments);
        Assert.DoesNotContain("--proxy", split.Arguments);
        Assert.Equal(new[] { "--desktop-ipc", "--count", "3" }, split.Arguments);
        Assert.Equal(SecretProxy, split.Environment[ProxyArgumentTransport.ProxyVariable]);
    }

    [Fact]
    public void The_pool_keeps_its_line_breaks_in_the_environment_value()
    {
        string pool = SecretProxy + "\nhttp://other:pw@eu.ipwo.net:7878";
        var split = ProxyArgumentTransport.Extract(new[] { "--proxy-pool", pool, "--json" });

        Assert.DoesNotContain("--proxy-pool", split.Arguments);
        Assert.DoesNotContain(pool, split.Arguments);
        Assert.Equal(pool, split.Environment[ProxyArgumentTransport.ProxyPoolVariable]);
        Assert.Equal(new[] { "--json" }, split.Arguments);
    }

    [Fact]
    public void Both_routes_are_transported_together()
    {
        var split = ProxyArgumentTransport.Extract(
            new[] { "--proxy", SecretProxy, "--proxy-pool", "a\nb" });

        Assert.Empty(split.Arguments);
        Assert.Equal(SecretProxy, split.Environment[ProxyArgumentTransport.ProxyVariable]);
        Assert.Equal("a\nb", split.Environment[ProxyArgumentTransport.ProxyPoolVariable]);
    }

    [Fact]
    public void A_flag_without_a_value_is_dropped_instead_of_swallowing_its_neighbour()
    {
        // Forwarding a bare "--proxy" would make argparse consume "--count" as the
        // proxy value, which is worse than having no proxy at all.
        var split = ProxyArgumentTransport.Extract(new[] { "--proxy", "--count", "3" });

        // The flag is gone; the token that followed it is not swallowed.
        Assert.Equal(new[] { "--count", "3" }, split.Arguments);
        Assert.False(split.Environment.ContainsKey(ProxyArgumentTransport.ProxyVariable));
    }

    [Fact]
    public void A_repeated_flag_keeps_the_last_value_like_argparse()
    {
        var split = ProxyArgumentTransport.Extract(new[] { "--proxy", "http://a:1", "--proxy", "http://b:2" });

        Assert.Equal("http://b:2", split.Environment[ProxyArgumentTransport.ProxyVariable]);
    }

    [Fact]
    public void Existing_environment_entries_are_preserved()
    {
        var existing = new Dictionary<string, string> { ["SMS_TOOL_COMMAND_ID"] = "abc" };
        var split = ProxyArgumentTransport.Extract(new[] { "--proxy", SecretProxy }, existing);

        Assert.Equal("abc", split.Environment["SMS_TOOL_COMMAND_ID"]);
        Assert.Equal(SecretProxy, split.Environment[ProxyArgumentTransport.ProxyVariable]);
    }

    [Fact]
    public void A_registration_plan_no_longer_puts_its_pool_on_the_command_line()
    {
        // The regression test: this is the exact shape the desktop builds for a
        // one-click registration batch.
        BackendCommandPlan plan = BackendCommandPlanner.CreateMailboxFileRegistration(
            "一键注册",
            "--chatai-mailbox-file",
            "C:\\tmp\\mailbox.txt",
            count: 2,
            workers: 2,
            registrationAtOnly: true,
            proxyPool: new[] { SecretProxy, "http://other:pw@eu.ipwo.net:7878" });

        Assert.Contains("--proxy", plan.Arguments);
        Assert.Contains(SecretProxy, plan.Arguments);

        ProxyArgumentTransport.Split routed = ProxyArgumentTransport.Extract(plan.Arguments, plan.Environment);

        Assert.DoesNotContain("--proxy", routed.Arguments);
        Assert.DoesNotContain("--proxy-pool", routed.Arguments);
        Assert.DoesNotContain(routed.Arguments, value => value.Contains("s3cret", StringComparison.Ordinal));
        Assert.Equal(SecretProxy, routed.Environment[ProxyArgumentTransport.ProxyVariable]);
        Assert.Contains("eu.ipwo.net:7878", routed.Environment[ProxyArgumentTransport.ProxyPoolVariable]);
        // Everything that is not a proxy route survives untouched.
        Assert.Contains("--chatai-mailbox-file", routed.Arguments);
        Assert.Contains("--registration-at-only", routed.Arguments);
    }
}
