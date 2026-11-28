namespace SmsWorkbench.Tests;

/// <summary>
/// Row decisions read the backend's machine state, not the grid's display text.
///
/// <para>
/// The 2026-10-26 fix made five predicates match the label in *every* shipped
/// language, which removed the language variable but not the root cause: they
/// still parsed prose, so the next rewording silently changed behaviour. Since
/// 2026-11-20 <see cref="AccountMachineState"/> owns those decisions and reads
/// <c>registration_status</c> / <c>refresh_token_status</c> /
/// <c>at_probe_status_code</c> / <c>promotion_state</c> /
/// <c>payment_qualification_state</c> / the TOTP booleans.
/// </para>
///
/// <para>
/// These tests pin the property that matters: for a row that carries a machine
/// value, the label is <b>irrelevant</b> — including a label that appears in no
/// catalog at all (the case a future rewording produces). The legacy fallback
/// keeps working for rows persisted before those fields existed, and those cases
/// are pinned too, because dropping them would recolour half of an existing pool.
/// </para>
/// </summary>
public sealed class AccountMachineStateTests
{
    private static PoolRow Row(
        string registrationStatus = "",
        string status = "",
        string refreshTokenStatus = "",
        bool hasAccessToken = false,
        string probeCode = "",
        bool twoFactor = false,
        string promotionState = "",
        string promotionStatus = "",
        string qualificationState = "",
        string accountType = "",
        string sourcePath = "") =>
        new()
        {
            RegistrationStatus = registrationStatus,
            Status = status,
            RefreshTokenStatus = refreshTokenStatus,
            HasAccessToken = hasAccessToken,
            AccessTokenProbeStatusCode = probeCode,
            TwoFactorEnabled = twoFactor,
            PromotionState = promotionState,
            PromotionStatus = promotionStatus,
            PaymentQualificationState = qualificationState,
            AccountType = accountType,
            SourcePath = sourcePath,
        };

    // ── the machine value wins over the label, in both directions ────────────

    [Fact]
    public void A_registered_machine_row_counts_even_when_the_label_says_otherwise()
    {
        // The label is a wording no catalog knows: exactly what a reword produces.
        var row = Row(registrationStatus: "registered", status: "Account ready (new wording)");

        Assert.True(AccountMachineState.StatusSaysRegistered(row));
        Assert.True(AccountMachineState.IsRegistered(row));
        Assert.False(AccountMachineState.NeedsRegistration(row));
        Assert.Equal(AccountMachineState.Success, AccountMachineState.StatusSeverity(row));
    }

    [Fact]
    public void A_pending_machine_row_is_offered_for_registration_even_when_the_label_says_registered()
    {
        // A stale label must not hide an unfinished row.
        var row = Row(registrationStatus: "pending", status: "已注册");

        Assert.False(AccountMachineState.StatusSaysRegistered(row));
        Assert.False(AccountMachineState.IsRegistered(row));
        Assert.True(AccountMachineState.NeedsRegistration(row));
        Assert.Equal(AccountMachineState.Warn, AccountMachineState.StatusSeverity(row));
    }

    [Fact]
    public void An_unrecognised_machine_state_falls_back_to_the_label_and_still_needs_attention()
    {
        // The `default:` branch exists so a state a future backend adds cannot
        // render neutral (invisible) and drop out of the attention counter.
        var row = Row(registrationStatus: "some_future_state", status: "已注册");

        Assert.True(AccountMachineState.StatusSaysRegistered(row));
        Assert.Equal(AccountMachineState.Success, AccountMachineState.StatusSeverity(row));
        Assert.True(AccountMachineState.NeedsAttention(row));
    }

    [Theory]
    [InlineData("registered", "success")]
    [InlineData("partial_registered", "warn")]
    [InlineData("at_probe_pending", "warn")]
    [InlineData("pending", "warn")]
    [InlineData("retry_pending", "warn")]
    [InlineData("failed", "danger")]
    [InlineData("dead_end", "danger")]
    [InlineData("quarantined", "danger")]
    [InlineData("cancelled", "danger")]
    [InlineData("batch_paused", "danger")]
    public void Status_severity_follows_the_registration_state(string state, string expected)
        => Assert.Equal(expected, AccountMachineState.StatusSeverity(Row(registrationStatus: state, status: "ignored wording")));

    // ── legacy rows keep the label rule ─────────────────────────────────────

