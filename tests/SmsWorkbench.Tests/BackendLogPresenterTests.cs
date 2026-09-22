using System.Text.Json;

namespace SmsWorkbench.Tests;

public class BackendLogPresenterTests
{
    [Fact]
    public void TaskStartLineHidesCliArgs()
    {
        string line = BackendLogPresenter.TaskStartLine("选中未注册邮箱注册");
        Assert.Equal("=========== 启动：选中未注册邮箱注册 ==========", line);
        Assert.DoesNotContain("python", line);
        Assert.DoesNotContain("--mailbox-file", line);
    }

    [Theory]
    // The terminal `result` frame is routine and fully redundant: every lane
    // prints its own human-readable closing line immediately before it, the
    // `batch_completed` stage line already closes scan/promotion runs, and the
    // outcome lands in the task grid and the result dialog anyway. Showing it
    // only ever repeated "go look at the dialog". `ok:false` is included on
    // purpose -- a failed run is still reported by its own `[!]` lines, so the
    // frame stays dropped regardless of the verdict it carries.
    [InlineData("@@SMSWORKBENCH_V2@@{\"version\":2,\"type\":\"result\",\"payload\":{\"ok\":true}}")]
    [InlineData("@@SMSWORKBENCH_V2@@{\"version\":2,\"type\":\"result\",\"payload\":{\"ok\":false}}")]
    // Same frame after the progress-dot run that used to defeat the prefix
    // test, so the drop cannot be dodged by a glued head.
    [InlineData(".........@@SMSWORKBENCH_V2@@{\"version\":2,\"type\":\"result\",\"payload\":{\"ok\":true}}")]
    public void TerminalResultEnvelopeIsNotShown(string raw)
    {
        Assert.Null(BackendLogPresenter.FormatLine(raw));
    }

    [Theory]
    // A progress `event` frame reaching the presenter means event parsing
    // already failed upstream, so it is a genuine diagnostic rather than
    // routine noise -- the pointer line has to stay. The dot run is glued on
    // purpose: it is the exact shape that used to defeat the prefix test and
    // leak hundreds of characters of raw JSON into the panel.
    [InlineData(".........@@SMSWORKBENCH_V2@@{\"version\":2,\"type\":\"event\"}")]
    [InlineData("..@@SMSWORKBENCH_V2@@{\"version\":2,\"type\":\"event\"}")]
    public void MalformedEventEnvelopeKeepsThePointerLine(string raw)
    {
        Assert.Equal("[*] 任务返回结构化结果（详情见结果弹窗与任务列表）",
            BackendLogPresenter.FormatLine(raw));
    }

    [Theory]
    // Fail loud: an envelope that cannot be parsed carries no frame type, so
    // it must not be silently swallowed by the `result` drop. Kept visible.
    [InlineData("@@SMSWORKBENCH_V2@@not json at all")]
    [InlineData("@@SMSWORKBENCH_V2@@{\"version\":2,\"type\":")]
    [InlineData("@@SMSWORKBENCH_V2@@{\"version\":2,\"payload\":{\"ok\":true}}")]
    [InlineData("@@SMSWORKBENCH_V2@@[1,2,3]")]
    public void UnparseableEnvelopeKeepsThePointerLine(string raw)
    {
        Assert.Equal("[*] 任务返回结构化结果（详情见结果弹窗与任务列表）",
            BackendLogPresenter.FormatLine(raw));
    }


    [Theory]
    [InlineData("==========================")]
    [InlineData("######################################")]
    public void BannerBarsAreSuppressed(string raw)
    {
        Assert.Null(BackendLogPresenter.FormatLine(raw));
    }

    [Theory]
    [InlineData("ChatGPT Email Batch Registration - 50 accounts", "── 批量注册开始 · 共 50 个账号 ──")]
    [InlineData("Account 3/50", "── 账号 3/50 ──")]
    public void BatchBannersBecomeStageHeaders(string raw, string expected)
    {
        Assert.Equal(expected, BackendLogPresenter.FormatLine(raw));
    }

    [Theory]
    // No timestamp in the fixtures on purpose: MainWindow.Helpers prepends
    // `[HH:mm:ss] ` *after* FormatLine runs, so an assertion that baked the
    // stamp into its input would pass while the real pipeline saw no stamp at
    // all. Indent stripping has to work on the bare Python output.
    [InlineData("  [Device] Reusing persisted device context",
        "[Device] Reusing persisted device context")]
    // Kept as the negative case for the noise denylist: `Email OTP validate`
    // carries the 4xx reason for deactivated accounts and must survive even
    // though it is unmarked ASCII, just like Python's
    // `Skipped: phone number already registered`.
    [InlineData("Email OTP validate: https://example.com 200",
        "Email OTP validate: https://example.com 200")]
    [InlineData("    Skipped: phone number already registered, not saving to database",
        "Skipped: phone number already registered, not saving to database")]
    [InlineData("\t\tdeep tab indent", "deep tab indent")]
    public void LeadingIndentIsStrippedSoBodiesShareOneLeftEdge(string raw, string expected)
    {
        Assert.Equal(expected, BackendLogPresenter.FormatLine(raw));
    }

