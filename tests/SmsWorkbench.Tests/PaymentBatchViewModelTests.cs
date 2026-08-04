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
        Assert.Equal("probe_id", service.LastRequest.BatchId);
        Assert.Equal("正在执行 Checkout 与 Stripe init 支付能力探测...", statusDuringRun);
        Assert.True(viewModel.HasRun);
        Assert.Single(viewModel.Results);
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
    public async Task InvalidMatrixStopsBeforeBackendExecution()
    {
        var service = new StubPaymentBatchService();
        var viewModel = new PaymentBatchViewModel(
            service,
            new StubFileLauncher(),
            new[] { new PaymentBatchAccount("user@example.com", true) });
        viewModel.MatrixRows[0].RegistrationCountry = "USA";

        await viewModel.RunCommand.ExecuteAsync(null);

        Assert.Null(service.LastRequest);
        Assert.Contains("两位字母", viewModel.Status, StringComparison.Ordinal);
        Assert.False(viewModel.HasRun);
    }

    private sealed class StubPaymentBatchService : IPaymentBatchService
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

        public StubPaymentBatchService(string? report = null)
        {
            _report = report ?? DefaultReport;
        }

        public PaymentBatchRequest? LastRequest { get; private set; }

        public Action? OnRun { get; set; }

        public IReadOnlyList<PaymentMatrixRow> LoadMatrix(string paymentMethod) => Array.Empty<PaymentMatrixRow>();

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
    }
}
