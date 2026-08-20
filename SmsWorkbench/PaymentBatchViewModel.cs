using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;
using System.Collections.ObjectModel;
using System.Text.Json;

namespace SmsWorkbench
{
    public sealed partial class PaymentBatchViewModel : ObservableObject
    {
        private static readonly PaymentProxyCountryOption AutomaticCheckoutCountryOption =
            new("", "自动（跟随账单区）");
        private static readonly char[] ManualTokenSeparators = ['\r', '\n', ',', ';'];

        private readonly IPaymentBatchService _paymentBatchService;
        private readonly IFileLauncher _fileLauncher;
        private readonly IPaymentCountryCatalog? _countryCatalog;
        private readonly PaymentBatchAccount[] _accounts;
        private readonly HashSet<string> _terminalProgressAccounts = new(StringComparer.OrdinalIgnoreCase);
        private string _automaticBatchId;
        private bool _acceptProgress;

        [ObservableProperty] private PaymentMethodOption selectedMethod;
        [ObservableProperty] private int workers = 2;
        [ObservableProperty] private int retries = 3;
        [ObservableProperty] private string manualAccessTokens = "";
        [ObservableProperty] private string canaryText = "0";
        [ObservableProperty] private string batchId = "";
        [ObservableProperty] private bool resumeCheckpoint;
        [ObservableProperty] private string checkoutProxyPool = "";
        [ObservableProperty] private string approveProxyPool = "";
        [ObservableProperty] private string checkoutProxyCountry = "";
        [ObservableProperty] private string approveProxyCountry = "";
        [ObservableProperty] private bool jitRefresh = true;
        [ObservableProperty] private bool probeOnly;
        [ObservableProperty] private bool requireZero = true;
        [ObservableProperty] private string status = "就绪";
        [ObservableProperty] private string reportPath = "";
        [ObservableProperty] private bool isRunning;
        [ObservableProperty] private bool hasRun;

        public PaymentBatchViewModel(
            IPaymentBatchService paymentBatchService,
            IFileLauncher fileLauncher,
            IEnumerable<PaymentBatchAccount> accounts)
            : this(paymentBatchService, fileLauncher, accounts, null)
        {
        }

        internal PaymentBatchViewModel(
            IPaymentBatchService paymentBatchService,
            IFileLauncher fileLauncher,
            IEnumerable<PaymentBatchAccount> accounts,
            IPaymentCountryCatalog? countryCatalog)
        {
            _paymentBatchService = paymentBatchService;
            _fileLauncher = fileLauncher;
            _countryCatalog = countryCatalog;
            _accounts = (accounts ?? Array.Empty<PaymentBatchAccount>())
                .Where(account => !string.IsNullOrWhiteSpace(account.Email))
                .GroupBy(account => account.Email.Trim(), StringComparer.OrdinalIgnoreCase)
                .Select(group => group.First() with { Email = group.Key })
                .ToArray();
            PaymentMethodOptions = PaymentMethods.BatchOptions;
            WorkerOptions = Enumerable.Range(1, 10).ToArray();
            RetryOptions = new[] { 0, 1, 2, 3, 4, 5 };
            selectedMethod = PaymentMethodOptions.First(option => option.Id == "momo");
            _automaticBatchId = CreateBatchId(selectedMethod.Id);
            batchId = _automaticBatchId;
            ReloadCountryOptions();
            ReloadProxyConfiguration();
        }

        public IReadOnlyList<PaymentMethodOption> PaymentMethodOptions { get; }

        public IReadOnlyList<int> WorkerOptions { get; }

        public IReadOnlyList<int> RetryOptions { get; }

        public IReadOnlyList<PaymentProxyCountryOption> CheckoutCountryOptions { get; private set; } =
            Array.Empty<PaymentProxyCountryOption>();

        public IReadOnlyList<PaymentProxyCountryOption> ApproveCountryOptions { get; private set; } =
            Array.Empty<PaymentProxyCountryOption>();

