using SmsWorkbench;

namespace SmsWorkbench.Tests;

public sealed class BackendCommandPlannerTests
{
    // ── Registration ────────────────────────────────────────────────────

    [Fact]
    public void CreatePoolRegistration_IncludesCountWorkersAndProxy()
    {
        var plan = BackendCommandPlanner.CreatePoolRegistration(
            count: 5,
            proxyPool: new[] { "http://proxy1:8080", "http://proxy2:8080" },
            workers: 3);

        Assert.Equal("邮箱池注册", plan.TaskName);
        Assert.Contains("--count", plan.Arguments);
        Assert.Contains("5", plan.Arguments);
        Assert.Contains("--workers", plan.Arguments);
        Assert.Contains("3", plan.Arguments);
        Assert.Contains("--no-phone-reuse", plan.Arguments);
        Assert.Contains("--proxy", plan.Arguments);
        Assert.Contains("http://proxy1:8080", plan.Arguments);
    }

    [Fact]
    public void CreatePoolRegistration_WithoutProxyPool_OmitsProxyArgs()
    {
        var plan = BackendCommandPlanner.CreatePoolRegistration(count: 1, proxyPool: Array.Empty<string>());
        Assert.DoesNotContain("--proxy", plan.Arguments);
        Assert.DoesNotContain("--proxy-pool", plan.Arguments);
    }

    [Fact]
    public void CreateMailboxFileRegistration_IncludesRegistrationAtOnly()
    {
        var plan = BackendCommandPlanner.CreateMailboxFileRegistration(
            "测试注册",
            "--mailbox-file",
            "C:\\test.txt",
            count: 3,
            workers: 4,
            registrationAtOnly: true,
            proxyPool: new[] { "http://proxy:8080" });

        Assert.Equal("测试注册", plan.TaskName);
        Assert.Contains("--registration-at-only", plan.Arguments);
        Assert.Contains("--no-phone-reuse", plan.Arguments);
        Assert.Contains("--mailbox-file", plan.Arguments);
        Assert.Contains("C:\\test.txt", plan.Arguments);
    }

    [Fact]
    public void CreateMailboxFileRegistration_WithoutRegistrationAtOnly_OmitsFlag()
    {
        var plan = BackendCommandPlanner.CreateMailboxFileRegistration(
            "测试", "--mailbox-file", "C:\\test.txt", 1, 2,
            registrationAtOnly: false, proxyPool: Array.Empty<string>());

        Assert.DoesNotContain("--registration-at-only", plan.Arguments);
        Assert.Contains("--no-phone-reuse", plan.Arguments);
    }

    [Fact]
    public void CreateMailboxFileRegistration_WithDisable2fa_AddsNo2faFlag()
    {
        var plan = BackendCommandPlanner.CreateMailboxFileRegistration(
            "测试", "--chatai-mailbox-file", "C:\\test.txt", 5, 5,
            registrationAtOnly: true, proxyPool: Array.Empty<string>(), disable2fa: true);

        Assert.Contains("--no-2fa", plan.Arguments);
    }

    [Fact]
    public void CreateMailboxFileRegistration_DefaultOmitsNo2faFlag()
    {
        var plan = BackendCommandPlanner.CreateMailboxFileRegistration(
            "测试", "--chatai-mailbox-file", "C:\\test.txt", 5, 5,
            registrationAtOnly: true, proxyPool: Array.Empty<string>());

        Assert.DoesNotContain("--no-2fa", plan.Arguments);
    }

    [Fact]
    public void CreateMailboxFileRegistration_WithPromotionCheck_AddsPostRegistrationFlag()
    {
        var plan = BackendCommandPlanner.CreateMailboxFileRegistration(
            "测试", "--mailbox-file", "C:\\test.txt", 2, 2,
            registrationAtOnly: true,
            proxyPool: Array.Empty<string>(),
            checkPromotion: true);

        Assert.Contains("--check-promotion-after-registration", plan.Arguments);
    }

    [Fact]
    public void CreateRemailTargetRegistration_WithDisable2fa_AddsNo2faFlag()
    {
        var plan = BackendCommandPlanner.CreateRemailTargetRegistration(
            count: 5, workers: 5, proxyPool: Array.Empty<string>(), disable2fa: true);

        Assert.Contains("--no-2fa", plan.Arguments);
    }

