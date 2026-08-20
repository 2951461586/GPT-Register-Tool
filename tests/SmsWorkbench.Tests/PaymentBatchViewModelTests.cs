using System.Text.Json;
using SmsWorkbench;

namespace SmsWorkbench.Tests;

public sealed class PaymentBatchViewModelTests
{
    [Fact]
    public async Task RunCommandBuildsProbeRequestFromUniqueAccounts()
    {
        var service = new StubPaymentBatchService();
        var viewModel = new PaymentBatchViewModel(
            service,
            new StubFileLauncher(),
            new[]
            {
                new PaymentBatchAccount("User@example.com", true),
                new PaymentBatchAccount("user@example.com", false),
                new PaymentBatchAccount("second@example.com", false)
            })
        {
            ProbeOnly = true,
            CanaryText = "1",
            BatchId = "probe id"
        };
        string statusDuringRun = "";
        service.OnRun = () => statusDuringRun = viewModel.Status;

        await viewModel.RunCommand.ExecuteAsync(null);

        Assert.NotNull(service.LastRequest);
        Assert.True(service.LastRequest.ProbeOnly);
        Assert.Equal(2, service.LastRequest.Accounts.Count);
        Assert.Equal(1, service.LastRequest.Canary);
        Assert.StartsWith("momo_", service.LastRequest.BatchId);
        Assert.NotEqual("probe_id", service.LastRequest.BatchId);
        Assert.Equal("正在执行 Checkout 与 Stripe init 支付能力探测...", statusDuringRun);
        Assert.True(viewModel.HasRun);
        Assert.Single(viewModel.Results);
    }

    [Fact]
    public void PayPalAuthorizationQueueVisibilityFollowsSelectedMethod()
    {
        var viewModel = new PaymentBatchViewModel(
            new StubPaymentBatchService(),
            new StubFileLauncher(),
            new[] { new PaymentBatchAccount("user@example.com", true) });

        Assert.False(viewModel.IsPayPalSelected);
        viewModel.SelectedMethod = viewModel.PaymentMethodOptions.First(option => option.Id == "paypal");
        Assert.True(viewModel.IsPayPalSelected);
        viewModel.SelectedMethod = viewModel.PaymentMethodOptions.First(option => option.Id == "momo");
        Assert.False(viewModel.IsPayPalSelected);
    }

    [Fact]
    public async Task BatchUsesSeparateCheckoutAndApprovePoolsAndCanSaveThem()
    {
        var service = new StubPaymentBatchService
        {
            ProxyConfiguration = new PaymentBatchProxyConfiguration(
                "http://checkout-config",
                "http://approve-config",
                "ID",
                "TR")
        };
        var viewModel = new PaymentBatchViewModel(
            service,
            new StubFileLauncher(),
            new[] { new PaymentBatchAccount("user@example.com", true) })
        {
            ProbeOnly = true,
            CanaryText = "1",
            CheckoutProxyPool = "http://checkout-one\nhttp://checkout-two",
            ApproveProxyPool = "http://approve-jp\nhttp://approve-tr",
            CheckoutProxyCountry = "ID",
            ApproveProxyCountry = "JP",
        };

        await viewModel.RunCommand.ExecuteAsync(null);

        Assert.NotNull(service.LastRequest);
        Assert.Equal("http://checkout-one\nhttp://checkout-two", service.LastRequest!.CheckoutProxyPool);
        Assert.Equal("http://approve-jp\nhttp://approve-tr", service.LastRequest.ApproveProxyPool);
        Assert.Equal("ID", service.LastRequest.CheckoutCountry);
        Assert.Equal("JP", service.LastRequest.ApproveCountry);
        // The matrix UI is hidden: the single neutral cell carries no stage
        // countries so every account follows the shared proxy settings above.
        Assert.Equal("", service.LastRequest.MatrixRows[0].CheckoutCountry);
        Assert.Equal("", service.LastRequest.MatrixRows[0].ApproveCountry);
        Assert.Equal("", service.LastRequest.MatrixRows[0].RegistrationCountry);
        Assert.Equal(1, service.LastRequest.MatrixRows[0].SampleSize);

        viewModel.SaveProxyConfigurationCommand.Execute(null);
        Assert.Equal("http://checkout-one\nhttp://checkout-two", service.LastSaved!.CheckoutProxyPool);
        Assert.Equal("http://approve-jp\nhttp://approve-tr", service.LastSaved.ApproveProxyPool);
        Assert.Equal("ID", service.LastSaved.CheckoutCountry);
        Assert.Equal("JP", service.LastSaved.ApproveCountry);
        Assert.Equal("JP", service.LastSaved.UpdateCountry);

        await viewModel.TestProxiesCommand.ExecuteAsync(null);
        Assert.Equal("ID", service.LastProbeCheckoutCountry);
        Assert.Equal("JP", service.LastProbeApproveCountry);
    }