        public ObservableCollection<PaymentBatchResultRow> Results { get; } = new();

        public string AccountSummary => _accounts.Length > 0
            ? $"账号 {_accounts.Length}  ·  AT 已获取 {_accounts.Count(account => account.HasAccessToken)}"
            : $"手动 AT {ParseManualAccessTokens().Length} / 10";

        public bool RequireZeroEnabled => !ProbeOnly;
        public bool IsPayPalSelected => string.Equals(SelectedMethod?.Id, "paypal", StringComparison.OrdinalIgnoreCase);

        private bool CanRun()
        {
            int manualCount = ParseManualAccessTokens().Length;
            return !IsRunning && (_accounts.Length > 0 || manualCount is > 0 and <= 10);
        }

        partial void OnResumeCheckpointChanged(bool value)
        {
            OnPropertyChanged(nameof(ExecutionModeSummary));
        }

        public string ExecutionModeSummary => ResumeCheckpoint
            ? "断点恢复（复用当前批次 ID）"
            : "新执行（每次生成新批次 ID）";

        partial void OnManualAccessTokensChanged(string value)
        {
            OnPropertyChanged(nameof(AccountSummary));
            RunCommand.NotifyCanExecuteChanged();
        }

        private bool CanOpenReport() => !IsRunning && _fileLauncher.Exists(ReportPath);

        partial void OnSelectedMethodChanged(PaymentMethodOption value)
        {
            if (value == null) return;
            if (string.IsNullOrWhiteSpace(BatchId) || string.Equals(BatchId, _automaticBatchId, StringComparison.Ordinal))
            {
                _automaticBatchId = CreateBatchId(value.Id);
                BatchId = _automaticBatchId;
            }
            OnPropertyChanged(nameof(RequireZeroEnabled));
            ReloadCountryOptions();
            ReloadProxyConfiguration();
            SaveProxyConfigurationCommand.NotifyCanExecuteChanged();
            OnPropertyChanged(nameof(IsPayPalSelected));
        }

        partial void OnProbeOnlyChanged(bool value)
        {
            OnPropertyChanged(nameof(RequireZeroEnabled));
        }

        partial void OnReportPathChanged(string value) => OpenReportCommand.NotifyCanExecuteChanged();

        partial void OnIsRunningChanged(bool value)
        {
            RunCommand.NotifyCanExecuteChanged();
            SaveProxyConfigurationCommand.NotifyCanExecuteChanged();
            TestProxiesCommand.NotifyCanExecuteChanged();
            OpenReportCommand.NotifyCanExecuteChanged();
        }

        [RelayCommand(CanExecute = nameof(CanOpenReport))]
        private void OpenReport() => _fileLauncher.Open(ReportPath);

        [RelayCommand]
        private void CopyResult(PaymentBatchResultRow row)
        {
            if (row == null || !row.HasCopyableResult) return;
            try
            {
                Clipboard.SetText(row.ResultValue);
                Status = $"已复制{row.ResultKind}：{row.AccountRef}";
            }
            catch (Exception exception)
            {
                Status = "复制失败：" + exception.Message;
            }
        }

        private bool CanSaveProxyConfiguration() => !IsRunning && SelectedMethod != null;

        [RelayCommand(CanExecute = nameof(CanSaveProxyConfiguration))]
        private void SaveProxyConfiguration()
        {
            string method = SelectedMethod?.Id ?? "paypal";
            SettingsSaveResult result = _paymentBatchService.SaveProxyConfiguration(
                method,
                new PaymentBatchProxyConfiguration(
                    CheckoutProxyPool,
                    ApproveProxyPool,
                    CheckoutProxyCountry,
                    ApproveProxyCountry,
                    ApproveProxyCountry));
            Status = result.Ok
                ? $"{PaymentMethods.DisplayName(method)} Checkout / Approve 代理配置已保存。"
                : result.Error;
        }