    [Fact]
    public void CreateRemailTargetRegistration_WithPromotionCheck_AddsPostRegistrationFlag()
    {
        var plan = BackendCommandPlanner.CreateRemailTargetRegistration(
            count: 5,
            workers: 5,
            proxyPool: Array.Empty<string>(),
            checkPromotion: true);

        Assert.Contains("--check-promotion-after-registration", plan.Arguments);
    }

    [Fact]
    public void CreatePhoneRegistration_IncludesPhoneRegisterFlag()
    {
        var plan = BackendCommandPlanner.CreatePhoneRegistration(
            count: 2,
            proxyPool: new[] { "http://proxy:8080" });

        Assert.Equal("手机号注册 (SMSBower)", plan.TaskName);
        Assert.Contains("--phone-register", plan.Arguments);
        Assert.Contains("--count", plan.Arguments);
        Assert.Contains("2", plan.Arguments);
    }

    [Fact]
    public void CreateCfWorkerRegistration_IncludesDomainAndRegistrationAtOnly()
    {
        var plan = BackendCommandPlanner.CreateCfWorkerRegistration(
            domain: "example.cloud",
            count: 5,
            workers: 3,
            proxyPool: Array.Empty<string>());

        Assert.Equal("CFWorker邮箱注册", plan.TaskName);
        Assert.Contains("--buy-cfworker-mailbox", plan.Arguments);
        Assert.Contains("--cfworker-domain", plan.Arguments);
        Assert.Contains("example.cloud", plan.Arguments);
        Assert.Contains("--registration-at-only", plan.Arguments);
    }

    [Fact]
    public void CreateRemailTargetRegistration_IncludesTargetAt200()
    {
        var plan = BackendCommandPlanner.CreateRemailTargetRegistration(
            count: 10, workers: 2, proxyPool: Array.Empty<string>());

        Assert.Contains("--target-at200", plan.Arguments);
        Assert.Contains("10", plan.Arguments);
        Assert.Contains("--buy-remail-mailbox", plan.Arguments);
        Assert.Contains("--remail-service-mode", plan.Arguments);
        Assert.Contains("purchase", plan.Arguments);
    }

    [Fact]
    public void CreateSmailrRegistration_IncludesDomain()
    {
        var plan = BackendCommandPlanner.CreateSmailrRegistration(
            domain: "smailr.com",
            count: 3,
            workers: 2,
            proxyPool: Array.Empty<string>());

        Assert.Contains("--buy-smailr-mailbox", plan.Arguments);
        Assert.Contains("--smailr-domain", plan.Arguments);
        Assert.Contains("smailr.com", plan.Arguments);
    }

    [Fact]
    public void CreateRerunFailedRegistration_DelegatesToMailboxFileRegistration()
    {
        var plan = BackendCommandPlanner.CreateRerunFailedRegistration(
            mailboxArgument: "--chatai-mailbox-file",
            mailboxFile: "C:\\failed.txt",
            count: 5,
            proxyPool: Array.Empty<string>());

        Assert.Contains("重新注册失败账号", plan.TaskName);
        Assert.Contains("5", plan.TaskName);
        Assert.Contains("--chatai-mailbox-file", plan.Arguments);
    }

    // ── One-click SMS ───────────────────────────────────────────────────

    [Fact]
    public void CreateOneClickSms_SingleEmail_UsesInlineArgs()
    {
        var plan = BackendCommandPlanner.CreateOneClickSms(
            mailboxArgument: "--mailbox-file",
            mailboxFile: "C:\\mbox.txt",
            emails: new[] { "user@example.com" },
            sessionFile: "C:\\session.json",
            proxyPool: Array.Empty<string>());

        Assert.Contains("--email", plan.Arguments);
        Assert.Contains("user@example.com", plan.Arguments);
        Assert.Contains("--session-file", plan.Arguments);
        Assert.DoesNotContain("--email-file", plan.Arguments);
        Assert.Empty(plan.TempFiles);
    }

