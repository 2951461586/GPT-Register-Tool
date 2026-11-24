using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;
using System.Collections.ObjectModel;
using System.Diagnostics.CodeAnalysis;
using System.Text.Json;

using SmsWorkbench.Localization;

namespace SmsWorkbench.ViewModels
{
    public sealed partial class PaymentBatchViewModel : ObservableObject
    {
        private static PaymentProxyCountryOption AutomaticCheckoutCountryOption =>
            new("", Loc.T("自动（跟随账单区）"));
        private static readonly char[] ManualTokenSeparators = ['\r', '\n', ',', ';'];

        private readonly IPaymentBatchService _paymentBatchService;
        private readonly IFileLauncher _fileLauncher;
        private readonly IPaymentCountryCatalog? _countryCatalog;
        private readonly PaymentBatchAccount[] _accounts;
        private readonly HashSet<string> _terminalProgressAccounts = new(StringComparer.OrdinalIgnoreCase);
        private readonly Dictionary<string, List<string>> _checkoutRegionPools = new(StringComparer.OrdinalIgnoreCase);
        private readonly List<string> _checkoutRegionOrder = new();
        private readonly Dictionary<string, List<string>> _approveRegionPools = new(StringComparer.OrdinalIgnoreCase);
        private readonly List<string> _approveRegionOrder = new();
        private string _automaticBatchId;
        private bool _acceptProgress;