        [RelayCommand(CanExecute = nameof(CanSaveProxyConfiguration))]
        private async Task TestProxiesAsync(CancellationToken cancellationToken)
        {
            string method = SelectedMethod?.Id ?? "paypal";
            Status = "正在探测 Checkout / Approve 代理出口...";
            IsRunning = true;
            try
            {
                JsonElement report = await _paymentBatchService.ProbeProxiesAsync(
                    method,
                    CheckoutProxyPool ?? "",
                    ApproveProxyPool ?? "",
                    CheckoutProxyCountry ?? "",
                    ApproveProxyCountry ?? "",
                    cancellationToken);
                Status = FormatProxyProbe(report);
            }
            catch (OperationCanceledException)
            {
                Status = "代理探测已取消。";
            }
            catch (TimeoutException)
            {
                Status = "代理探测超时。";
            }
            catch (Exception exception)
            {
                Status = "代理探测失败：" + exception.Message;
            }
            finally
            {
                IsRunning = false;
            }
        }

        private static string FormatProxyProbe(JsonElement report)
        {
            bool ok = report.TryGetProperty("ok", out JsonElement okElement)
                && okElement.ValueKind == JsonValueKind.True;
            var parts = new List<string>();
            if (report.TryGetProperty("stages", out JsonElement stages)
                && stages.ValueKind == JsonValueKind.Object)
            {
                foreach (JsonProperty stage in stages.EnumerateObject())
                {
                    JsonElement value = stage.Value;
                    bool stageOk = value.TryGetProperty("ok", out JsonElement stageOkElement)
                        && stageOkElement.ValueKind == JsonValueKind.True;
                    string cc = JsonString(value, "country_code");
                    string region = JsonString(value, "region");
                    string ip = JsonString(value, "ip");
                    string error = JsonString(value, "error");
                    string where = string.Join("/", new[] { cc, region }.Where(item => item.Length > 0));
                    parts.Add(stageOk
                        ? $"{stage.Name}✓ {where} {ip}".Trim()
                        : $"{stage.Name}✗ {error}".Trim());
                }
            }
            string prefix = ok ? "代理探测通过：" : "代理探测存在问题：";
            return parts.Count > 0 ? prefix + string.Join("  |  ", parts) : prefix + "无可探测的代理";
        }

        [RelayCommand(IncludeCancelCommand = true, CanExecute = nameof(CanRun))]
        private async Task RunAsync(CancellationToken cancellationToken)
        {
            if (!TryCreateRequest(out PaymentBatchRequest request)) return;
            Results.Clear();
            _terminalProgressAccounts.Clear();
            _acceptProgress = true;
            foreach (PaymentBatchAccount account in request.Accounts)
            {
                Results.Add(new PaymentBatchResultRow
                {
                    AccountRef = account.Email,
                    CurrentStage = "等待",
                    ProgressText = "0%",
                    ResultStatus = "等待",
                });
            }
            ReportPath = "";
            Status = ProbeOnly
                ? "正在执行 Checkout 与 Stripe init 支付能力探测..."
                : "正在执行 JIT 探测与协议支付批次...";
            IsRunning = true;
            try
            {
                IProgress<BackendOutputLine> progress = new Progress<BackendOutputLine>(ApplyProgress);
                JsonElement report = _paymentBatchService is IPaymentBatchProgressService progressService
                    ? await progressService.RunAsync(request, progress, cancellationToken)
                    : await _paymentBatchService.RunAsync(request, cancellationToken);
                HasRun = true;
                _acceptProgress = false;
                Results.Clear();
                PopulateResults(report);
                ReportPath = JsonString(report, "report_path");
                string error = JsonString(report, "error");
                string summary = error.Length > 0 && !report.TryGetProperty("counts", out _)
                    ? "执行失败：" + error
                    : FormatSummary(report);
                int resumed = JsonInt(report, "resumed");
                Status = request.ResumeCheckpoint
                    ? $"断点恢复 · 已恢复 {resumed} 个账号 · {summary}"
                    : "新执行 · " + summary;
            }
            catch (OperationCanceledException)
            {
                Status = request.ProbeOnly
                    ? "已取消。"
                    : "结果未知，请先核对批次断点和支付服务状态，不要重试。";
            }
            catch (TimeoutException)
            {
                Status = request.ProbeOnly
                    ? "能力探测已超时，可按策略重试。"
                    : "结果未知，请先核对批次断点和支付服务状态，不要重试。";
            }
            catch (Exception exception)
            {
                Status = "执行失败：" + exception.Message;
            }
            finally
            {
                _acceptProgress = false;
                IsRunning = false;
            }
        }