    [Fact]
    public async Task ResumedPresenceOnlyPaymentResultIsVisibleButNotCopyable()
    {
        var service = new StubPaymentBatchService("""
            {
              "ok": true,
              "report_path": "report.json",
              "counts": { "requested": 1, "authenticated": 1, "link_ready": 1 },
              "results": [
                {
                  "account_ref": "resumed-ref",
                  "authenticated": true,
                  "decision": "ready",
                  "url_present": true,
                  "attempts": 1
                }
              ]
            }
            """);
        var viewModel = new PaymentBatchViewModel(
            service,
            new StubFileLauncher(),
            new[] { new PaymentBatchAccount("user@example.com", true) });

        await viewModel.RunCommand.ExecuteAsync(null);

        PaymentBatchResultRow row = Assert.Single(viewModel.Results);
        Assert.Equal("支付链接", row.ResultKind);
        Assert.Equal("已生成（报告仅保留存在状态）", row.ResultDisplay);
        Assert.True(row.ResultPresent);
        Assert.False(row.HasCopyableResult);
    }

    [Fact]
    public async Task RunCommandDisplaysConcretePaymentResultsInsteadOfReadyDecision()
    {
        var service = new StubPaymentBatchService("""
            {
              "ok": false,
              "report_path": "report.json",
              "counts": { "requested": 4, "authenticated": 4 },
              "results": [
                {
                  "account_ref": "link@example.com",
                  "authenticated": true,
                  "decision": "ready",
                  "url": "https://pay.example/short",
                  "long_url": "https://pay.example/long",
                  "qr_data": "qr-ignored",
                  "attempts": 1
                },
                {
                  "account_ref": "qr@example.com",
                  "authenticated": true,
                  "decision": "ready_with_qr",
                  "qr_data": "000201010212...",
                  "qr_path": "C:\\runtime\\qr.png",
                  "attempts": 1
                },
                {
                  "account_ref": "qr-file@example.com",
                  "authenticated": true,
                  "decision": "ready_with_qr",
                  "qr_path": "C:\\runtime\\qr-only.png",
                  "attempts": 1
                },
                {
                  "account_ref": "failed@example.com",
                  "authenticated": true,
                  "decision": "checkout_failed",
                  "error": "provider rejected checkout",
                  "attempts": 1
                }
              ]
            }
            """);
        var viewModel = new PaymentBatchViewModel(
            service,
            new StubFileLauncher(),
            new[] { new PaymentBatchAccount("user@example.com", true) });

        await viewModel.RunCommand.ExecuteAsync(null);

        Assert.Collection(
            viewModel.Results,
            link =>
            {
                Assert.Equal("支付链接", link.ResultKind);
                Assert.Equal("https://pay.example/short", link.ResultValue);
                Assert.Equal(link.ResultValue, link.ResultDisplay);
                Assert.True(link.HasCopyableResult);
            },
            qr =>
            {
                Assert.Equal("二维码内容", qr.ResultKind);
                Assert.Equal("000201010212...", qr.ResultValue);
                Assert.Equal(qr.ResultValue, qr.ResultDisplay);
            },
            qrFile =>
            {
                Assert.Equal("二维码文件", qrFile.ResultKind);
                Assert.Equal("C:\\runtime\\qr-only.png", qrFile.ResultValue);
            },
            failed =>
            {
                Assert.Equal("checkout_failed", failed.ResultDisplay);
                Assert.False(failed.HasCopyableResult);
            });
    }

    [Fact]
    public void CountryOptionsComeFromThePaymentCatalog()
    {
        var viewModel = new PaymentBatchViewModel(
            new StubPaymentBatchService(),
            new StubFileLauncher(),
            new[] { new PaymentBatchAccount("user@example.com", true) });

        Assert.Equal("momo", viewModel.SelectedMethod.Id);
        Assert.Equal(new PaymentProxyCountryOption("", "自动（跟随账单区）"), viewModel.CheckoutCountryOptions[0]);
        Assert.Equal(
            PaymentMethods.CheckoutCountryOptions("momo"),
            viewModel.CheckoutCountryOptions.Skip(1).ToArray());
        Assert.Equal(PaymentMethods.ApproveCountryOptions("momo"), viewModel.ApproveCountryOptions);
    }

