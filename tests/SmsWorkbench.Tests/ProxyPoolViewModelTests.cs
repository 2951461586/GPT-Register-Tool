using System.Text.Json;

namespace SmsWorkbench.Tests;

/// <summary>
/// The per-row latency test must write back into the row and leave every other row
/// (selection, scroll, screening verdicts) alone.
/// </summary>
public sealed class ProxyPoolViewModelTests
{
    private static JsonElement Payload(string json) => JsonDocument.Parse(json).RootElement.Clone();

    private sealed class StubProxyPoolService : IProxyPoolService
    {
        public Func<ProxyPoolRequest, JsonElement>? TestHandler { get; set; }

        public ProxyPoolRequest? LastTestRequest { get; private set; }

        public Task<JsonElement> ListAsync(string lane, CancellationToken cancellationToken)
            => Task.FromResult(Payload("{\"proxies\":[]}"));

        public Task<JsonElement> TestAsync(ProxyPoolRequest request, CancellationToken cancellationToken)
        {
            LastTestRequest = request;
            return Task.FromResult(TestHandler?.Invoke(request) ?? Payload("{\"results\":[]}"));
        }

        public Task<JsonElement> ScreenAsync(ProxyPoolRequest request, CancellationToken cancellationToken)
            => Task.FromResult(Payload("{\"samples\":[]}"));
    }

    [Fact]
    public async Task RowTestWritesLatencyAndVerdictIntoThatRowOnly()
    {
        var service = new StubProxyPoolService
        {
            TestHandler = _ => Payload(
                "{\"results\":[{\"proxy\":\"http://a:1\",\"reachable\":true,\"country_match\":true," +
                "\"latency_ms\":\"123\",\"edge\":\"clean\",\"network_type\":\"residential\"}]}"),
        };
        var viewModel = new ProxyPoolViewModel(service);
        var measured = new ProxyPoolRow { Proxy = "http://a:1", Latency = "", Result = "" };
        var untouched = new ProxyPoolRow { Proxy = "http://b:2", Latency = "999", Result = "可用" };
        viewModel.Rows.Add(measured);
        viewModel.Rows.Add(untouched);

        await viewModel.MeasureRowAsync(measured);

        Assert.Equal("123", measured.Latency);
        Assert.Equal("residential", measured.NetworkType);
        Assert.Equal("clean", measured.Edge);
        Assert.False(measured.IsTesting);
        // The measurement must not rebuild the grid.
        Assert.Equal(2, viewModel.Rows.Count);
        Assert.Same(untouched, viewModel.Rows[1]);
        Assert.Equal("999", untouched.Latency);
        Assert.Equal("可用", untouched.Result);
        // One-entry pool, one worker: this is a single exit, not a batch.
        Assert.NotNull(service.LastTestRequest);
        Assert.Equal("http://a:1", service.LastTestRequest!.PoolText);
        Assert.Equal(1, service.LastTestRequest.Workers);
    }

    [Fact]
    public async Task FailingRowTestReportsTheErrorOnTheRow()
    {
        var service = new StubProxyPoolService
        {
            TestHandler = _ => throw new InvalidOperationException("backend offline"),
        };
        var viewModel = new ProxyPoolViewModel(service);
        var row = new ProxyPoolRow { Proxy = "http://a:1" };
        viewModel.Rows.Add(row);

        await viewModel.MeasureRowAsync(row);

        Assert.Equal("-", row.Latency);
        Assert.Contains("backend offline", row.Result);
        Assert.False(row.IsTesting);
    }

    [Fact]
    public async Task EmptyMeasurementLeavesAMarkerInsteadOfABlankCell()
    {
        var service = new StubProxyPoolService
        {
            TestHandler = _ => Payload("{\"results\":[{\"proxy\":\"http://a:1\",\"latency_ms\":\"\"}]}"),
        };
        var viewModel = new ProxyPoolViewModel(service);
        var row = new ProxyPoolRow { Proxy = "http://a:1" };
        viewModel.Rows.Add(row);

        await viewModel.MeasureRowAsync(row);

        Assert.Equal("-", row.Latency);
    }
}