        private bool TryCreateRequest(out PaymentBatchRequest request)
        {
            request = null;
            if (!int.TryParse(CanaryText.Trim(), out int canary) || canary < 0)
            {
                Status = "Canary 数量必须是非负整数。";
                return false;
            }
            string normalizedBatchId = ResumeCheckpoint
                ? Regex.Replace((BatchId ?? "").Trim(), @"[^A-Za-z0-9_.-]+", "_")
                : CreateBatchId(SelectedMethod?.Id ?? "paypal");
            if (normalizedBatchId.Length == 0) normalizedBatchId = CreateBatchId(SelectedMethod?.Id ?? "paypal");
            BatchId = normalizedBatchId;
            PaymentBatchAccount[] accounts = EffectiveAccounts();
            if (accounts.Length == 0)
            {
                if (ParseManualAccessTokens().Length <= 10)
                    Status = "请选择账号，或输入 1 至 10 个 Access Token。";
                return false;
            }
            request = new PaymentBatchRequest(
                accounts,
                SelectedMethod?.Id ?? "paypal",
                Workers,
                Retries,
                canary,
                normalizedBatchId,
                CheckoutProxyPool ?? "",
                ApproveProxyPool ?? "",
                CheckoutProxyCountry ?? "",
                string.IsNullOrWhiteSpace(ApproveProxyCountry) ? DefaultApproveCountry : ApproveProxyCountry,
                JitRefresh,
                ProbeOnly,
                RequireZero,
                new[] { CreateNeutralMatrixRow() })
            {
                ResumeCheckpoint = ResumeCheckpoint,
            };
            return true;
        }

        private PaymentBatchAccount[] EffectiveAccounts()
        {
            if (_accounts.Length > 0) return _accounts;
            string[] tokens = ParseManualAccessTokens();
            if (tokens.Length > 10)
            {
                Status = "手动 Access Token 最多允许 10 个。";
                return Array.Empty<PaymentBatchAccount>();
            }
            return tokens.Select((token, index) => new PaymentBatchAccount($"AT-{index + 1}", true, token)).ToArray();
        }

        private string[] ParseManualAccessTokens()
            => (ManualAccessTokens ?? "")
                .Split(ManualTokenSeparators, StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries)
                .Where(value => value.Length > 0)
                .Distinct(StringComparer.Ordinal)
                .ToArray();

        private void ApplyProgress(BackendOutputLine line)
        {
            if (!_acceptProgress) return;
            if (!BackendProgressEventParser.TryParse(line.Text, out BackendProgressEvent progress)) return;
            string accountRef = ResolveProgressAccount(progress.AccountRef);
            PaymentBatchResultRow? row = Results.FirstOrDefault(item => item.AccountRef.Equals(accountRef, StringComparison.OrdinalIgnoreCase));
            if (row == null) return;
            bool accountTerminal = progress.AccountTerminal;
            // Backend events can arrive out of order when adapter callbacks and
            // the executor's terminal event share stdout. Never let a stale
            // running event regress a terminal row back to "执行中".
            if (_terminalProgressAccounts.Contains(row.AccountRef)
                && !accountTerminal)
                return;
            int percent = PaymentStageProgress(progress.Stage, accountTerminal, SelectedMethod?.Id);
            if (percent >= row.ProgressPercent || accountTerminal)
            {
                row.ProgressPercent = Math.Max(row.ProgressPercent, percent);
                row.ProgressText = $"{(int)row.ProgressPercent}%";
                row.CurrentStage = PaymentStageLabel(progress.Stage);
            }
            if (accountTerminal)
            {
                _terminalProgressAccounts.Add(row.AccountRef);
                row.ProgressPercent = 100;
                row.ProgressText = "100%";
            }
            row.ResultStatus = accountTerminal
                ? progress.Status.Equals("completed", StringComparison.OrdinalIgnoreCase) ? "成功" : "失败"
                : "执行中";
            Status = $"{accountRef}  {row.CurrentStage}  {row.ProgressText}";
        }