    private static readonly PaymentProxyCountryOption[] StubCheckoutDefaults = { new("US", "美国 US") };
    private static readonly PaymentProxyCountryOption[] StubApproveDefaults = { new("JP", "日本 JP") };
    private static readonly PaymentProxyCountryOption[] StubGoPayCheckoutOverride = { new("ID", "印度尼西亚 ID") };
    private static readonly PaymentProxyCountryOption[] StubGoPayApproveOverride = { new("TR", "土耳其 TR") };
    private static readonly string[] ExpectedCheckoutDefaultCodes = { "", "US" };
    private static readonly string[] ExpectedApproveDefaultCodes = { "JP" };
    private static readonly string[] ExpectedGoPayCheckoutCodes = { "", "ID" };
    private static readonly string[] ExpectedGoPayApproveCodes = { "TR" };

    [Fact]
    public void SwitchingMethodReResolvesCountryOptionsFromCatalogOverrides()
    {
        var catalog = new StubCountryCatalog(
            checkoutDefaults: StubCheckoutDefaults,
            approveDefaults: StubApproveDefaults,
            checkoutOverrides: new Dictionary<string, IReadOnlyList<PaymentProxyCountryOption>>
            {
                ["gopay"] = StubGoPayCheckoutOverride
            },
            approveOverrides: new Dictionary<string, IReadOnlyList<PaymentProxyCountryOption>>
            {
                ["gopay"] = StubGoPayApproveOverride
            });
        var service = new StubPaymentBatchService
        {
            ProxyConfiguration = new PaymentBatchProxyConfiguration("", "", "", "")
        };
        var viewModel = new PaymentBatchViewModel(
            service,
            new StubFileLauncher(),
            new[] { new PaymentBatchAccount("user@example.com", true) },
            catalog);

        Assert.Equal(ExpectedCheckoutDefaultCodes, viewModel.CheckoutCountryOptions.Select(option => option.Code).ToArray());
        Assert.Equal(ExpectedApproveDefaultCodes, viewModel.ApproveCountryOptions.Select(option => option.Code).ToArray());
        Assert.Equal("JP", viewModel.ApproveProxyCountry);

        viewModel.SelectedMethod = viewModel.PaymentMethodOptions.First(option => option.Id == "gopay");

        Assert.Equal(ExpectedGoPayCheckoutCodes, viewModel.CheckoutCountryOptions.Select(option => option.Code).ToArray());
        Assert.Equal(ExpectedGoPayApproveCodes, viewModel.ApproveCountryOptions.Select(option => option.Code).ToArray());
        Assert.Equal("TR", viewModel.ApproveProxyCountry);
    }

    [Fact]
    public async Task NeutralMatrixCellAllowsBatchRunWithoutCohorts()
    {
        var service = new StubPaymentBatchService();
        var viewModel = new PaymentBatchViewModel(
            service,
            new StubFileLauncher(),
            new[] { new PaymentBatchAccount("user@example.com", true) });

        await viewModel.RunCommand.ExecuteAsync(null);

        // The hidden matrix is replaced by one neutral cell that must always
        // be valid, so the backend request is still created.
        Assert.NotNull(service.LastRequest);
        Assert.Single(service.LastRequest!.MatrixRows);
        Assert.Equal("", service.LastRequest.MatrixRows[0].RegistrationCountry);
        Assert.True(viewModel.HasRun);
    }

    [Fact]
    public void ManualAccessTokensCreateAtRowsAndEnforceLimit()
    {
        var viewModel = new PaymentBatchViewModel(
            new StubPaymentBatchService(),
            new StubFileLauncher(),
            Array.Empty<PaymentBatchAccount>())
        {
            ManualAccessTokens = "at-one\nat-two"
        };

        Assert.Equal("手动 AT 2 / 10", viewModel.AccountSummary);
        viewModel.RunCommand.NotifyCanExecuteChanged();
        Assert.True(viewModel.RunCommand.CanExecute(null));

        viewModel.ManualAccessTokens = string.Join('\n', Enumerable.Range(1, 11).Select(i => "at-" + i));
        Assert.False(viewModel.RunCommand.CanExecute(null));
    }

