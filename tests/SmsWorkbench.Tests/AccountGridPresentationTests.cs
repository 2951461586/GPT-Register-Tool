using System.ComponentModel;

namespace SmsWorkbench.Tests;

public sealed class AccountGridPresentationTests
{
    [Theory]
    [InlineData("可试用Plus·-100%·×1month")]
    [InlineData("可试用 plus")]
    public void TrialEligiblePromotionIsRecognized(string status)
    {
        Assert.True(PromotionStatusPresentation.IsTrialEligible(status));
        Assert.Equal(0, PromotionStatusPresentation.SortRank(status));
    }

    // ── 支付资格 badge severity ───────────────────────────────────────────────
    //
    // Measured 2026-10-28: the badge fed the composed label into
    // StatusSeverityConverter, whose rules only matched "AT失效" and "检测失败".
    // 可用 / 无可用方式 / 无0元优惠 / 币种不符 / 出口被拒 / 未确定 therefore all
    // rendered as BadgeNeutralFg (#57606A) — "the column is always grey".
    // The colour now keys off payment_qualification_state
    // (sms_tool/payment/qualification_states.py), so a label reword or a language
    // switch cannot change it.

    [Theory]
    [InlineData("eligible", "success")]
    [InlineData("ineligible", "warn")]
    [InlineData("auth_invalid", "danger")]
    [InlineData("egress_denied", "warn")]
    [InlineData("probe_failed", "danger")]
    [InlineData("unknown", "info")]
    [InlineData("", "neutral")]
    [InlineData("not-a-real-state", "neutral")]
    public void QualificationSeverityFollowsTheMachineState(string state, string expected)
        => Assert.Equal(expected, PaymentQualificationSeverityConverter.SeverityFor(state, null));

    [Fact]
    public void QualificationStateWinsOverTheLabel()
    {
        // A stale or mistranslated label must not recolour the badge.
        Assert.Equal(
            "success",
            PaymentQualificationSeverityConverter.SeverityFor("eligible", "无可用方式"));
        Assert.Equal(
            "danger",
            PaymentQualificationSeverityConverter.SeverityFor("auth_invalid", "可用·PayPal"));
    }

    [Theory]
    [InlineData("可用·PayPal", "success")]
    [InlineData("可用", "success")]
    [InlineData("Available·PayPal", "success")]
    [InlineData("无可用方式", "warn")]
    [InlineData("无可用方式·地区不符", "warn")]
    [InlineData("No payment method", "warn")]
    [InlineData("无0元优惠", "warn")]
    [InlineData("币种不符", "warn")]
    [InlineData("Currency mismatch", "warn")]
    [InlineData("出口被拒", "warn")]
    [InlineData("Egress denied", "warn")]
    [InlineData("AT失效", "danger")]
    [InlineData("AT invalid", "danger")]
    [InlineData("检测失败", "danger")]
    [InlineData("Check failed", "danger")]
    [InlineData("风控拦截", "danger")]
    [InlineData("Risk blocked", "danger")]
    [InlineData("未确定", "info")]
    [InlineData("Undetermined", "info")]
    [InlineData("", "neutral")]
    public void LegacyRowsWithoutAStateStillGetAColour(string label, string expected)
    {
        // Rows written before the state field existed carry only the label.
        Assert.Equal(expected, PaymentQualificationSeverityConverter.SeverityFor(null, label));
    }

    [Fact]
    public void ConverterReadsStateThenLabelFromTheMultiBinding()
    {
        var converter = new PaymentQualificationSeverityConverter();

        Assert.Equal(
            "success",
            converter.Convert(
                new object[] { "eligible", "可用·PayPal" },
                typeof(string),
                null!,
                System.Globalization.CultureInfo.InvariantCulture));
        Assert.Equal(
            "warn",
            converter.Convert(
                new object[] { "", "无可用方式" },
                typeof(string),
                null!,
                System.Globalization.CultureInfo.InvariantCulture));
        Assert.Equal(
            "neutral",
            converter.Convert(
                new object[] { null!, null! },
                typeof(string),
                null!,
                System.Globalization.CultureInfo.InvariantCulture));
    }

    [Fact]
    public void TrialEligiblePromotionUsesSuccessSeverity()
    {
        var converter = new StatusSeverityConverter();

        object severity = converter.Convert(
            "可试用Plus·-100%·×1month",
            typeof(string),
            null!,
            System.Globalization.CultureInfo.InvariantCulture);

        Assert.Equal("success", severity);
    }

    [Fact]
    public void PromotionOrderingRunsBeforePagingAndPrioritizesTrialEligibleRows()
    {
        var rows = new[]
        {
            new PoolRow { Identifier = "empty@example.com", PromotionStatus = "" },
            new PoolRow { Identifier = "none@example.com", PromotionStatus = "无优惠" },
            new PoolRow { Identifier = "trial@example.com", PromotionStatus = "可试用Plus·-100%·×1month" },
        };

        string[] ordered = AccountGridOrdering.Apply(
                rows,
                nameof(PoolRow.PromotionStatus),
                ListSortDirection.Ascending)
            .Select(row => row.Identifier)
            .ToArray();

        Assert.Equal(new[] { "trial@example.com", "none@example.com", "empty@example.com" }, ordered);
    }
}