    [Theory]
    // Per-account, per-stage step tracing: 26 accounts of this is ~150 lines
    // that no operator can act on.
    [InlineData("Existing account login: starting email OTP flow")]
    [InlineData("Existing account authorize: 200 https://auth.openai.com/email-verification")]
    [InlineData("Existing account continue: 200")]
    [InlineData("Existing account continue follow: 200 https://auth.openai.com/email-verification")]
    [InlineData("Existing account OTP send: /api/accounts/email-otp/resend 200 {\"success\": true}")]
    [InlineData("Protocol diagnostic[existing_authorize_continue]: {\"http_status\": 200}")]
    [InlineData("  Protocol diagnostic[existing_authorize_continue]: {\"http_status\": 200}")]
    [InlineData("Redirect[signin]: 302 /api/accounts/authorize cookies={}")]
    [InlineData("Signup username continue: 200 https://auth.openai.com/about-you")]
    [InlineData("Email verification page: 200")]
    [InlineData("code:****45!")]
    [InlineData("[*] Protocol auth session refreshed.")]
    [InlineData("[*] Waiting for protocol auth session... 403")]
    [InlineData("[*] Auth session refreshed.")]
    [InlineData("[*] Waiting for auth session... 403")]
    [InlineData("[*] Codex OAuth protocol stage: email_otp")]
    [InlineData("[*] Passwordless OTP send skipped: send-otp 409")]
    [InlineData("[*] Passwordless OTP send already pending; continuing to mailbox polling")]
    [InlineData("[*] Email OTP resend: 200")]
    [InlineData("[*] Email OTP resend already pending; keeping previous OTP search window")]
    public void HotPathNoiseIsSuppressed(string raw)
    {
        Assert.Null(BackendLogPresenter.FormatLine(raw));
    }

    [Theory]
    // Negative cases: same `[*]` marker, same neighbourhood as the suppressed
    // lines, but these carry the reason an account failed. A denylist that
    // grows by prefix will eventually eat one of these, which is why they are
    // asserted rather than assumed.
    [InlineData("[*] Email OTP validate failed: 403 {\"error\": {\"message\": \"deactivated\"}}")]
    [InlineData("[*] OAuth state invalid, restarting auth flow (attempt 1/3)")]
    [InlineData("[*] Session state invalid, restarting auth flow (attempt 1/3)")]
    public void FailureReasonsSurviveTheDenylist(string raw)
    {
        Assert.Equal(raw, BackendLogPresenter.FormatLine(raw));
    }

    [Theory]
    // Progress dots are written with end="", so if the Python-side suppression
    // is ever bypassed they arrive glued to the head of a line. The denylist
    // has to keep working through them, and a bare dot run is noise on its own.
    [InlineData(".........")]
    [InlineData(".  Existing account OTP send: /api/accounts/email-otp/resend 200 {\"success\": true}")]
    [InlineData(".........Protocol diagnostic[existing_authorize_continue]: {\"http_status\": 200}")]
    [InlineData("....[*] Codex OAuth protocol stage: email_otp")]
    public void NoiseIsSuppressedEvenWhenGluedBehindProgressDots(string raw)
    {
        Assert.Null(BackendLogPresenter.FormatLine(raw));
    }

    [Fact]
    public void TimeoutSurvivesBehindProgressDots()
    {
        // The one thing a dot-glued poll line still has to tell the operator.
        Assert.Equal("timeout", BackendLogPresenter.FormatLine("................... timeout"));
    }

    [Theory]
    [InlineData("[*] Account 3/50 user@example.com: registered")]
    [InlineData("[!] Registration failed for user@example.com: browser_email_verification_stuck")]
    public void StagedPrintsPassThroughUnchanged(string raw)
    {
        Assert.Equal(raw, BackendLogPresenter.FormatLine(raw));
    }

    // ── Structured progress events → panel stage lines ──────────────────

    private static BackendProgressEvent ScanEvent(
        string domain, string stage, string status = "running", int total = 0, string detail = "")
        => new(domain, "run-1", "a@example.com", "", stage, status, detail, Total: total);