    [Fact]
    public void CreateOneClickSms_MultipleEmails_WritesTempFile()
    {
        var plan = BackendCommandPlanner.CreateOneClickSms(
            mailboxArgument: "--mailbox-file",
            mailboxFile: "C:\\mbox.txt",
            emails: new[] { "a@b.com", "c@d.com" },
            sessionFile: "",
            proxyPool: Array.Empty<string>());

        Assert.Contains("--email-file", plan.Arguments);
        Assert.DoesNotContain("--email", plan.Arguments);
        Assert.Single(plan.TempFiles);
    }

    // ── Account liveness ────────────────────────────────────────────────

    [Fact]
    public void CreateAccountScan_MultipleEmails_UsesEmailFile()
    {
        var plan = BackendCommandPlanner.CreateAccountScan(
            emails: new[] { "a@b.com", "c@d.com" },
            sessionFile: "",
            workers: 4,
            autoRelogin: false,
            proxyPool: Array.Empty<string>());

        Assert.Contains("--email-file", plan.Arguments);
        Assert.DoesNotContain("--email", plan.Arguments);
        Assert.Single(plan.TempFiles);
    }

    [Fact]
    public void CreateAccountScan_SingleEmail_UsesInlineEmail()
    {
        var plan = BackendCommandPlanner.CreateAccountScan(
            emails: new[] { "user@example.com" },
            sessionFile: "C:\\session.json",
            workers: 4,
            autoRelogin: true,
            proxyPool: Array.Empty<string>());

        Assert.Contains("--email", plan.Arguments);
        Assert.Contains("user@example.com", plan.Arguments);
        Assert.Contains("--quota-auto-relogin", plan.Arguments);
        Assert.Contains("--quota-relogin-timeout", plan.Arguments);
        Assert.Contains("300", plan.Arguments);
    }

    [Fact]
    public void CreateAccountScan_AutoReloginDisabled_OmitsReloginArgs()
    {
        var plan = BackendCommandPlanner.CreateAccountScan(
            emails: new[] { "user@example.com" },
            sessionFile: "C:\\session.json",
            workers: 4,
            autoRelogin: false,
            proxyPool: Array.Empty<string>());

        Assert.DoesNotContain("--quota-auto-relogin", plan.Arguments);
        Assert.DoesNotContain("--quota-relogin-timeout", plan.Arguments);
    }

    [Fact]
    public void CreateQuotaUsageProbe_IncludesEmail()
    {
        var plan = BackendCommandPlanner.CreateQuotaUsageProbe(
            email: "user@example.com",
            refreshTimeoutSeconds: 120,
            proxyPool: Array.Empty<string>());

        Assert.Contains("--quota-usage", plan.Arguments);
        Assert.Contains("--email", plan.Arguments);
        Assert.Contains("user@example.com", plan.Arguments);
        Assert.Contains("--refresh-timeout", plan.Arguments);
        Assert.Contains("120", plan.Arguments);
    }

    // ── Deletion ────────────────────────────────────────────────────────

    [Fact]
    public void CreateDeleteAccount_IncludesDesktopIpc()
    {
        var plan = BackendCommandPlanner.CreateDeleteAccount("user@example.com");
        Assert.Contains("--delete-account", plan.Arguments);
        Assert.Contains("--email", plan.Arguments);
        Assert.Contains("user@example.com", plan.Arguments);
        Assert.Contains("--desktop-ipc", plan.Arguments);
        Assert.Equal(120000, plan.TimeoutMilliseconds);
    }

    [Fact]
    public void CreateBatchDeleteAccounts_WritesTempFile()
    {
        var plan = BackendCommandPlanner.CreateBatchDeleteAccounts(
            new[] { "a@b.com", "c@d.com" }, workers: 6);
        Assert.Contains("--email-file", plan.Arguments);
        Assert.Contains("--workers", plan.Arguments);
        Assert.Contains("6", plan.Arguments);
        Assert.Single(plan.TempFiles);
        Assert.Equal(120000, plan.TimeoutMilliseconds);
    }

    // ── Import ──────────────────────────────────────────────────────────

    [Fact]
    public void CreateAccountImport_NormalizesTarget()
    {
        var plan = BackendCommandPlanner.CreateAccountImport(
            "SUB2API",
            new[] { "user@example.com" });
        Assert.Contains("--import-target", plan.Arguments);
        Assert.Contains("sub2api", plan.Arguments);
    }

