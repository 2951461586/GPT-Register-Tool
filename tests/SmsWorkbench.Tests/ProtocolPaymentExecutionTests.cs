using SmsWorkbench;

namespace SmsWorkbench.Tests;

public sealed class ProtocolPaymentExecutionPlannerTests
{
    [Fact]
    public void PayPalAccountPlanPreservesSessionRefreshAndStageOptions()
    {
        ProtocolPaymentExecutionPlan plan = ProtocolPaymentExecutionPlanner.Create(
            Request(
                paymentMethod: "paypal",
                accountEmail: " user@example.com ",
                sessionFile: "C:\\sessions\\user.json",
                jitRefresh: false,
                requireZero: false,
                requireBaToken: true));

        Assert.Equal("PayPal 协议提链", plan.TaskName);
        Assert.Equal("正在执行 PayPal 协议提链...", plan.StatusText);
        Assert.Equal("extract_link", plan.Operation);
        Assert.True(plan.MayHaveSideEffects);
        string[] expectedArguments =
        {
            "--extract-payment-link",
            "--payment-method", "paypal",
            "--target-country", "US",
            "--email", "user@example.com",
            "--session-file", "C:\\sessions\\user.json",
            "--checkout-proxy-pool", "http://checkout-one\nhttp://checkout-two",
            "--approve-proxy-pool", "http://approve-one",
            "--no-jit-at-refresh",
            "--checkout-proxy-country", "US",
            "--approve-proxy-country", "TR",
            "--update-proxy-country", "TR",
            "--no-require-zero",
            "--require-ba-token",
        };
        Assert.Equal(expectedArguments, plan.Arguments);
    }

    [Fact]
    public void ProxyTestKeepsDirectCardLinkTransport()
    {
        IReadOnlyList<string> arguments = ProtocolPaymentExecutionPlanner.CreateProxyTestArguments(
            "direct-card",
            "",
            " http://checkout-one\nhttp://checkout-two ",
            "http://approve-one",
            "us",
            "ph",
            "tr");

        Assert.Equal("direct_card", ArgumentAfter(arguments, "--payment-method"));
        Assert.Equal("http://checkout-one\nhttp://checkout-two", ArgumentAfter(arguments, "--checkout-proxy-pool"));
        Assert.Equal("http://approve-one", ArgumentAfter(arguments, "--approve-proxy-pool"));
        Assert.Equal("US", ArgumentAfter(arguments, "--checkout-proxy-country"));
        Assert.Equal("PH", ArgumentAfter(arguments, "--approve-proxy-country"));
        Assert.Equal("TR", ArgumentAfter(arguments, "--update-proxy-country"));
    }

    [Fact]
    public void DirectCardPlanIsLinkExtractionOnly()
    {
        ProtocolPaymentExecutionPlan plan = ProtocolPaymentExecutionPlanner.Create(Request("direct_card"));

        Assert.Equal("extract_link", plan.Operation);
        Assert.False(plan.MayHaveSideEffects);
        Assert.DoesNotContain("--blik-code", plan.Arguments);
    }

    [Fact]
    public void SessionOnlyProbeAlwaysCarriesProbeFlagAndNeverBlikCode()
    {
        ProtocolPaymentExecutionPlan plan = ProtocolPaymentExecutionPlanner.Create(
            Request(
                paymentMethod: "blik",
                accountEmail: "",
                sessionFile: "C:\\sessions\\user.json",
                probeOnly: true));

        Assert.Equal("payment_method_capability_probe", plan.Operation);
        Assert.False(plan.MayHaveSideEffects);
        Assert.Contains("--payment-probe-only", plan.Arguments);
        Assert.DoesNotContain("--blik-code", plan.Arguments);
    }

    [Fact]
    public void BlikPlanUsesPaymentOperationOnlyOutsideProbe()
    {
        ProtocolPaymentExecutionRequest request = Request("blik") with { BlikCode = "123456" };
        ProtocolPaymentExecutionPlan plan = ProtocolPaymentExecutionPlanner.Create(request);

        Assert.Equal("execute_payment", plan.Operation);
        Assert.Equal("BLIK 协议支付", plan.TaskName);
        Assert.Contains("--blik-code", plan.Arguments);
    }

    private static ProtocolPaymentExecutionRequest Request(
        string paymentMethod,
        string accountEmail = "user@example.com",
        string sessionFile = "C:\\sessions\\user.json",
        bool jitRefresh = true,
        bool probeOnly = false,
        bool requireZero = true,
        bool requireBaToken = false)
        => new(
            paymentMethod,
            "US",
            "",
            "http://checkout-one\nhttp://checkout-two",
            "http://approve-one",
            jitRefresh,
            probeOnly,
            requireZero,
            requireBaToken,
            "",
            "US",
            "TR",
            "TR",
            accountEmail,
            sessionFile);

    private static string ArgumentAfter(IReadOnlyList<string> arguments, string option)
    {
        int index = arguments.ToList().IndexOf(option);
        Assert.True(index >= 0 && index + 1 < arguments.Count, $"Missing argument {option}");
        return arguments[index + 1];
    }
}

