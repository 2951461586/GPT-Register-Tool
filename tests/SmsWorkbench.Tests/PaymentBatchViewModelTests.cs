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

        await viewModel.RunCommand.ExecuteAsync(null);

        Assert.NotNull(service.LastRequest);
        Assert.True(service.LastRequest.ProbeOnly);
        Assert.Equal(2, service.LastRequest.Accounts.Count);
        Assert.Equal(1, service.LastRequest.Canary);
        Assert.Equal("probe_id", service.LastRequest.BatchId);
        Assert.True(viewModel.HasRun);
        Assert.Single(viewModel.Results);
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
        public PaymentBatchRequest? LastRequest { get; private set; }

        public IReadOnlyList<PaymentMatrixRow> LoadMatrix(string paymentMethod) => Array.Empty<PaymentMatrixRow>();

        public PaymentMatrixRow CreateDefaultMatrixRow(string paymentMethod) => new()
        {
            Name = "default",
            SampleSize = 1
        };

        public Task<JsonElement> RunAsync(PaymentBatchRequest request, CancellationToken cancellationToken)
        {
            LastRequest = request;
            using JsonDocument document = JsonDocument.Parse("""
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
                """);
            return Task.FromResult(document.RootElement.Clone());
        }
    }
}