    [Fact]
    public void CreateAccountImport_DefaultsToCpa()
    {
        var plan = BackendCommandPlanner.CreateAccountImport(
            "unknown",
            new[] { "user@example.com" });
        Assert.Contains("cpa", plan.Arguments);
    }

    [Fact]
    public void CreateSingleAccountImport_WritesTempFile()
    {
        var plan = BackendCommandPlanner.CreateSingleAccountImport(
            "cpa",
            "user@example.com");
        Assert.Contains("--email-file", plan.Arguments);
        Assert.Single(plan.TempFiles);
    }

    // ── Export conversion ───────────────────────────────────────────────

    [Fact]
    public void CreateSessionConversion_IncludesAllArgs()
    {
        var plan = BackendCommandPlanner.CreateSessionConversion(
            "C:\\source.json",
            "sub2api",
            "C:\\output.json");

        Assert.Contains("--convert-session-json", plan.Arguments);
        Assert.Contains("C:\\source.json", plan.Arguments);
        Assert.Contains("--convert-format", plan.Arguments);
        Assert.Contains("sub2api", plan.Arguments);
        Assert.Contains("--convert-output", plan.Arguments);
        Assert.Contains("C:\\output.json", plan.Arguments);
    }

    // ── Refresh / maintenance ───────────────────────────────────────────

    [Fact]
    public void CreateRefreshSession_IncludesEmailAndSessionFile()
    {
        var plan = BackendCommandPlanner.CreateRefreshSession(
            "user@example.com",
            "C:\\session.json");
        Assert.Contains("--refresh-session", plan.Arguments);
        Assert.Contains("--session-file", plan.Arguments);
    }

    [Fact]
    public void CreateRebuildSqlite_HasMinimalArgs()
    {
        var plan = BackendCommandPlanner.CreateRebuildSqlite();
        Assert.Equal("重建SQLite索引", plan.TaskName);
        Assert.Contains("--rebuild-sqlite", plan.Arguments);
        Assert.Single(plan.Arguments);
    }

    // ── Inbox ───────────────────────────────────────────────────────────

    [Fact]
    public void CreateViewInbox_IncludesEmailAndLimit()
    {
        var plan = BackendCommandPlanner.CreateViewInbox(
            email: "user@example.com",
            limit: 10,
            mailboxArgument: "--mailbox-file",
            mailboxLine: "user@example.com----password----clientId----refreshToken",
            sessionFile: "C:\\session.json",
            mailboxProxy: "http://proxy:8080",
            remailToken: "abc123",
            tempDirectory: null!);

        Assert.Contains("--view-inbox", plan.Arguments);
        Assert.Contains("--email", plan.Arguments);
        Assert.Contains("user@example.com", plan.Arguments);
        Assert.Contains("--inbox-limit", plan.Arguments);
        Assert.Contains("10", plan.Arguments);
        Assert.Contains("--proxy", plan.Arguments);
        Assert.Contains("http://proxy:8080", plan.Arguments);
        Assert.Single(plan.Environment);
        Assert.Equal("abc123", plan.Environment["REMAIL_SERVICE_TOKEN"]);
    }

    [Fact]
    public void CreateChangeEmail_UsesProviderWorkersAndTemporaryEmailFile()
    {
        var plan = BackendCommandPlanner.CreateChangeEmail(
            new[] { "a@example.com", "b@example.com" },
            provider: "icloud",
            mailboxFile: "C:\\mailboxes.txt",
            workers: 2,
            smailrDomain: "",
            cfworkerDomain: "",
            proxyPool: new[] { "http://proxy:8080" },
            tempDirectory: null!);

        Assert.Contains("--change-email", plan.Arguments);
        Assert.Contains("--change-email-provider", plan.Arguments);
        Assert.Contains("icloud", plan.Arguments);
        Assert.Contains("--change-email-workers", plan.Arguments);
        Assert.Contains("2", plan.Arguments);
        Assert.Contains("--change-email-mailbox-file", plan.Arguments);
        Assert.Contains("--proxy", plan.Arguments);
        Assert.Contains("http://proxy:8080", plan.Arguments);
        Assert.Single(plan.TempFiles);
    }

