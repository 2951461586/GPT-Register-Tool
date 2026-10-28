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

        /// <summary>
        /// Config-wide egress-separation verdict: which lanes reuse a registration exit.
        /// <para>
        /// Comes from the backend's <c>lane_isolation</c> block, which is computed
        /// over the whole configuration rather than over <see cref="Lane"/>: the
        /// question "does this signup exit also carry the qualification probe?" is
        /// about the config, and this window is the only place an operator sees it
        /// without running <c>scripts/proxy_census.py</c>. A declared overlap
        /// (<c>proxy.allow_lane_overlap</c>) reports as declared, not as a conflict.
        /// </para>
        /// </summary>
        [ObservableProperty]
        private string laneIsolationText = "";

        /// <summary>True when the isolation text describes a real conflict (renders in Danger).</summary>
        [ObservableProperty]
        private bool laneIsolationWarn;

        /// <summary>False until a payload has actually carried an isolation verdict.</summary>
        [ObservableProperty]
        private bool laneIsolationVisible;

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

        /// <summary>
        /// Measure one row through the same single-proxy path the whole-pool command
        /// uses, and write the answer back into that row.
        /// </summary>
        /// <remarks>
        /// The grid is not rebuilt: a measurement is one row's business, and replacing
        /// the collection would drop the operator's selection, scroll position, and any
        /// screening verdict the other rows already carry. The service is asked with a
        /// one-entry pool, so this needs no new backend operation and is not a second
        /// code path to keep in sync with <see cref="TestAsync"/>.
        /// </remarks>
        /// <remarks>
        /// The command is a thin wrapper so the behaviour is callable without a command
        /// instance: a test does not need the toolkit's generated surface, and neither
        /// does a future call site that measures a row without a click.
        /// </remarks>
        [RelayCommand]
        private Task TestRowAsync(ProxyPoolRow? row, CancellationToken cancellationToken)
            => MeasureRowAsync(row, cancellationToken);

        /// <summary>Measures one row in place. Public so callers other than the button can reuse it.</summary>
        public async Task MeasureRowAsync(ProxyPoolRow? row, CancellationToken cancellationToken = default)
        {
            if (row is null || row.IsTesting || string.IsNullOrWhiteSpace(row.Proxy)) return;

            row.IsTesting = true;
            row.Latency = Loc.T("测速中…");
            try
            {
                JsonElement payload = await _proxyPoolService.TestAsync(
                    new ProxyPoolRequest(Lane, row.Proxy, Country, 1, 6.0, ParseInt(QualityMin, 70)),
                    cancellationToken).ConfigureAwait(true);

                ProxyPoolRow? measured = FirstResultRow(payload);
                if (measured is null)
                {
                    row.Latency = "-";
                    row.Result = Loc.T("无结果");
                    return;
                }

                row.Latency = string.IsNullOrWhiteSpace(measured.Latency) ? "-" : measured.Latency;
                row.Result = measured.Result;
                row.Edge = measured.Edge;
                row.NetworkType = measured.NetworkType;
            }
            catch (OperationCanceledException)
            {
                row.Latency = "-";
                row.Result = Loc.T("已取消");
            }
            catch (Exception ex)
            {
                row.Latency = "-";
                row.Result = Loc.T("失败：") + ex.Message;
            }
            finally
            {
                row.IsTesting = false;
            }
        }

        private static ProxyPoolRow? FirstResultRow(JsonElement payload)
        {
            if (payload.ValueKind != JsonValueKind.Object
                || !payload.TryGetProperty("results", out JsonElement results))
            {
                return null;
            }

            foreach (JsonElement item in Enumerate(results))
            {
                return FromTestRow(item);
            }

            return null;
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
            ApplyLaneIsolation(payload);
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

        /// <summary>
        /// Render the backend's <c>lane_isolation</c> block, if this payload has one.
        /// <para>
        /// Only the list command carries it. A test/screen payload therefore leaves
        /// the banner at its previous value instead of blanking it: the two commands
        /// say nothing about lane isolation, and clearing the line would read as
        /// "checked, all clear" when nothing was checked.
        /// </para>
        /// </summary>
        private void ApplyLaneIsolation(JsonElement payload)
        {
            if (!payload.TryGetProperty("lane_isolation", out JsonElement isolation)
                || isolation.ValueKind != JsonValueKind.Object)
                return;

            bool ok = Bool(isolation, "ok");
            string acknowledged = Names(Conflicts(isolation, "acknowledged"));
            string conflicts = Names(Conflicts(isolation, "conflicts"));
            LaneIsolationVisible = true;
            LaneIsolationWarn = !ok;
            LaneIsolationText = !ok
                ? Loc.F("出口隔离：冲突 — {0} 复用注册出口", conflicts)
                : acknowledged.Length > 0
                    ? Loc.F("出口隔离：已声明复用 {0}", acknowledged)
                    : Loc.T("出口隔离：OK（无 lane 复用注册出口）");
        }

        /// <summary>
        /// Lane names from either the <c>conflicts</c> map (lane -> endpoints) or the
        /// <c>acknowledged</c> list, joined for display. The endpoints themselves are
        /// deliberately not shown: the grid below already lists them.
        /// </summary>
        private static List<string> Conflicts(JsonElement isolation, string name)
        {
            var lanes = new List<string>();
            if (!isolation.TryGetProperty(name, out JsonElement value)) return lanes;
            if (value.ValueKind == JsonValueKind.Object)
            {
                foreach (JsonProperty property in value.EnumerateObject())
                    lanes.Add(property.Name);
            }
            else if (value.ValueKind == JsonValueKind.Array)
            {
                foreach (JsonElement item in value.EnumerateArray())
                    lanes.Add(item.ToString() ?? "");
            }
            return lanes;
        }

        private static string Names(List<string> lanes)
            => string.Join(", ", lanes.Where(lane => lane.Length > 0));

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
