using System.Windows.Input;

namespace SmsWorkbench
{
    public sealed partial class ProtocolPaymentViewModel : ObservableObject, IDisposable
    {
        private readonly IProtocolPaymentService _service;
        private CancellationTokenSource _cancellation;
        private string _lastUrl = "";
        private string _lastQrPath = "";

        public ProtocolPaymentViewModel(
            IProtocolPaymentService service,
            IFileLauncher fileLauncher,
            ProtocolPaymentAccount account,
            IStageMatrixStore stageMatrixStore = null)
        {
            _service = service;
            FileLauncher = fileLauncher;
            Account = account;
            StageMatrix = new StageMatrixViewModel(stageMatrixStore);
            ProtocolPaymentPreferences preferences = service.LoadPreferences();
            Methods = PaymentMethods.All.ToArray();
            BillingCountries = PaymentMethods.BillingCountryOptions;
            SelectedMethod = Methods.FirstOrDefault(method => method.Id == PaymentMethods.Normalize(preferences.Method)) ?? Methods[0];
            TargetCountry = ResolveBilling(preferences.TargetCountry, SelectedMethod.DefaultCountry);
            LoadCountriesAndProxyConfiguration(true);

            RunCommand = new AsyncRelayCommand(RunAsync, () => !IsRunning);
            TestProxyCommand = new AsyncRelayCommand(TestProxyAsync, () => !IsRunning);
            SaveProxyCommand = new RelayCommand(SaveProxy, () => !IsRunning);
            CancelCommand = new RelayCommand(Cancel, () => IsRunning);
            CopyCommand = new RelayCommand(CopyResult, () => HasUrl);
            OpenQrCommand = new RelayCommand(OpenQr, () => HasQr);
        }

        public IFileLauncher FileLauncher { get; }
        public ProtocolPaymentAccount Account { get; }
        public bool IsManual => Account == null;
        public bool IsSelectedAccount => Account != null;
        public IReadOnlyList<PaymentMethodDefinition> Methods { get; }
        public IReadOnlyList<PaymentProxyCountryOption> BillingCountries { get; }
        public IReadOnlyList<PaymentProxyCountryOption> CheckoutCountries { get; private set; } = Array.Empty<PaymentProxyCountryOption>();
        public IReadOnlyList<PaymentProxyCountryOption> ApproveCountries { get; private set; } = Array.Empty<PaymentProxyCountryOption>();
        public StageMatrixViewModel StageMatrix { get; }
        public ICommand RunCommand { get; }
        public ICommand TestProxyCommand { get; }
        public ICommand SaveProxyCommand { get; }
        public ICommand CancelCommand { get; }
        public ICommand CopyCommand { get; }
        public ICommand OpenQrCommand { get; }

        [ObservableProperty] private PaymentMethodDefinition selectedMethod;
        [ObservableProperty] private string manualAccessToken = "";
        [ObservableProperty] private string targetCountry = "US";
        [ObservableProperty] private string checkoutProxyPool = "";
        [ObservableProperty] private string approveProxyPool = "";
        [ObservableProperty] private string checkoutCountry = "";
        [ObservableProperty] private string approveCountry = "";
        [ObservableProperty] private string updateCountry = "";
        [ObservableProperty] private string blikCode = "";
        [ObservableProperty] private bool jitRefresh = true;
        [ObservableProperty] private bool probeOnly;
        [ObservableProperty] private bool requireZero = true;
        [ObservableProperty] private bool requireBaToken = true;
        [ObservableProperty] private bool isRunning;
        [ObservableProperty] private string resultText = "";
        [ObservableProperty] private string statusText = "就绪";

        public bool ShowManualToken => IsManual;
        public bool ShowJitAndProbe => IsSelectedAccount;
        public bool ShowProbeOnly => IsSelectedAccount || IsOfflineValidationOnly;
        public bool ShowBlickCode => SelectedMethod?.Id == "blik";
        public bool ShowStageCountries => SelectedMethod != null;
        public bool IsOfflineValidationOnly => SelectedMethod?.Adapter == "regional_wallet";
        public bool CanToggleProbeOnly => ShowProbeOnly;
        public string RunActionText => ProbeOnly ? "开始探测" : SelectedMethod?.Id == "blik" ? "执行支付" : "提取链接";
        public bool CanRequireBa => SelectedMethod?.Id == "paypal" && !ProbeOnly;
        public bool CanRequireZero => !ProbeOnly;
        public bool CanEditUpdateCountry => SelectedMethod?.Id is "paypal" or "gopay" or "direct_card";
        public bool HasUrl => _lastUrl.Length > 0;
        public bool HasQr => _lastQrPath.Length > 0 && FileLauncher.Exists(_lastQrPath);
        public string AccountLabel => Account == null ? "手动 Access Token" : Account.Email;