public sealed class ProtocolPaymentResultPresenterTests
{
    [Fact]
    public void SuccessfulResultProducesDisplayTextAndCopyTargets()
    {
        ProtocolPaymentResultPresentation result = ProtocolPaymentResultPresenter.Parse(
            """
            {
              "ok": true,
              "status": "completed",
              "operation": "extract_link",
              "message": "verified",
              "probe": { "status_code": 200 },
              "refreshed": true,
              "token_telemetry": { "age_seconds": 12, "expires_in_seconds": 3600 },
              "url": "https://pay.example/short",
              "hosted_url": "https://pay.example/hosted",
              "state": "completed",
              "payment_method": "direct_card",
              "qr_path": "C:\\runtime\\payment.png",
              "amount": 0,
              "currency": "usd",
              "approval_ok": true,
              "target_country": "PH"
            }
            """);

        Assert.Equal("https://pay.example/short", result.Url);
        Assert.Equal("C:\\runtime\\payment.png", result.QrPath);
        Assert.Contains("[成功] 提取成功!", result.Text, StringComparison.Ordinal);
        Assert.Equal("extract_link", result.Operation);
        Assert.Equal("completed", result.TerminalState);
        Assert.Contains("AT 探测: HTTP 200", result.Text, StringComparison.Ordinal);
        Assert.Contains("审批状态: 已批准", result.Text, StringComparison.Ordinal);
    }

    [Theory]
    [InlineData("unknown", "[结果未知，请先核对账号状态，不要重试]")]
    [InlineData("cancelled", "[已取消]")]
    [InlineData("timed_out", "[已超时]")]
    public void TerminalFailureUsesExistingUiClassification(string state, string prefix)
    {
        ProtocolPaymentResultPresentation result = ProtocolPaymentResultPresenter.Parse(
            $$"""
            { "ok": false, "state": "{{state}}", "error": "request stopped", "error_code": "E_STOP" }
            """);

        Assert.StartsWith(prefix, result.Text, StringComparison.Ordinal);
        Assert.Contains("错误代码: E_STOP", result.Text, StringComparison.Ordinal);
        Assert.Equal(state, result.TerminalState);
        Assert.Equal("", result.Url);
        Assert.Equal("", result.QrPath);
    }

    [Fact]
    public void IneligiblePaymentFailureShowsKeyFieldsInsteadOfRawJson()
    {
        ProtocolPaymentResultPresentation result = ProtocolPaymentResultPresenter.Parse(
            """
            {
              "ok": false,
              "decision": "account_trial_ineligible",
              "decision_text": "账号没有真正试用资格，且未检测到 MoMo",
              "payment_method": "momo",
              "subscription_plan": "chatgptfreeplan",
              "amount_due": 0,
              "currency": "usd",
              "has_qr": false,
              "qr_path": ""
            }
            """);

        Assert.Contains("[失败] 账号没有真正试用资格，且未检测到 MoMo", result.Text, StringComparison.Ordinal);
        Assert.Contains("判定: account_trial_ineligible", result.Text, StringComparison.Ordinal);
        Assert.Contains("支付方式: momo", result.Text, StringComparison.Ordinal);
        Assert.Contains("订阅状态: chatgptfreeplan", result.Text, StringComparison.Ordinal);
        Assert.Contains("应付金额: 0 USD", result.Text, StringComparison.Ordinal);
        Assert.DoesNotContain("{", result.Text, StringComparison.Ordinal);
        Assert.DoesNotContain("\"decision\"", result.Text, StringComparison.Ordinal);
    }

    [Fact]
    public void CompletedExecutePaymentIsShownAsPaymentOnlyWhenOperationSaysSo()
    {
        ProtocolPaymentResultPresentation result = ProtocolPaymentResultPresenter.Parse(
            "{ \"ok\": true, \"status\": \"completed\", \"operation\": \"execute_payment\" }");

        Assert.Contains("[成功] 支付已完成", result.Text, StringComparison.Ordinal);
        Assert.Equal("execute_payment", result.Operation);
    }

    [Fact]
    public void AbortingSideEffectingPlanRequiresReconciliation()
    {
        ProtocolPaymentExecutionPlan plan = ProtocolPaymentExecutionPlanner.Create(
            new ProtocolPaymentExecutionRequest(
                "momo",
                "VN",
                "http://proxy.example:8080",
                "http://checkout-ignored",
                "http://approve-ignored",
                true,
                false,
                true,
                false,
                "",
                "VN",
                "VN",
                "VN",
                "user@example.com",
                "C:\\sessions\\user.json"));
        ProtocolPaymentResultPresentation result = ProtocolPaymentResultPresenter.Aborted(plan, "cancelled");

        Assert.Equal("unknown", result.TerminalState);
        Assert.False(result.Retryable);
        Assert.True(result.RequiresReconciliation);
        Assert.Contains("不要重试", result.Text, StringComparison.Ordinal);
    }

    [Fact]
    public void NonJsonBackendOutputIsShownVerbatim()
    {
        ProtocolPaymentResultPresentation result = ProtocolPaymentResultPresenter.Parse("plain backend output");

        Assert.Equal("plain backend output", result.Text);
        Assert.Equal("", result.Url);
        Assert.Equal("", result.QrPath);
    }

    [Fact]
    public void SensitiveValuesAreFullyRedactedFromOperatorText()
    {
        const string url = "https://pay.example/approve?ba_token=BA-secret-value";
        ProtocolPaymentResultPresentation result = ProtocolPaymentResultPresenter.Parse(
            $$"""
            { "ok": true, "status": "completed", "operation": "extract_link", "url": "{{url}}", "card_last4": "4242" }
            """);

        Assert.Equal(url, result.Url);
        Assert.DoesNotContain("BA-secret", result.Text, StringComparison.Ordinal);
        Assert.DoesNotContain("4242", result.Text, StringComparison.Ordinal);
        Assert.Contains("[REDACTED]", result.Text, StringComparison.Ordinal);
    }
}
