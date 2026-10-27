// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch stays `annotations`.
#nullable enable

using SmsWorkbench.Localization;

namespace SmsWorkbench
{
    /// <summary>
    /// ProxyPool window view model: one grid, three read-only backends
    /// (list / connectivity test / residential screening).
    /// <para>
    /// The grid rows are rebuilt from the backend payload on every command, never
    /// merged: a screening result and a connectivity result describe the same
    /// proxy differently, and merging them would show a stale "available" next to
    /// a fresh "datacenter".
    /// </para>
    /// </summary>
    public sealed partial class ProxyPoolViewModel : ObservableObject
    {
        private readonly IProxyPoolService _proxyPoolService;

        [ObservableProperty]
        private string lane = "protocol_registration";

        /// <summary>Optional paste box; empty means "use the configured lane".</summary>
        [ObservableProperty]
        private string poolText = "";

        [ObservableProperty]
        private string country = "";

        [ObservableProperty]
        private string workers = "8";

        [ObservableProperty]
        private string qualityMin = "70";

        [ObservableProperty]
        private string statusText = "";

        [ObservableProperty]
        [NotifyCanExecuteChangedFor(nameof(LoadCommand))]
        [NotifyCanExecuteChangedFor(nameof(TestCommand))]
        [NotifyCanExecuteChangedFor(nameof(ScreenCommand))]
        private bool isRunning;

        public ObservableCollection<ProxyPoolRow> Rows { get; } = new();

        public ProxyPoolViewModel(IProxyPoolService proxyPoolService)
        {
            _proxyPoolService = proxyPoolService;
        }

        private bool CanRun() => !IsRunning;

        [RelayCommand(CanExecute = nameof(CanRun), IncludeCancelCommand = true)]
        private async Task LoadAsync(CancellationToken cancellationToken)
        {
            await RunAsync(
                token => _proxyPoolService.ListAsync(Lane, token),
                Loc.T("已加载配置池"),
                cancellationToken).ConfigureAwait(true);
        }

        [RelayCommand(CanExecute = nameof(CanRun), IncludeCancelCommand = true)]
        private async Task TestAsync(CancellationToken cancellationToken)
        {
            await RunAsync(
                token => _proxyPoolService.TestAsync(Request(), token),
                Loc.T("连通性测试完成"),
                cancellationToken).ConfigureAwait(true);
        }

        [RelayCommand(CanExecute = nameof(CanRun), IncludeCancelCommand = true)]
        private async Task ScreenAsync(CancellationToken cancellationToken)
        {
            await RunAsync(
                token => _proxyPoolService.ScreenAsync(Request(), token),
                Loc.T("住宅筛选完成"),
                cancellationToken).ConfigureAwait(true);
        }

        private ProxyPoolRequest Request()
            => new(Lane, PoolText, Country, ParseInt(Workers, 8), 6.0, ParseInt(QualityMin, 70));

        private async Task RunAsync(
            Func<CancellationToken, Task<JsonElement>> operation,
            string successLabel,
            CancellationToken cancellationToken)
        {
            IsRunning = true;
            StatusText = Loc.T("正在执行…");
            try
            {
                JsonElement payload = await operation(cancellationToken).ConfigureAwait(true);
                Apply(payload);
                StatusText = successLabel + " · " + Loc.F("共 {0} 条", Rows.Count);
            }
            catch (OperationCanceledException)
            {
                StatusText = Loc.T("已取消");
            }
            catch (Exception ex)
            {
                StatusText = Loc.T("失败：") + ex.Message;
            }
            finally
            {
                IsRunning = false;
            }
        }

        private static int ParseInt(string value, int fallback)
            => int.TryParse((value ?? "").Trim(), NumberStyles.Integer, CultureInfo.InvariantCulture, out int parsed)
                ? parsed
                : fallback;

        /// <summary>Replace the grid from whichever payload family the backend returned.</summary>
        private void Apply(JsonElement payload)
        {
            Rows.Clear();
            if (payload.ValueKind != JsonValueKind.Object) return;
            if (payload.TryGetProperty("proxies", out JsonElement proxies))
            {
                foreach (JsonElement item in Enumerate(proxies))
                    Rows.Add(FromListRow(item));
                return;
            }
            if (payload.TryGetProperty("results", out JsonElement results))
            {
                foreach (JsonElement item in Enumerate(results))
                    Rows.Add(FromTestRow(item));
                return;
            }
            if (payload.TryGetProperty("samples", out JsonElement samples))
            {
                foreach (JsonElement item in Enumerate(samples))
                    Rows.Add(FromScreenRow(item));
            }
        }

        private static IEnumerable<JsonElement> Enumerate(JsonElement array)
        {
            if (array.ValueKind != JsonValueKind.Array) yield break;
            foreach (JsonElement item in array.EnumerateArray())
            {
                if (item.ValueKind == JsonValueKind.Object) yield return item;
            }
        }

        private static ProxyPoolRow FromListRow(JsonElement item)
        {
            bool measured = Bool(item, "measured");
            return new ProxyPoolRow
            {
                Proxy = Str(item, "proxy"),
                Country = Str(item, "country"),
                Asn = Str(item, "asn"),
                Isp = Str(item, "isp"),
                NetworkType = Str(item, "network_type"),
                Edge = "",
                Result = measured ? Loc.T("已测量") : Loc.T("未测量"),
                Quality = "",
                Latency = "",
                Eligible = measured,
            };
        }

        private static ProxyPoolRow FromTestRow(JsonElement item)
        {
            bool reachable = Bool(item, "reachable");
            bool countryMatch = Bool(item, "country_match");
            string result = !reachable
                ? Loc.T("不可用")
                : countryMatch
                    ? Loc.T("可用")
                    : Loc.T("地区不符");
            return new ProxyPoolRow
            {
                Proxy = Str(item, "proxy"),
                Country = Str(item, "country"),
                Asn = Str(item, "asn"),
                Isp = Str(item, "isp"),
                NetworkType = Str(item, "network_type"),
                Edge = Str(item, "edge"),
                Result = result,
                Latency = Str(item, "latency_ms"),
                Quality = "",
                Eligible = reachable && (countryMatch || !item.TryGetProperty("country_match", out _)),
            };
        }

        private static ProxyPoolRow FromScreenRow(JsonElement item)
        {
            bool eligible = Bool(item, "eligible");
            string reason = Str(item, "reason");
            return new ProxyPoolRow
            {
                Proxy = Str(item, "proxy"),
                Country = Str(item, "country"),
                Asn = Str(item, "asn"),
                Isp = Str(item, "isp"),
                NetworkType = Str(item, "network_type"),
                Edge = "",
                Result = eligible ? Loc.T("合格") : (reason.Length > 0 ? reason : Str(item, "reason_code")),
                Latency = Str(item, "latency_ms"),
                Quality = Str(item, "quality_score"),
                Eligible = eligible,
            };
        }

        private static string Str(JsonElement item, string name)
            => item.TryGetProperty(name, out JsonElement value) ? (value.ToString() ?? "") : "";

        private static bool Bool(JsonElement item, string name)
            => item.TryGetProperty(name, out JsonElement value) && value.ValueKind == JsonValueKind.True;
    }
}