    [Fact]
    public async Task ProgressAndHashedFinalReferenceResolveToDisplayedAccount()
    {
        var service = new ProgressStubPaymentBatchService();
        var viewModel = new PaymentBatchViewModel(
            service,
            new StubFileLauncher(),
            new[] { new PaymentBatchAccount("User@example.com", true) });

        await viewModel.RunCommand.ExecuteAsync(null);

        PaymentBatchResultRow row = Assert.Single(viewModel.Results);
        Assert.Equal("User@example.com", row.AccountRef);
        Assert.Equal("100%", row.ProgressText);
        Assert.Equal("成功", row.ResultStatus);
    }

    [Fact]
    public async Task StaleRunningEventCannotRegressTerminalRowToExecuting()
    {
        SynchronizationContext? previousContext = SynchronizationContext.Current;
        SynchronizationContext.SetSynchronizationContext(new ImmediateSynchronizationContext());
        try
        {
        var service = new OutOfOrderProgressPaymentBatchService();
        var viewModel = new PaymentBatchViewModel(
            service,
            new StubFileLauncher(),
            new[] { new PaymentBatchAccount("User@example.com", true) });

        await viewModel.RunCommand.ExecuteAsync(null);

        PaymentBatchResultRow row = Assert.Single(viewModel.Results);
        Assert.Equal("100%", row.ProgressText);
        Assert.Equal("成功", row.ResultStatus);
        Assert.Equal("完成", row.CurrentStage);
        }
        finally
        {
            SynchronizationContext.SetSynchronizationContext(previousContext);
        }
    }

    private sealed class StubCountryCatalog : IPaymentCountryCatalog
    {
        private readonly IReadOnlyList<PaymentProxyCountryOption> _checkoutDefaults;
        private readonly IReadOnlyList<PaymentProxyCountryOption> _approveDefaults;
        private readonly IReadOnlyDictionary<string, IReadOnlyList<PaymentProxyCountryOption>> _checkoutOverrides;
        private readonly IReadOnlyDictionary<string, IReadOnlyList<PaymentProxyCountryOption>> _approveOverrides;

        public StubCountryCatalog(
            IReadOnlyList<PaymentProxyCountryOption> checkoutDefaults,
            IReadOnlyList<PaymentProxyCountryOption> approveDefaults,
            IReadOnlyDictionary<string, IReadOnlyList<PaymentProxyCountryOption>> checkoutOverrides,
            IReadOnlyDictionary<string, IReadOnlyList<PaymentProxyCountryOption>> approveOverrides)
        {
            _checkoutDefaults = checkoutDefaults;
            _approveDefaults = approveDefaults;
            _checkoutOverrides = checkoutOverrides;
            _approveOverrides = approveOverrides;
        }

        public IReadOnlyList<PaymentProxyCountryOption> CheckoutCountryOptions(string paymentMethod)
            => _checkoutOverrides.TryGetValue(paymentMethod, out IReadOnlyList<PaymentProxyCountryOption>? options)
                ? options
                : _checkoutDefaults;

        public IReadOnlyList<PaymentProxyCountryOption> ApproveCountryOptions(string paymentMethod)
            => _approveOverrides.TryGetValue(paymentMethod, out IReadOnlyList<PaymentProxyCountryOption>? options)
                ? options
                : _approveDefaults;
    }

    private class StubPaymentBatchService : IPaymentBatchService
    {
        private const string DefaultReport = """
            {
              "ok": true,
              "report_path": "report.json",
              "counts": { "requested": 2, "authenticated": 2 },
              "results": [
                {
                  "account_ref": "user@example.com",
                  "authenticated": true,
                  "decision": "probe_authenticated",
                  "attempts": 0
                }
              ]
            }
            """;

        private readonly string _report;

        public PaymentBatchProxyConfiguration ProxyConfiguration { get; set; } =
            new("", "", "", "JP");

        public PaymentBatchProxyConfiguration? LastSaved { get; private set; }

        public StubPaymentBatchService(string? report = null)
        {
            _report = report ?? DefaultReport;
        }

        public PaymentBatchRequest? LastRequest { get; private set; }

        public Action? OnRun { get; set; }

        public IReadOnlyList<PaymentMatrixRow> LoadMatrix(string paymentMethod) => Array.Empty<PaymentMatrixRow>();

        public PaymentBatchProxyConfiguration LoadProxyConfiguration(string paymentMethod)
            => ProxyConfiguration;

        public SettingsSaveResult SaveProxyConfiguration(
            string paymentMethod,
            PaymentBatchProxyConfiguration configuration)
        {
            LastSaved = configuration;
            return new(true);
        }