        partial void OnSelectedMethodChanged(PaymentMethodDefinition value)
        {
            if (value == null) return;
            TargetCountry = ResolveBilling(TargetCountry, value.DefaultCountry);
            LoadCountriesAndProxyConfiguration(true);
            OnPropertyChanged(nameof(ShowBlickCode));
            OnPropertyChanged(nameof(ShowStageCountries));
            OnPropertyChanged(nameof(IsOfflineValidationOnly));
            OnPropertyChanged(nameof(CanToggleProbeOnly));
            OnPropertyChanged(nameof(ShowProbeOnly));
            OnPropertyChanged(nameof(CanRequireBa));
            OnPropertyChanged(nameof(CanEditUpdateCountry));
            OnPropertyChanged(nameof(RunActionText));
        }

        partial void OnProbeOnlyChanged(bool value)
        {
            OnPropertyChanged(nameof(CanRequireBa));
            OnPropertyChanged(nameof(CanRequireZero));
            OnPropertyChanged(nameof(RunActionText));
        }

        partial void OnIsRunningChanged(bool value)
        {
            (RunCommand as AsyncRelayCommand)?.NotifyCanExecuteChanged();
            (TestProxyCommand as AsyncRelayCommand)?.NotifyCanExecuteChanged();
            (SaveProxyCommand as RelayCommand)?.NotifyCanExecuteChanged();
            (CancelCommand as RelayCommand)?.NotifyCanExecuteChanged();
        }

        private void LoadCountriesAndProxyConfiguration(bool loadCountries)
        {
            CheckoutCountries = PaymentMethods.CheckoutCountryOptions(SelectedMethod.Id);
            ApproveCountries = PaymentMethods.ApproveCountryOptions(SelectedMethod.Id);
            OnPropertyChanged(nameof(CheckoutCountries));
            OnPropertyChanged(nameof(ApproveCountries));
            PaymentBatchProxyConfiguration configured = _service.LoadProxyConfiguration(SelectedMethod.Id);
            CheckoutProxyPool = configured.CheckoutProxyPool ?? "";
            ApproveProxyPool = configured.ApproveProxyPool ?? "";
            CheckoutCountry = SelectCountry(configured.CheckoutCountry, CheckoutCountries, SelectedMethod.DefaultCountry);
            ApproveCountry = SelectCountry(configured.ApproveCountry, ApproveCountries, SelectedMethod.DefaultCountry);
            UpdateCountry = SelectCountry(configured.UpdateCountry, ApproveCountries, PaymentMethods.DefaultUpdateCountry(SelectedMethod.Id, SelectedMethod.DefaultCountry));
        }

        private async Task TestProxyAsync()
        {
            IsRunning = true;
            StatusText = "正在测试 checkout / approve / update 代理出口...";
            try
            {
                ResultText = await _service.TestProxiesAsync(
                    SelectedMethod.Id,
                    new PaymentBatchProxyConfiguration(CheckoutProxyPool, ApproveProxyPool, CheckoutCountry, ApproveCountry, UpdateCountry),
                    CancellationToken.None);
                StatusText = "代理探测完成";
            }
            catch (Exception exception)
            {
                ResultText = "[异常] " + SensitiveDataSanitizer.Redact(exception.Message);
                StatusText = "代理探测失败";
            }
            finally
            {
                IsRunning = false;
            }
        }

        private void SaveProxy()
        {
            SettingsSaveResult result = _service.SaveProxyConfiguration(
                SelectedMethod.Id,
                new PaymentBatchProxyConfiguration(CheckoutProxyPool, ApproveProxyPool, CheckoutCountry, ApproveCountry, UpdateCountry));
            ResultText = result.Ok
                ? "[成功] 已保存当前支付方式的 Checkout / Approve-Update 代理池。"
                : "[失败] " + result.Error;
        }