        private string ResolveProgressAccount(string accountRef)
        {
            if (string.IsNullOrWhiteSpace(accountRef)) return "";
            PaymentBatchResultRow exact = Results.FirstOrDefault(row => row.AccountRef.Equals(accountRef, StringComparison.OrdinalIgnoreCase));
            if (exact != null) return exact.AccountRef;
            return ResolveAccountDisplay(accountRef);
        }

        private string ResolveAccountDisplay(string accountRef)
        {
            if (string.IsNullOrWhiteSpace(accountRef)) return "";
            PaymentBatchAccount? account = EffectiveAccounts()
                .FirstOrDefault(item => item.Email.Equals(accountRef, StringComparison.OrdinalIgnoreCase)
                    || PaymentAccountRef(item.Email).Equals(accountRef, StringComparison.OrdinalIgnoreCase));
            return account?.Email ?? accountRef;
        }

        private static string PaymentAccountRef(string value)
        {
            byte[] hash = System.Security.Cryptography.SHA256.HashData(Encoding.UTF8.GetBytes((value ?? "").Trim().ToLowerInvariant()));
            return Convert.ToHexString(hash).ToLowerInvariant()[..16];
        }

        private static int PaymentStageProgress(string stage, bool terminal, string? method = null)
        {
            if (terminal) return 100;
            string normalized = (stage ?? "").Trim().ToLowerInvariant();
            IReadOnlyList<string> stages = Array.Empty<string>();
            try { stages = PaymentMethods.Find(method).Stages ?? Array.Empty<string>(); } catch { }
            if (stages.Count == 0) stages = new[] { "routing", "auth_gate", "checkout", "stripe_init", "provider", "confirm", "redirect", "artifact" };
            int index = Array.FindIndex(stages.ToArray(), item => item == normalized || (normalized == "checkout_create" && item == "checkout") || (normalized == "capability_probe" && item == "stripe_init") || (normalized == "payment_method" && item == "provider"));
            return index < 0 ? 5 : Math.Clamp((index + 1) * 95 / stages.Count, 5, 95);
        }

        private static string PaymentStageLabel(string stage) => (stage ?? "").Trim().ToLowerInvariant() switch
        {
            "routing" => "路由准备",
            "auth_gate" => "AT 验证",
            "checkout" or "checkout_create" => "创建 Checkout",
            "stripe_init" or "capability_probe" => "能力探测",
            "provider" or "payment_method" => "支付方式处理",
            "approve" or "confirm" => "支付确认",
            "redirect" or "promotion" => "结果确认",
            "completed" => "完成",
            _ => string.IsNullOrWhiteSpace(stage) ? "执行中" : stage,
        };

        /// <summary>
        /// Single neutral matrix cell: no registration-country cohort and no
        /// per-cell stage countries, so every account follows the shared
        /// Checkout / Approve proxy settings configured above. Only the
        /// method-owned strategy default (e.g. MoMo custom promo) is kept.
        /// </summary>
        private PaymentMatrixRow CreateNeutralMatrixRow()
        {
            PaymentMatrixRow row = _paymentBatchService.CreateDefaultMatrixRow(SelectedMethod?.Id ?? "paypal");
            row.Name = "default";
            row.RegistrationCountry = "";
            row.CheckoutCountry = "";
            row.PromotionCountry = "";
            row.ProviderCountry = "";
            row.ApproveCountry = "";
            row.RedirectCountry = "";
            row.SampleSize = 1;
            return row;
        }