    // ── Shared helpers ──────────────────────────────────────────────────

    [Fact]
    public void MailboxArgumentForLine_ReturnsChataiForDashedLine()
    {
        string result = BackendCommandPlanner.MailboxArgumentForLine(
            "user@example.com----password----clientId----refreshToken");
        Assert.Equal("--chatai-mailbox-file", result);
    }

    [Fact]
    public void MailboxArgumentForLine_ReturnsMailboxForTripleDash()
    {
        string result = BackendCommandPlanner.MailboxArgumentForLine(
            "user@example.com---password---refreshToken");
        Assert.Equal("--mailbox-file", result);
    }

    [Fact]
    public void MailboxArgumentForLine_ReturnsMailboxForCfWorker()
    {
        string result = BackendCommandPlanner.MailboxArgumentForLine(
            "cfworker://abc123");
        Assert.Equal("--mailbox-file", result);
    }

    [Fact]
    public void MailboxArgumentForLine_ReturnsEmptyForComment()
    {
        Assert.Equal("", BackendCommandPlanner.MailboxArgumentForLine("# comment"));
    }

    [Fact]
    public void MailboxArgumentForLine_ReturnsEmptyForEmptyLine()
    {
        Assert.Equal("", BackendCommandPlanner.MailboxArgumentForLine(""));
        Assert.Equal("", BackendCommandPlanner.MailboxArgumentForLine("   "));
    }

    [Fact]
    public void NormalizeImportTarget_NormalizesCorrectly()
    {
        Assert.Equal("sub2api", BackendCommandPlanner.NormalizeImportTarget("sub2api"));
        Assert.Equal("sub2api", BackendCommandPlanner.NormalizeImportTarget("SUB2API"));
        Assert.Equal("cliproxyapi", BackendCommandPlanner.NormalizeImportTarget("cliproxyapi"));
        Assert.Equal("cpa", BackendCommandPlanner.NormalizeImportTarget("cpa"));
        Assert.Equal("cpa", BackendCommandPlanner.NormalizeImportTarget("unknown"));
        Assert.Equal("cpa", BackendCommandPlanner.NormalizeImportTarget(""));
    }

    [Fact]
    public void ImportTargetLabel_ReturnsCorrectLabel()
    {
        Assert.Equal("SUB2API", BackendCommandPlanner.ImportTargetLabel("sub2api"));
        Assert.Equal("CLIProxyAPI", BackendCommandPlanner.ImportTargetLabel("cliproxyapi"));
        Assert.Equal("CPA", BackendCommandPlanner.ImportTargetLabel("cpa"));
        Assert.Equal("CPA", BackendCommandPlanner.ImportTargetLabel(""));
    }

    [Fact]
    public void AppendProxyPool_SingleProxy_AddsScalar()
    {
        var args = new List<string>();
        BackendCommandPlanner.AppendProxyPool(args, new[] { "http://proxy:8080" });
        Assert.Contains("--proxy", args);
        Assert.Contains("http://proxy:8080", args);
        Assert.DoesNotContain("--proxy-pool", args);
    }

    [Fact]
    public void AppendProxyPool_MultipleProxies_AddsPool()
    {
        var args = new List<string>();
        BackendCommandPlanner.AppendProxyPool(args, new[] { "http://p1:8080", "http://p2:8080" });
        Assert.Contains("--proxy", args);
        Assert.Contains("--proxy-pool", args);
    }

    [Fact]
    public void AppendProxyPool_EmptyPool_DoesNothing()
    {
        var args = new List<string>();
        BackendCommandPlanner.AppendProxyPool(args, Array.Empty<string>());
        Assert.Empty(args);
    }

    [Fact]
    public void AppendSessionFile_AddsWhenNonEmpty()
    {
        var args = new List<string>();
        BackendCommandPlanner.AppendSessionFile(args, "C:\\session.json");
        Assert.Contains("--session-file", args);
        Assert.Contains("C:\\session.json", args);
    }

    [Fact]
    public void AppendSessionFile_SkipsWhenEmpty()
    {
        var args = new List<string> { "--existing" };
        BackendCommandPlanner.AppendSessionFile(args, "");
        Assert.Single(args);
    }
}