        private async Task RunAsync()
        {
            if (IsOfflineValidationOnly && !ProbeOnly)
            {
                ResultText = "该区域支付方式目前只开放独立适配器离线契约验证；生产 transport 尚未配置。请启用能力探测。";
                StatusText = "仅支持离线验证";
                return;
            }
            if (IsManual && string.IsNullOrWhiteSpace(ManualAccessToken))
            {
                ResultText = "请输入 Access Token";
                return;
            }
            if (!ProbeOnly && SelectedMethod.Id == "blik"
                && (BlikCode.Trim().Length != 6 || !BlikCode.Trim().All(char.IsDigit)))
            {
                ResultText = "请输入有效的 6 位 BLIK Code";
                return;
            }

            SavePreferences();
            StageMatrix.Reset();
            IsRunning = true;
            _cancellation = new CancellationTokenSource();
            _lastUrl = "";
            _lastQrPath = "";
            OnPropertyChanged(nameof(HasUrl));
            OnPropertyChanged(nameof(HasQr));
            StatusText = ProbeOnly ? "正在执行 Checkout / Stripe init 能力探测..." : "正在执行协议支付...";
            var progress = new Progress<BackendOutputLine>(line =>
            {
                if (BackendProgressEventParser.TryParse(line.Text, out BackendProgressEvent progressEvent))
                {
                    StageMatrix.Apply(progressEvent);
                    StatusText = progressEvent.Detail.Length > 0 ? progressEvent.Detail : progressEvent.Stage;
                }
            });
            try
            {
                ProtocolPaymentRunResult outcome = await _service.RunAsync(
                    new ProtocolPaymentRequest(
                        SelectedMethod.Id,
                        ManualAccessToken,
                        TargetCountry,
                        CheckoutProxyPool,
                        ApproveProxyPool,
                        JitRefresh,
                        ProbeOnly,
                        RequireZero,
                        RequireBaToken,
                        BlikCode,
                        CheckoutCountry,
                        ApproveCountry,
                        UpdateCountry,
                        Account),
                    progress,
                    _cancellation.Token);
                ResultText = outcome.Presentation.Text;
                _lastUrl = outcome.Presentation.Url ?? "";
                _lastQrPath = outcome.Presentation.QrPath ?? "";
                StatusText = outcome.Error.Length > 0 ? "执行失败" : "已结束";
                OnPropertyChanged(nameof(HasUrl));
                OnPropertyChanged(nameof(HasQr));
                (CopyCommand as RelayCommand)?.NotifyCanExecuteChanged();
                (OpenQrCommand as RelayCommand)?.NotifyCanExecuteChanged();
            }
            finally
            {
                _cancellation.Dispose();
                _cancellation = null;
                IsRunning = false;
            }
        }

        private void Cancel()
        {
            if (_cancellation == null) return;
            StatusText = "正在取消协议支付任务...";
            _cancellation.Cancel();
        }

        public void Dispose()
        {
            _cancellation?.Cancel();
            _cancellation?.Dispose();
            _cancellation = null;
            GC.SuppressFinalize(this);
        }

        private void CopyResult()
        {
            if (!HasUrl) return;
            Clipboard.SetText(_lastUrl);
            StatusText = "支付链接已复制";
        }

        private void OpenQr()
        {
            if (!HasQr) return;
            FileLauncher.Open(_lastQrPath);
        }

        private void SavePreferences()
        {
            _service.SavePreferences(new ProtocolPaymentPreferences
            {
                Method = SelectedMethod.Id,
                TargetCountry = TargetCountry,
                CheckoutCountry = CheckoutCountry,
                ApproveCountry = ApproveCountry,
                UpdateCountry = UpdateCountry
            });
        }

        private string ResolveBilling(string wanted, string fallback)
            => BillingCountries.Any(country => country.Code.Equals((wanted ?? "").Trim(), StringComparison.OrdinalIgnoreCase))
                ? BillingCountries.First(country => country.Code.Equals(wanted.Trim(), StringComparison.OrdinalIgnoreCase)).Code
                : fallback;

        private static string SelectCountry(string wanted, IReadOnlyList<PaymentProxyCountryOption> options, string fallback)
        {
            string selected = options.FirstOrDefault(option => option.Code.Equals((wanted ?? "").Trim(), StringComparison.OrdinalIgnoreCase))?.Code
                ?? options.FirstOrDefault(option => option.Code.Equals(fallback, StringComparison.OrdinalIgnoreCase))?.Code;
            return selected ?? (options.Count > 0 ? options[0].Code : "");
        }
    }
}