        private void ReloadCountryOptions()
        {
            if (SelectedMethod == null) return;
            CheckoutCountryOptions = new[] { AutomaticCheckoutCountryOption }
                .Concat(ResolveCheckoutCountryOptions(SelectedMethod.Id))
                .ToArray();
            ApproveCountryOptions = ResolveApproveCountryOptions(SelectedMethod.Id);
            OnPropertyChanged(nameof(CheckoutCountryOptions));
            OnPropertyChanged(nameof(ApproveCountryOptions));
        }

        private IReadOnlyList<PaymentProxyCountryOption> ResolveCheckoutCountryOptions(string paymentMethod)
            => _countryCatalog?.CheckoutCountryOptions(paymentMethod)
                ?? PaymentMethods.CheckoutCountryOptions(paymentMethod);

        private IReadOnlyList<PaymentProxyCountryOption> ResolveApproveCountryOptions(string paymentMethod)
            => _countryCatalog?.ApproveCountryOptions(paymentMethod)
                ?? PaymentMethods.ApproveCountryOptions(paymentMethod);

        private string DefaultApproveCountry => ApproveCountryOptions.Count > 0 ? ApproveCountryOptions[0].Code : "";

        private void ReloadProxyConfiguration()
        {
            if (_paymentBatchService == null || SelectedMethod == null) return;
            PaymentBatchProxyConfiguration configured = _paymentBatchService.LoadProxyConfiguration(SelectedMethod.Id);
            CheckoutProxyPool = configured.CheckoutProxyPool ?? "";
            ApproveProxyPool = configured.ApproveProxyPool ?? "";
            CheckoutProxyCountry = configured.CheckoutCountry ?? "";
            // The configured approve country wins only when the catalog offers
            // it for the selected method; otherwise fall back to the catalog's
            // first approve option instead of a hardcoded JP/TR pair.
            string configuredApproveCountry = (configured.ApproveCountry ?? "").Trim().ToUpperInvariant();
            ApproveProxyCountry = ApproveCountryOptions.Any(option => option.Code == configuredApproveCountry)
                ? configuredApproveCountry
                : DefaultApproveCountry;
        }