    [Theory]
    [InlineData("已注册", true)]
    [InlineData("Registered", true)]
    [InlineData("支付完成✅", true)]
    [InlineData("Payment completed", true)]
    [InlineData("已导入CPA", true)]
    [InlineData("Imported CPA", true)]
    [InlineData("待支付", false)]
    [InlineData("Pending payment", false)]
    [InlineData("", false)]
    public void A_row_without_machine_state_still_decides_from_its_label(string label, bool expected)
        => Assert.Equal(expected, AccountMachineState.StatusSaysRegistered(Row(status: label)));

    [Fact]
    public void Legacy_severity_still_colours_a_row_that_predates_the_machine_fields()
    {
        Assert.Equal(AccountMachineState.Success, AccountMachineState.StatusSeverity(Row(status: "已注册")));
        Assert.Equal(AccountMachineState.Danger, AccountMachineState.StatusSeverity(Row(status: "AT失效")));
        Assert.Equal(AccountMachineState.Neutral, AccountMachineState.StatusSeverity(Row()));
    }

    // ── token badges ────────────────────────────────────────────────────────

    [Theory]
    [InlineData("oauth_present", "success")]
    [InlineData("legacy_present", "warn")]
    [InlineData("no_rt", "danger")]
    [InlineData("missing", "danger")]
    [InlineData("", "neutral")]
    public void Refresh_token_severity_reads_the_code(string code, string expected)
        => Assert.Equal(expected, AccountMachineState.RefreshTokenSeverity(Row(refreshTokenStatus: code)));

    [Fact]
    public void A_missing_refresh_token_is_danger_not_neutral()
    {
        // Measured bug: the badge compared display strings, so the raw code
        // `no_rt` matched no rule and rendered neutral grey.
        Assert.Equal(AccountMachineState.Danger, AccountMachineState.RefreshTokenSeverity(Row(refreshTokenStatus: "no_rt")));
    }

    [Fact]
    public void Access_token_severity_reads_presence_and_the_probe_code()
    {
        Assert.Equal(AccountMachineState.Danger, AccountMachineState.AccessTokenSeverity(Row()));
        Assert.Equal(AccountMachineState.Success, AccountMachineState.AccessTokenSeverity(Row(hasAccessToken: true)));
        Assert.Equal(
            AccountMachineState.Success,
            AccountMachineState.AccessTokenSeverity(Row(hasAccessToken: true, probeCode: "200")));
        Assert.Equal(
            AccountMachineState.Danger,
            AccountMachineState.AccessTokenSeverity(Row(hasAccessToken: true, probeCode: "401")));
    }

    [Theory]
    [InlineData(true, "success")]
    [InlineData(false, "warn")]
    public void Two_factor_severity_reads_the_machine_flag(bool enabled, string expected)
        => Assert.Equal(expected, AccountMachineState.TwoFactorSeverity(Row(twoFactor: enabled)));

    // ── promotion badge ─────────────────────────────────────────────────────

    [Theory]
    [InlineData("trial_eligible", "success")]
    [InlineData("subscribed", "info")]
    [InlineData("free", "neutral")]
    [InlineData("auth_invalid", "danger")]
    [InlineData("probe_failed", "danger")]
    [InlineData("unknown", "info")]
    public void Promotion_severity_reads_the_machine_state(string state, string expected)
        => Assert.Equal(expected, AccountMachineState.PromotionSeverity(Row(promotionState: state)));

    [Fact]
    public void The_promotion_badge_no_longer_depends_on_the_label_wording()
    {
        // A trial-eligible row always renders success, whatever the label says.
        Assert.Equal(
            AccountMachineState.Success,
            AccountMachineState.PromotionSeverity(Row(promotionState: "trial_eligible", promotionStatus: "无优惠")));
        // ...and an auth-invalid row is danger even if the label was translated
        // into wording the legacy table never learned.
        Assert.Equal(
            AccountMachineState.Danger,
            AccountMachineState.PromotionSeverity(Row(promotionState: "auth_invalid", promotionStatus: "Login expired")));
        // Legacy fallback: no state, so the label rule answers.
        Assert.Equal(
            AccountMachineState.Success,
            AccountMachineState.PromotionSeverity(Row(promotionStatus: "可试用Plus·-100%·×1month")));
    }

    // ── counters, filters and export ────────────────────────────────────────

    [Fact]
    public void The_registered_counter_follows_machine_state_not_the_label()
    {
        var pool = new PoolViewModel();
        pool.AllRows.Add(Row(registrationStatus: "registered", status: "wording nobody shipped", sourcePath: "pool.txt"));
        pool.AllRows.Add(Row(registrationStatus: "pending", status: "已注册", sourcePath: "pool.txt"));
        pool.AllRows.Add(Row(status: "Registered", sourcePath: "pool.txt")); // legacy row

        pool.UpdateOverview();

        Assert.Equal("2", pool.RegisteredCountText);
    }