        public PaymentMatrixRow CreateDefaultMatrixRow(string paymentMethod) => new()
        {
            Name = "default",
            SampleSize = 1
        };

        public Task<JsonElement> RunAsync(
            PaymentBatchRequest request,
            CancellationToken cancellationToken)
        {
            LastRequest = request;
            OnRun?.Invoke();
            using JsonDocument document = JsonDocument.Parse(_report);
            return Task.FromResult(document.RootElement.Clone());
        }

        public string? LastProbeMethod { get; private set; }
        public string? LastProbeCheckoutCountry { get; private set; }
        public string? LastProbeApproveCountry { get; private set; }

        public Task<JsonElement> ProbeProxiesAsync(
            string paymentMethod,
            string checkoutProxyPool,
            string approveProxyPool,
            string checkoutCountry,
            string approveCountry,
            CancellationToken cancellationToken)
        {
            LastProbeMethod = paymentMethod;
            LastProbeCheckoutCountry = checkoutCountry;
            LastProbeApproveCountry = approveCountry;
            const string probe = """
                {
                  "ok": true,
                  "payment_method": "paypal",
                  "stages": {
                    "checkout": { "ok": true, "ip": "203.0.113.9", "country_code": "US", "region": "CA", "expected_country_paypal_supported": true },
                    "approve": { "ok": true, "ip": "203.0.113.10", "country_code": "GB", "region": "ENG", "expected_country_paypal_supported": true }
                  }
                }
                """;
            using JsonDocument document = JsonDocument.Parse(probe);
            return Task.FromResult(document.RootElement.Clone());
        }
    }

    private sealed class ProgressStubPaymentBatchService : StubPaymentBatchService, IPaymentBatchProgressService
    {
        public ProgressStubPaymentBatchService()
            : base("""{"ok":true,"counts":{"requested":1},"results":[{"account_ref":"b4c9a289323b21a0","ok":true,"terminal_state":"completed"}]}""")
        {
        }

        public Task<JsonElement> RunAsync(PaymentBatchRequest request, IProgress<BackendOutputLine> progress, CancellationToken cancellationToken)
        {
            string line = "@@SMSWORKBENCH_V2@@{\"schema\":\"smsworkbench.ipc.v2\",\"version\":2,\"type\":\"event\",\"terminal\":true,\"payload\":{\"domain\":\"payment\",\"account_ref\":\"b4c9a289323b21a0\",\"stage\":\"completed\",\"status\":\"completed\"}}";
            progress.Report(new BackendOutputLine(BackendOutputChannel.StandardOutput, line));
            using JsonDocument document = JsonDocument.Parse("{\"ok\":true,\"counts\":{\"requested\":1},\"results\":[{\"account_ref\":\"b4c9a289323b21a0\",\"ok\":true,\"terminal_state\":\"completed\"}]}");
            return Task.FromResult(document.RootElement.Clone());
        }
    }

    private sealed class OutOfOrderProgressPaymentBatchService : StubPaymentBatchService, IPaymentBatchProgressService
    {
        public OutOfOrderProgressPaymentBatchService()
            : base("""{"ok":true,"counts":{"requested":1},"results":[{"account_ref":"b4c9a289323b21a0","ok":true,"terminal_state":"completed"}]}""")
        {
        }

        public Task<JsonElement> RunAsync(PaymentBatchRequest request, IProgress<BackendOutputLine> progress, CancellationToken cancellationToken)
        {
            progress.Report(new BackendOutputLine(BackendOutputChannel.StandardOutput, Event("completed", "completed", true)));
            progress.Report(new BackendOutputLine(BackendOutputChannel.StandardOutput, Event("adapter", "running", false)));
            return Task.FromException<JsonElement>(new InvalidOperationException("stop after progress"));
        }

        private static string Event(string stage, string status, bool terminal)
            => $"@@SMSWORKBENCH_V2@@{{\"schema\":\"smsworkbench.ipc.v2\",\"version\":2,\"type\":\"event\",\"terminal\":{terminal.ToString().ToLowerInvariant()},\"payload\":{{\"domain\":\"payment\",\"account_ref\":\"b4c9a289323b21a0\",\"stage\":\"{stage}\",\"status\":\"{status}\",\"account_terminal\":{terminal.ToString().ToLowerInvariant()}}}}}";
    }

    private sealed class ImmediateSynchronizationContext : SynchronizationContext
    {
        public override void Post(SendOrPostCallback callback, object? state) => callback(state);
    }
}