        private void PopulateResults(JsonElement report)
        {
            if (!report.TryGetProperty("results", out JsonElement values) || values.ValueKind != JsonValueKind.Array) return;
            foreach (JsonElement row in values.EnumerateArray())
            {
                string eligibility = "未知";
                if (row.TryGetProperty("eligible", out JsonElement eligible)
                    && eligible.ValueKind is JsonValueKind.True or JsonValueKind.False)
                    eligibility = eligible.GetBoolean() ? "符合" : "不符合";
                string decision = JsonString(row, "decision");
                string paymentUrl = FirstNonEmpty(JsonString(row, "url"), JsonString(row, "long_url"));
                string qrData = JsonString(row, "qr_data");
                string qrPath = JsonString(row, "qr_path");
                bool paymentUrlPresent = paymentUrl.Length > 0
                    || JsonBool(row, "url_present")
                    || JsonBool(row, "long_url_present");
                bool qrDataPresent = qrData.Length > 0 || JsonBool(row, "qr_data_present");
                bool qrPathPresent = qrPath.Length > 0 || JsonBool(row, "qr_path_present");
                string terminalState = FirstNonEmpty(
                    JsonString(row, "terminal_state"),
                    JsonString(row, "status"),
                    JsonString(row, "state"));
                if (terminalState.Equals("canceled", StringComparison.OrdinalIgnoreCase))
                    terminalState = "cancelled";
                string resultKind = paymentUrlPresent
                    ? "支付链接"
                    : qrDataPresent
                        ? "二维码内容"
                        : qrPathPresent ? "二维码文件" : "";
                string resultValue = FirstNonEmpty(paymentUrl, qrData, qrPath);
                Results.Add(new PaymentBatchResultRow
                {
                    AccountRef = ResolveAccountDisplay(JsonString(row, "account_ref")),
                    MatrixCell = JsonString(row, "matrix_cell"),
                    AuthStatus = JsonBool(row, "authenticated") ? "200" : "失败",
                    RefreshStatus = JsonBool(row, "refreshed") ? "已刷新" : "未刷新",
                    Eligibility = eligibility,
                    Decision = decision.Length > 0 ? decision : JsonString(row, "error"),
                    TerminalState = terminalState,
                    ErrorStage = JsonString(row, "error_stage"),
                    Retryable = JsonBool(row, "retryable"),
                    ResultKind = resultKind,
                    ResultValue = resultValue,
                    ResultPresent = paymentUrlPresent || qrDataPresent || qrPathPresent,
                    AuthorizationQueued = JsonBool(row, "authorization_queued"),
                    AuthorizationStatus = JsonString(row, "authorization_status"),
                    ProgressPercent = 100,
                    ProgressText = "100%",
                    CurrentStage = "完成",
                    ResultStatus = JsonBool(row, "ok")
                        || terminalState.Equals("completed", StringComparison.OrdinalIgnoreCase)
                        || paymentUrlPresent || qrDataPresent || qrPathPresent
                        ? "成功"
                        : "失败",
                    Attempts = JsonInt(row, "attempts")
                });
            }
        }

        private static string FormatSummary(JsonElement report)
        {
            if (!report.TryGetProperty("counts", out JsonElement counts) || counts.ValueKind != JsonValueKind.Object)
                return "批次已结束，但未返回计数。";
            return $"请求 {JsonInt(counts, "requested")}  ·  AT 200 {JsonInt(counts, "authenticated")}"
                + $"  ·  JIT {JsonInt(counts, "refreshed")}  ·  资格 {JsonInt(counts, "eligible")}"
                + $"  ·  完成 {JsonInt(counts, "completed")}  ·  链接 {JsonInt(counts, "link_ready")}"
                + $"  ·  二维码 {JsonInt(counts, "qr_ready")}  ·  取消 {JsonInt(counts, "cancelled")}"
                + $"  ·  未知 {JsonInt(counts, "unknown")}  ·  超时 {JsonInt(counts, "timed_out")}"
                + $"  ·  失败 {JsonInt(counts, "failed")}  ·  可重试 {JsonInt(counts, "retryable")}"
                + $"  ·  断点恢复 {JsonInt(report, "resumed")}";
        }

        private static string JsonString(JsonElement element, string name)
        {
            if (!element.TryGetProperty(name, out JsonElement value)) return "";
            return value.ValueKind == JsonValueKind.String ? value.GetString() ?? "" : value.ToString();
        }

        private static int JsonInt(JsonElement element, string name)
        {
            if (!element.TryGetProperty(name, out JsonElement value)) return 0;
            if (value.ValueKind == JsonValueKind.Number && value.TryGetInt32(out int number)) return number;
            return int.TryParse(value.ToString(), out number) ? number : 0;
        }

        private static bool JsonBool(JsonElement element, string name)
            => element.TryGetProperty(name, out JsonElement value) && value.ValueKind == JsonValueKind.True;

        private static string FirstNonEmpty(params string[] values)
            => values.FirstOrDefault(value => !string.IsNullOrWhiteSpace(value))?.Trim() ?? "";

        private static string CreateBatchId(string paymentMethod)
            => PaymentMethods.Normalize(paymentMethod) + "_" + DateTime.Now.ToString("yyyyMMdd_HHmmss", CultureInfo.InvariantCulture);
    }
}