    [Fact]
    public void The_attention_filter_uses_machine_state()
    {
        Assert.True(AccountMachineState.NeedsAttention(Row(registrationStatus: "failed")));
        Assert.True(AccountMachineState.NeedsAttention(Row(registrationStatus: "partial_registered")));
        Assert.True(AccountMachineState.NeedsAttention(Row(registrationStatus: "at_probe_transport_unknown")));
        Assert.True(AccountMachineState.NeedsAttention(Row(registrationStatus: "account_deactivated")));
        Assert.True(AccountMachineState.NeedsAttention(Row(registrationStatus: "at_invalid")));
        // Missing RT ("no_rt" / "missing") is normal on AT-only email accounts and
        // must not count toward Overview "异常/待处理".
        Assert.False(AccountMachineState.NeedsAttention(Row(registrationStatus: "registered", refreshTokenStatus: "no_rt")));
        Assert.False(AccountMachineState.NeedsAttention(Row(registrationStatus: "registered", refreshTokenStatus: "missing")));
        Assert.False(AccountMachineState.NeedsAttention(Row(registrationStatus: "active", status: "已注册", refreshTokenStatus: "no_rt")));
        Assert.True(AccountMachineState.NeedsAttention(Row(registrationStatus: "active", status: "账号掉号")));
        // Fresh unregistered mailbox-pool rows emit registration_status="unknown"
        // with Status="可收信" and must not inflate the attention counter.
        Assert.False(AccountMachineState.NeedsAttention(Row(registrationStatus: "unknown", status: "可收信")));
        Assert.False(
            AccountMachineState.NeedsAttention(
                Row(registrationStatus: "registered", refreshTokenStatus: "oauth_present")));
        // Legacy label path: missing RT alone is also excluded from attention.
        Assert.False(AccountMachineState.NeedsAttention(new PoolRow { Status = "已注册", RefreshToken = "未获取" }));
        Assert.True(AccountMachineState.NeedsAttention(Row(status: "网络失败")));
    }

    [Fact]
    public void Backend_account_rows_resolve_live_status_over_frozen_active_registration_state()
    {
        var healthy = AccountRowFactory.BuildBackendAccountRow(
            new Dictionary<string, object>
            {
                ["id"] = "1",
                ["email"] = "ok@example.com",
                ["status"] = "registered",
                ["registration_state"] = "active",
                ["refresh_token_status"] = "no_rt",
                ["has_access_token"] = "true",
            },
            "accounts.sqlite3",
            "{}");
        Assert.Equal("registered", healthy.RegistrationStatus);
        Assert.False(AccountMachineState.NeedsAttention(healthy));

        var deactivated = AccountRowFactory.BuildBackendAccountRow(
            new Dictionary<string, object>
            {
                ["id"] = "2",
                ["email"] = "dead@example.com",
                ["status"] = "account_deactivated",
                ["registration_state"] = "active",
                ["refresh_token_status"] = "no_rt",
                ["has_access_token"] = "true",
            },
            "accounts.sqlite3",
            "{}");
        Assert.Equal("account_deactivated", deactivated.RegistrationStatus);
        Assert.True(AccountMachineState.NeedsAttention(deactivated));

        var probeUnknown = AccountRowFactory.BuildBackendAccountRow(
            new Dictionary<string, object>
            {
                ["id"] = "3",
                ["email"] = "pending@example.com",
                ["status"] = "pending",
                ["registration_state"] = "at_probe_transport_unknown",
                ["refresh_token_status"] = "no_rt",
                ["has_access_token"] = "true",
            },
            "accounts.sqlite3",
            "{}");
        Assert.Equal("at_probe_transport_unknown", probeUnknown.RegistrationStatus);
        Assert.True(AccountMachineState.NeedsAttention(probeUnknown));
    }

    [Fact]
    public void Export_accepts_a_machine_registered_row_without_reading_its_label()
    {
        Assert.True(AccountMachineState.CanExport(Row(registrationStatus: "registered", status: "unknown wording")));
        Assert.True(AccountMachineState.CanExport(Row(hasAccessToken: true)));
        Assert.False(AccountMachineState.CanExport(Row(registrationStatus: "pending")));
    }

    [Fact]
    public void A_session_row_is_still_registered_without_any_status_field()
    {
        // Unchanged behaviour for the pool-file rows: the account type decides.
        Assert.True(AccountMachineState.IsRegistered(Row(accountType: "OAuth Session")));
        Assert.True(AccountMachineState.IsRegistered(Row(sourcePath: "accounts.sqlite3")));
    }
}