    [Theory]
    [InlineData("account_scan", 50, "── 账号测活开始 · 共 50 个账号 ──")]
    [InlineData("account_promotion", 8, "── 账号优惠检测开始 · 共 8 个账号 ──")]
    // Case-insensitive domain match: the backend owns the exact casing.
    [InlineData("Account_Scan", 3, "── 账号测活开始 · 共 3 个账号 ──")]
    public void ProgressEventLine_RendersBatchStartPerDomain(string domain, int total, string expected)
    {
        Assert.Equal(expected,
            BackendLogPresenter.ProgressEventLine(ScanEvent(domain, "batch_started", "running", total)));
    }

    [Fact]
    public void ProgressEventLine_RendersBatchCompletedWithDetail()
    {
        string? line = BackendLogPresenter.ProgressEventLine(
            ScanEvent("account_scan", "batch_completed", "completed", 50,
                "正常 45/50，AT失效 3，掉号 1，超时 1"));
        Assert.Equal("── 账号测活结束 · 正常 45/50，AT失效 3，掉号 1，超时 1 ──", line);
    }

    [Fact]
    public void ProgressEventLine_RendersOneClickSmsAccountOutcome()
    {
        var progress = new BackendProgressEvent(
            "one_click_sms", "run-1", "a***@example.com", "", "failed", "failed",
            "email_otp_poll_timeout", FailureClass: "mailbox");
        Assert.Equal(
            "一键接码 · a***@example.com · failed · 失败 · email_otp_poll_timeout",
            BackendLogPresenter.ProgressEventLine(progress));
    }

    [Fact]
    public void ProgressEventLine_SurvivesMissingDetailAndTotal()
    {
        Assert.Equal("── 账号测活开始 ──",
            BackendLogPresenter.ProgressEventLine(ScanEvent("account_scan", "batch_started")));
        Assert.Equal("── 账号测活结束 ──",
            BackendLogPresenter.ProgressEventLine(ScanEvent("account_scan", "batch_completed")));
    }

    [Theory]
    [InlineData("account_completed")]
    [InlineData("probe")]
    [InlineData("")]
    public void ProgressEventLine_SkipsPerAccountRows(string stage)
    {
        // Failures already print from Python with the richer relogin note;
        // duplicating every row here would re-flood the panel.
        Assert.Null(BackendLogPresenter.ProgressEventLine(ScanEvent("account_scan", stage)));
    }

    [Theory]
    [InlineData("registration")]
    [InlineData("payment")]
    [InlineData("account_health")]
    [InlineData("")]
    public void ProgressEventLine_IgnoresOtherDomains(string domain)
    {
        Assert.Null(BackendLogPresenter.ProgressEventLine(ScanEvent(domain, "batch_started", "running", 4)));
    }

    [Fact]
    public void ProgressEventLine_AcceptsNull()
    {
        Assert.Null(BackendLogPresenter.ProgressEventLine(null));
    }

    [Theory]
    [InlineData("{\"ok\": true}", true)]
    [InlineData("    \"email\": \"user@example.com\",", true)]
    [InlineData("}", true)]
    [InlineData("[", true)]
    [InlineData("[{", true)]
    [InlineData("[*] marker stays", false)]
    [InlineData("[!] failure marker stays", false)]
    [InlineData("[-] dash marker stays", false)]
    [InlineData("[*] Account 1/50: registered", false)]
    // The backend's bracketed marker vocabulary must never be read as JSON.
    // Each of these used to open an unparseable block, so the line was
    // swallowed and replaced by the "无法解析的多行输出" line -- 231 of the
    // 240 such lines measured in one real backend_stdout.log came from here.
    [InlineData("[0-Extract sentinel token]", false)]
    [InlineData("[2-Auth flow]", false)]
    [InlineData("[10-Finalize registration]", false)]
    [InlineData("[Error] registration_preflight_failed:no_healthy_route", false)]
    [InlineData("[WARN] config_unread_keys: 61 key(s) set but never read", false)]
    [InlineData("[ OK ] python: 3.11.8", false)]
    public void JsonLookingLinesAreClassified(string trimmed, bool expected)
    {
        Assert.Equal(expected, BackendLogPresenter.LooksLikeJson(trimmed));
    }

    [Fact]
    public void PrettyJsonBlockFoldsIntoResultsSummary()
    {
        var folder = new BackendLogFolder();
        var lines = new List<string>();
        foreach (string raw in new[]
                 {
                     "{",
                     "  \"ok\": true,",
                     "  \"total\": 3,",
                     "  \"success\": 2,",
                     "  \"failed\": 1,",
                     "  \"trial_eligible\": 1,",
                     "  \"results\": [",
                     "    {\"email\": \"a@x.com\", \"ok\": true, \"probe\": {\"status\": \"active\"}},",
                     "    {\"email\": \"b@x.com\", \"ok\": false, \"probe\": {\"status\": \"account_deactivated\"}},",
                     "    {\"email\": \"c@x.com\", \"ok\": false, \"error\": \"timeout\"}",
                     "  ]",
                     "}",
                 })
        {
            lines.AddRange(folder.Feed(raw));
        }

        Assert.Single(lines);
        Assert.Contains("成功 1/3", lines[0]);
        Assert.Contains("注销 1", lines[0]);
        Assert.Contains("可试优惠 1", lines[0]);
        // No raw JSON text may leak into the panel.
        Assert.DoesNotContain("\"", lines[0]);
    }