        [ObservableProperty] private PaymentMethodOption selectedMethod;
        [ObservableProperty] private int workers = 2;
        [ObservableProperty] private int retries = 3;
        [ObservableProperty] private string manualAccessTokens = "";
        [ObservableProperty] private string canaryText = "0";
        [ObservableProperty] private string batchId = "";
        [ObservableProperty] private bool resumeCheckpoint;
        [ObservableProperty] private string extractServerApiBase = "";
        [ObservableProperty] private string extractServerCdk = "";
        [ObservableProperty] private string extractServerEndpoint = "/api/v1/extract";
        [ObservableProperty] private string extractServerTimeoutText = "60";
        [ObservableProperty] private bool extractServerPassProxy = true;
        [ObservableProperty] private bool extractServerVerifyTls = true;
        [ObservableProperty] private string checkoutProxyPool = "";
        [ObservableProperty] private string approveProxyPool = "";
        [ObservableProperty] private string checkoutProxyCountry = "";
        [ObservableProperty] private string approveProxyCountry = "";
        [ObservableProperty] private string checkoutRegionSummary = "";
        [ObservableProperty] private string approveRegionSummary = "";
        [ObservableProperty] private bool jitRefresh = true;
        [ObservableProperty] private bool probeOnly;
        [ObservableProperty] private bool requireZero = true;
        [ObservableProperty] private string status = Loc.T("就绪");
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
            string defaultMethodId = PaymentMethods.Normalize(null);
            selectedMethod = PaymentMethodOptions.FirstOrDefault(option => option.Id == defaultMethodId)
                ?? PaymentMethodOptions[0];
            _automaticBatchId = CreateBatchId(selectedMethod.Id);
            batchId = _automaticBatchId;
            ReloadCountryOptions();
            ReloadProxyConfiguration();
            ReloadExtractServerConfiguration();
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
            ? Loc.F("账号 {0}  ·  AT 已获取 {1}", _accounts.Length, _accounts.Count(account => account.HasAccessToken))
            : Loc.F("手动 AT {0} / 10", ParseManualAccessTokens().Length);

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
            ? Loc.T("断点恢复（复用当前批次 ID）")
            : Loc.T("新执行（每次生成新批次 ID）");

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
            SaveExtractServerConfigurationCommand.NotifyCanExecuteChanged();
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
                Status = Loc.F("已复制{0}：{1}", row.ResultKind, row.AccountRef);
            }
            catch (Exception exception)
            {
                Status = Loc.T("复制失败：") + exception.Message;
            }
        }

        private bool CanSaveExtractServerConfiguration() => !IsRunning;

        [RelayCommand(CanExecute = nameof(CanSaveExtractServerConfiguration))]
        private void SaveExtractServerConfiguration()
        {
            if (!TryBuildExtractServerConfiguration(out ExtractLinkServerConfiguration? config))
                return;
            SettingsSaveResult result = _paymentBatchService.SaveExtractServerConfiguration(config);
            Status = result.Ok
                ? Loc.T("提链服务器配置已保存。")
                : result.Error;
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
                    ApproveProxyCountry,
                    PaymentProxyRegionPlanner.FullPool(_checkoutRegionPools, _checkoutRegionOrder),
                    PaymentProxyRegionPlanner.FullPool(_approveRegionPools, _approveRegionOrder)));
            Status = result.Ok
                ? Loc.F("{0} Checkout / Approve 代理配置已保存。", PaymentMethods.DisplayName(method))
                : result.Error;
        }

        [RelayCommand(CanExecute = nameof(CanSaveProxyConfiguration))]
        private async Task TestProxiesAsync(CancellationToken cancellationToken)
        {
            string method = SelectedMethod?.Id ?? "paypal";
            Status = Loc.T("正在探测 Checkout / Approve 代理出口...");
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
                Status = PaymentBatchReportParser.FormatProxyProbe(report);
            }
            catch (OperationCanceledException)
            {
                Status = Loc.T("代理探测已取消。");
            }
            catch (TimeoutException)
            {
                Status = Loc.T("代理探测超时。");
            }
            catch (Exception exception)
            {
                Status = Loc.T("代理探测失败：") + exception.Message;
            }
            finally
            {
                IsRunning = false;
            }
        }

        [RelayCommand(IncludeCancelCommand = true, CanExecute = nameof(CanRun))]
        private async Task RunAsync(CancellationToken cancellationToken)
        {
            if (!TryCreateRequest(out PaymentBatchRequest? request)) return;
            Results.Clear();
            _terminalProgressAccounts.Clear();
            _acceptProgress = true;
            foreach (PaymentBatchAccount account in request.Accounts)
            {
                Results.Add(new PaymentBatchResultRow
                {
                    AccountRef = account.Email,
                    CurrentStage = Loc.T("等待"),
                    ProgressText = "0%",
                    ResultStatus = Loc.T("等待"),
                });
            }
            ReportPath = "";
            Status = ProbeOnly
                ? Loc.T("正在执行 Checkout 与 Stripe init 支付能力探测...")
                : Loc.T("正在执行提链服务器批量提取...");
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
                ReportPath = PaymentBatchReportParser.JsonString(report, "report_path");
                string error = PaymentBatchReportParser.JsonString(report, "error");
                string summary = error.Length > 0 && !report.TryGetProperty("counts", out _)
                    ? Loc.T("执行失败：") + error
                    : PaymentBatchReportParser.FormatSummary(report);
                int resumed = PaymentBatchReportParser.JsonInt(report, "resumed");
                Status = request.ResumeCheckpoint
                    ? Loc.F("断点恢复 · 已恢复 {0} 个账号 · {1}", resumed, summary)
                    : Loc.T("新执行 · ") + summary;
            }
            catch (OperationCanceledException)
            {
                Status = request.ProbeOnly
                    ? Loc.T("已取消。")
                    : Loc.T("结果未知，请先核对批次断点和支付服务状态，不要重试。");
            }
            catch (TimeoutException)
            {
                Status = request.ProbeOnly
                    ? Loc.T("能力探测已超时，可按策略重试。")
                    : Loc.T("结果未知，请先核对批次断点和支付服务状态，不要重试。");
            }
            catch (Exception exception)
            {
                Status = Loc.T("执行失败：") + exception.Message;
            }
            finally
            {
                _acceptProgress = false;
                IsRunning = false;
            }
        }

        private bool TryBuildExtractServerConfiguration([NotNullWhen(true)] out ExtractLinkServerConfiguration? configuration)
        {
            configuration = null;
            string rawTimeout = (ExtractServerTimeoutText ?? "").Trim();
            int timeoutSeconds = 60;
            if (rawTimeout.Length > 0 && (!int.TryParse(rawTimeout, out timeoutSeconds) || timeoutSeconds <= 0))
            {
                Status = Loc.T("提链超时秒数必须是正整数。");
                return false;
            }
            string endpoint = (ExtractServerEndpoint ?? "").Trim();
            if (endpoint.Length == 0)
                endpoint = "/api/v1/extract";
            configuration = new ExtractLinkServerConfiguration(
                (ExtractServerApiBase ?? "").Trim(),
                (ExtractServerCdk ?? "").Trim(),
                endpoint,
                timeoutSeconds,
                ExtractServerPassProxy,
                ExtractServerVerifyTls);
            return true;
        }

        private void ReloadExtractServerConfiguration()
        {
            if (_paymentBatchService == null) return;
            ExtractLinkServerConfiguration configured = _paymentBatchService.LoadExtractServerConfiguration();
            ExtractServerApiBase = configured.ApiBase ?? "";
            ExtractServerCdk = configured.Cdk ?? "";
            ExtractServerEndpoint = string.IsNullOrWhiteSpace(configured.Endpoint) ? "/api/v1/extract" : configured.Endpoint;
            ExtractServerTimeoutText = (configured.TimeoutSeconds > 0 ? configured.TimeoutSeconds : 60).ToString(CultureInfo.InvariantCulture);
            ExtractServerPassProxy = configured.PassProxy;
            ExtractServerVerifyTls = configured.VerifyTls;
        }

        private bool TryCreateRequest([NotNullWhen(true)] out PaymentBatchRequest? request)
        {
            request = null;
            if (!int.TryParse(CanaryText.Trim(), out int canary) || canary < 0)
            {
                Status = Loc.T("Canary 数量必须是非负整数。");
                return false;
            }
            if (!TryBuildExtractServerConfiguration(out ExtractLinkServerConfiguration? extractServer))
                return false;
            string normalizedBatchId = ResumeCheckpoint
                ? Regex.Replace((BatchId ?? "").Trim(), @"[^A-Za-z0-9_.-]+", "_")
                : CreateBatchId(SelectedMethod?.Id ?? "paypal");
            if (normalizedBatchId.Length == 0) normalizedBatchId = CreateBatchId(SelectedMethod?.Id ?? "paypal");
            BatchId = normalizedBatchId;
            PaymentBatchAccount[] accounts = EffectiveAccounts();
            if (accounts.Length == 0)
            {
                if (ParseManualAccessTokens().Length <= 10)
                    Status = Loc.T("请选择账号，或输入 1 至 10 个 Access Token。");
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
                ResolveUpdateCountry(),
                JitRefresh,
                ProbeOnly,
                RequireZero,
                new[] { CreateNeutralMatrixRow() })
            {
                ResumeCheckpoint = ResumeCheckpoint,
                ExtractServer = extractServer,
            };
            return true;
        }

        private PaymentBatchAccount[] EffectiveAccounts()
        {
            if (_accounts.Length > 0) return _accounts;
            string[] tokens = ParseManualAccessTokens();
            if (tokens.Length > 10)
            {
                Status = Loc.T("手动 Access Token 最多允许 10 个。");
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
            if (!BackendProgressEventParser.TryParse(line.Text, out BackendProgressEvent? progress)) return;
            string accountRef = ResolveProgressAccount(progress.AccountRef);
            PaymentBatchResultRow? row = Results.FirstOrDefault(item => item.AccountRef.Equals(accountRef, StringComparison.OrdinalIgnoreCase));
            if (row == null) return;
            bool accountTerminal = progress.AccountTerminal;
            if (_terminalProgressAccounts.Contains(row.AccountRef)
                && !accountTerminal)
                return;
            int percent = PaymentBatchReportParser.PaymentStageProgress(progress.Stage, accountTerminal, SelectedMethod?.Id);
            if (percent >= row.ProgressPercent || accountTerminal)
            {
                row.ProgressPercent = Math.Max(row.ProgressPercent, percent);
                row.ProgressText = $"{(int)row.ProgressPercent}%";
                row.CurrentStage = PaymentBatchReportParser.PaymentStageLabel(progress.Stage);
            }
            if (accountTerminal)
            {
                _terminalProgressAccounts.Add(row.AccountRef);
                row.ProgressPercent = 100;
                row.ProgressText = "100%";
            }
            row.ResultStatus = accountTerminal
                ? progress.Status.Equals("completed", StringComparison.OrdinalIgnoreCase) ? Loc.T("成功") : Loc.T("失败")
                : Loc.T("执行中");
            Status = $"{accountRef}  {row.CurrentStage}  {row.ProgressText}";
        }

        private string ResolveProgressAccount(string accountRef)
        {
            if (string.IsNullOrWhiteSpace(accountRef)) return "";
            PaymentBatchResultRow? exact = Results.FirstOrDefault(row => row.AccountRef.Equals(accountRef, StringComparison.OrdinalIgnoreCase));
            if (exact != null) return exact.AccountRef;
            return ResolveAccountDisplay(accountRef);
        }

        private string ResolveAccountDisplay(string accountRef)
        {
            if (string.IsNullOrWhiteSpace(accountRef)) return "";
            PaymentBatchAccount? account = EffectiveAccounts()
                .FirstOrDefault(item => item.Email.Equals(accountRef, StringComparison.OrdinalIgnoreCase)
                    || PaymentBatchReportParser.PaymentAccountRef(item.Email).Equals(accountRef, StringComparison.OrdinalIgnoreCase));
            return account?.Email ?? accountRef;
        }

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

        private string ResolveUpdateCountry()
        {
            if (_paymentBatchService == null) return "";
            try
            {
                return (_paymentBatchService.LoadProxyConfiguration(SelectedMethod?.Id ?? "paypal").UpdateCountry ?? "")
                    .Trim().ToUpperInvariant();
            }
            catch
            {
                return "";
            }
        }

        private void ReloadProxyConfiguration()
        {
            if (_paymentBatchService == null || SelectedMethod == null) return;
            PaymentBatchProxyConfiguration configured = _paymentBatchService.LoadProxyConfiguration(SelectedMethod.Id);
            string checkoutSource = !string.IsNullOrWhiteSpace(configured.CheckoutProxySourcePool)
                ? configured.CheckoutProxySourcePool
                : configured.CheckoutProxyPool ?? "";
            string approveSource = !string.IsNullOrWhiteSpace(configured.ApproveProxySourcePool)
                ? configured.ApproveProxySourcePool
                : configured.ApproveProxyPool ?? "";
            PaymentProxyRegionPlanner.InitializeBuckets(checkoutSource, _checkoutRegionPools, _checkoutRegionOrder);
            PaymentProxyRegionPlanner.InitializeBuckets(approveSource, _approveRegionPools, _approveRegionOrder);
            CheckoutProxyCountry = configured.CheckoutCountry ?? "";
            string configuredApproveCountry = (configured.ApproveCountry ?? "").Trim().ToUpperInvariant();
            ApproveProxyCountry = ApproveCountryOptions.Any(option => option.Code == configuredApproveCountry)
                ? configuredApproveCountry
                : DefaultApproveCountry;
            RefreshRegionSummaries();
        }

        private void RefreshRegionSummaries()
        {
            CheckoutRegionSummary = PaymentProxyRegionPlanner.FormatRegionSummary(_checkoutRegionPools, _checkoutRegionOrder);
            ApproveRegionSummary = PaymentProxyRegionPlanner.FormatRegionSummary(_approveRegionPools, _approveRegionOrder);
        }

        partial void OnCheckoutProxyPoolChanged(string value)
        {
            PaymentProxyRegionPlanner.SyncDisplay(value ?? "", CheckoutProxyCountry, _checkoutRegionPools, _checkoutRegionOrder);
            RefreshRegionSummaries();
        }

        partial void OnCheckoutProxyCountryChanged(string value)
        {
            CheckoutProxyPool = PaymentProxyRegionPlanner.ComputeDisplay(value ?? "", _checkoutRegionPools, _checkoutRegionOrder);
            RefreshRegionSummaries();
        }

        partial void OnApproveProxyPoolChanged(string value)
        {
            PaymentProxyRegionPlanner.SyncDisplay(value ?? "", ApproveProxyCountry, _approveRegionPools, _approveRegionOrder);
            RefreshRegionSummaries();
        }

        partial void OnApproveProxyCountryChanged(string value)
        {
            ApproveProxyPool = PaymentProxyRegionPlanner.ComputeDisplay(value ?? "", _approveRegionPools, _approveRegionOrder);
            RefreshRegionSummaries();
        }

        private void PopulateResults(JsonElement report)
        {
            foreach (PaymentBatchResultRow resultRow in PaymentBatchReportParser.ParseResults(report, ResolveAccountDisplay))
            {
                Results.Add(resultRow);
                _terminalProgressAccounts.Add(resultRow.AccountRef);
            }
        }

        private static string CreateBatchId(string paymentMethod)
            => PaymentMethods.Normalize(paymentMethod) + "_" + DateTime.Now.ToString("yyyyMMdd_HHmmss", CultureInfo.InvariantCulture);
    }
}