    [Fact]
    public void SingleLineJsonFoldsIntoFailureLine()
    {
        var folder = new BackendLogFolder();
        var lines = folder.Feed("{\"ok\": false, \"error\": \"missing_access_token\"}").ToList();
        Assert.Single(lines);
        Assert.StartsWith("[!]", lines[0]);
        Assert.Contains("missing_access_token", lines[0]);
    }

    [Fact]
    public void UnterminatedJsonBlockFoldsAndContinues()
    {
        var folder = new BackendLogFolder();
        var lines = new List<string>();
        lines.AddRange(folder.Feed("{"));
        lines.AddRange(folder.Feed("  \"broken\":"));
        lines.AddRange(folder.Feed("[*] Account 1/50 user@example.com: registered"));

        Assert.Equal(2, lines.Count);
        Assert.Contains("折叠", lines[0]);
        Assert.Equal("[*] Account 1/50 user@example.com: registered", lines[1]);
    }

    [Theory]
    [InlineData("[0-Extract sentinel token]")]
    [InlineData("[2-Auth flow]")]
    [InlineData("[8d-Validate access token]")]
    [InlineData("[10-Finalize registration]")]
    [InlineData("[Error] registration_preflight_failed:no_healthy_route:RuntimeError")]
    [InlineData("[WARN] config_unread_keys: 61 key(s) set but never read")]
    [InlineData("[ OK ] python: 3.11.8")]
    public void BracketedMarkersReachThePanelUnfolded(string raw)
    {
        var folder = new BackendLogFolder();
        var lines = folder.Feed(raw).ToList();

        Assert.Single(lines);
        Assert.Equal(raw, lines[0]);
        Assert.DoesNotContain("折叠", lines[0]);
    }

    [Fact]
    public void BlockLargerThanTheOldLineCapStillFolds()
    {
        // A `--doctor --json` report measures 569 lines. The old 500-line cap
        // tripped mid-document, the partial buffer failed to parse, and the
        // operator got "无法解析的多行输出" instead of a summary.
        var folder = new BackendLogFolder();
        var lines = new List<string>();
        lines.AddRange(folder.Feed("{"));
        lines.AddRange(folder.Feed("  \"checks\": ["));
        for (int i = 0; i < 600; i++)
            lines.AddRange(folder.Feed($"    {{\"name\": \"check{i}\", \"status\": \"ok\"}},"));
        lines.AddRange(folder.Feed("    {\"name\": \"last\", \"status\": \"ok\"}"));
        lines.AddRange(folder.Feed("  ]"));
        lines.AddRange(folder.Feed("}"));

        Assert.Single(lines);
        Assert.Equal("[*] 后端返回了结构化结果（已在日志中折叠）", lines[0]);
    }

    [Fact]
    public void EnvelopeInsideFolderNeverLeaksJson()
    {
        // End-to-end through the folder: a terminal `result` frame is dropped
        // outright (its lane prints its own closing line first), so nothing
        // must surface -- least of all the payload. A progress `event` frame
        // that reaches the folder still yields the pointer line, and that line
        // must stay free of raw JSON.
        var resultLines = new BackendLogFolder().Feed(
            "@@SMSWORKBENCH_V2@@{\"version\":2,\"schema\":\"smsworkbench.ipc.v2\",\"type\":\"result\",\"payload\":{\"results\":[],\"total\":0}}").ToList();
        Assert.Empty(resultLines);

        var eventLines = new BackendLogFolder().Feed(
            "@@SMSWORKBENCH_V2@@{\"version\":2,\"schema\":\"smsworkbench.ipc.v2\",\"type\":\"event\",\"payload\":{\"stage\":\"batch_started\"}}").ToList();
        Assert.Single(eventLines);
        Assert.DoesNotContain("payload", eventLines[0]);
        Assert.DoesNotContain("{", eventLines[0]);
    }

    [Fact]
    public void GenericObjectFoldsIntoNeutralLine()
    {
        JsonDocument document = JsonDocument.Parse("{\"quota\": {\"used\": 3}}");
        string summary = BackendJsonSummary.Summarize(document);
        Assert.Equal("[*] 后端返回了结构化结果（已在日志中折叠）", summary);
    }
}
