using SmsWorkbench.Localization;

namespace SmsWorkbench.ViewModels
{
    /// <summary>
    /// Window-independent ViewModel for the non-CAPTCHA protocol card quote and
    /// payment window (<c>cs_*</c> Stripe Hosted &amp; <c>oaics_*</c> Custom).
    /// <para>
    /// Sensitive card fields (PAN / CVC) stay strictly in memory for the current
    /// invocation and are wiped automatically in <c>finally</c> once a payment
    /// attempt completes. When the gateway returns <c>challenge_required</c>,
    /// the ViewModel never attempts CAPTCHA bypass; instead it surfaces the
    /// checkout URL so the operator can open it in the browser manually.
    /// </para>
    /// </summary>
    public sealed partial class ProtocolCardPaymentViewModel : ObservableObject
    {
        private readonly IProtocolCardPaymentService _service;
        private readonly Action<string>? _openUrl;
        private readonly Action<string>? _copyText;

        public IReadOnlyList<ProtocolCardCountryOption> BillingCountryOptions { get; }
            = ProtocolCardPaymentCatalog.BillingCountries;

        [ObservableProperty]
        [NotifyPropertyChangedFor(nameof(HasChallengeUrl))]
        [NotifyCanExecuteChangedFor(nameof(OpenCheckoutUrlCommand))]
        private string checkoutUrl = "";

        [ObservableProperty]
        private string publishableKey = "";

        [ObservableProperty]
        private ProtocolCardCountryOption selectedCountry;

        [ObservableProperty]
        private string proxy = "";

        [ObservableProperty]
        private string paymentMethodId = "";

        [ObservableProperty]
        private string cardNumber = "";

        [ObservableProperty]
        private string cardExpMonth = "";

        [ObservableProperty]
        private string cardExpYear = "";

        [ObservableProperty]
        private string cardCvc = "";

        [ObservableProperty]
        private bool readOnlyMode;

        [ObservableProperty]
        private string timeoutText = "60";

        [ObservableProperty]
        [NotifyPropertyChangedFor(nameof(HasBoundAccount))]
        [NotifyPropertyChangedFor(nameof(BoundAccountSummary))]
        private string accountEmail = "";

        [ObservableProperty]
        private string sessionFile = "";

        [ObservableProperty]
        [NotifyCanExecuteChangedFor(nameof(QuoteCommand))]
        [NotifyCanExecuteChangedFor(nameof(PayCommand))]
        [NotifyCanExecuteChangedFor(nameof(ClearCardCommand))]
        [NotifyCanExecuteChangedFor(nameof(CancelRunCommand))]
        private bool isRunning;

        [ObservableProperty]
        private string statusText = "";

        [ObservableProperty]
        private bool hasResult;

        [ObservableProperty]
        private string resultBadgeText = "";

        [ObservableProperty]
        private string resultBadgeSeverity = "neutral";

        [ObservableProperty]
        private string resultModeText = "";

        [ObservableProperty]
        private string amountSummary = "";

        [ObservableProperty]
        private string resultMessage = "";

        [ObservableProperty]
        [NotifyPropertyChangedFor(nameof(HasChallengeUrl))]
        [NotifyCanExecuteChangedFor(nameof(OpenCheckoutUrlCommand))]
        private bool manualActionRequired;

        [ObservableProperty]
        [NotifyPropertyChangedFor(nameof(HasChallengeUrl))]
        [NotifyCanExecuteChangedFor(nameof(OpenCheckoutUrlCommand))]
        private string challengeCheckoutUrl = "";

        [ObservableProperty]
        [NotifyPropertyChangedFor(nameof(HasResultJson))]
        [NotifyCanExecuteChangedFor(nameof(CopyResultJsonCommand))]
        private string resultJson = "";

        [ObservableProperty]
        private int activeStepIndex;

        [ObservableProperty]
        private string stepState = "idle";

        public bool HasBoundAccount => !string.IsNullOrWhiteSpace(AccountEmail);

        public string BoundAccountSummary => HasBoundAccount
            ? Loc.F("关联账号：{0}", AccountEmail.Trim())
            : Loc.T("未绑定账号（oaics_ 模式建议先选中账号）");

        public bool HasChallengeUrl => ManualActionRequired && TryValidateHttpsUrl(EffectiveCheckoutUrl, out _);

        public bool HasResultJson => !string.IsNullOrWhiteSpace(ResultJson);

        private string EffectiveCheckoutUrl => !string.IsNullOrWhiteSpace(ChallengeCheckoutUrl)
            ? ChallengeCheckoutUrl
            : CheckoutUrl;

        public ProtocolCardPaymentViewModel(
            IProtocolCardPaymentService service,
            string initialCheckoutUrl = "",
            string initialAccountEmail = "",
            string initialSessionFile = "",
            Action<string>? openUrl = null,
            Action<string>? copyText = null)
        {
            _service = service ?? throw new ArgumentNullException(nameof(service));
            _openUrl = openUrl;
            _copyText = copyText;
            selectedCountry = BillingCountryOptions[0];
            checkoutUrl = (initialCheckoutUrl ?? "").Trim();
            accountEmail = (initialAccountEmail ?? "").Trim();
            sessionFile = (initialSessionFile ?? "").Trim();
            proxy = _service.ResolveDefaultProxy();
            statusText = Loc.T("就绪：可先查询报价，确认金额后再执行协议支付。");
        }

        private bool CanRun() => !IsRunning;

        private bool CanCancelRun() => IsRunning;

        private bool CanOpenCheckoutUrl() => HasChallengeUrl && _openUrl != null;

        private bool CanCopyResultJson() => HasResultJson && _copyText != null;

        [RelayCommand(CanExecute = nameof(CanRun), IncludeCancelCommand = true)]
        private async Task QuoteAsync(CancellationToken cancellationToken)
        {
            if (!TryValidateCommon(out int timeoutSeconds, out string error))
            {
                StatusText = error;
                return;
            }

            await ExecuteOperationAsync(
                BuildRequest(quoteOnly: true, timeoutSeconds: timeoutSeconds),
                Loc.T("正在查询结账报价…"),
                clearCardAfterRun: false,
                cancellationToken).ConfigureAwait(true);
        }

        [RelayCommand(CanExecute = nameof(CanRun), IncludeCancelCommand = true)]
        private async Task PayAsync(CancellationToken cancellationToken)
        {
            if (!TryValidateCommon(out int timeoutSeconds, out string error))
            {
                StatusText = error;
                return;
            }

            bool isOaics = CheckoutUrl.Contains("oaics_", StringComparison.OrdinalIgnoreCase);
            if (!ReadOnlyMode && isOaics && !string.IsNullOrWhiteSpace(PaymentMethodId) && string.IsNullOrWhiteSpace(CardNumber))
            {
                StatusText = Loc.T("oaics_ 自定义结账不支持复用已有 pm_，请直接填写卡号、有效期与 CVC。");
                return;
            }

            if (!ReadOnlyMode && string.IsNullOrWhiteSpace(PaymentMethodId) && !TryValidateCardInputs(out string cardError))
            {
                StatusText = cardError;
                return;
            }

            await ExecuteOperationAsync(
                BuildRequest(quoteOnly: false, timeoutSeconds: timeoutSeconds),
                ReadOnlyMode ? Loc.T("正在执行只读核账…") : Loc.T("正在执行协议卡付…"),
                clearCardAfterRun: true,
                cancellationToken).ConfigureAwait(true);
        }

        [RelayCommand(CanExecute = nameof(CanCancelRun))]
        public void CancelRun()
        {
            if (QuoteCancelCommand.CanExecute(null))
                QuoteCancelCommand.Execute(null);
            if (PayCancelCommand.CanExecute(null))
                PayCancelCommand.Execute(null);
        }

        [RelayCommand(CanExecute = nameof(CanRun))]
        private void ClearCard()
        {
            ClearSensitiveCardFields();
            PaymentMethodId = "";
            CardExpMonth = "";
            CardExpYear = "";
            StatusText = Loc.T("已清空内存中的卡信息。");
        }

        [RelayCommand(CanExecute = nameof(CanOpenCheckoutUrl))]
        private void OpenCheckoutUrl()
        {
            if (!TryValidateHttpsUrl(EffectiveCheckoutUrl, out string safeUrl))
            {
                StatusText = Loc.T("结账链接必须是有效的 HTTPS 地址。");
                return;
            }

            try
            {
                _openUrl?.Invoke(safeUrl);
                StatusText = Loc.T("已在浏览器打开结账页面，请手动完成验证。");
            }
            catch (Exception ex)
            {
                StatusText = Loc.T("打开浏览器失败：") + ex.Message;
            }
        }

        [RelayCommand(CanExecute = nameof(CanCopyResultJson))]
        private void CopyResultJson()
        {
            if (string.IsNullOrWhiteSpace(ResultJson)) return;
            try
            {
                _copyText?.Invoke(ResultJson);
                StatusText = Loc.T("已复制结果 JSON。");
            }
            catch (Exception ex)
            {
                StatusText = Loc.T("复制失败：") + ex.Message;
            }
        }

        private async Task ExecuteOperationAsync(
            ProtocolCardPaymentRequest request,
            string runningStatus,
            bool clearCardAfterRun,
            CancellationToken cancellationToken)
        {
            IsRunning = true;
            ActiveStepIndex = request.QuoteOnly ? 0 : 1;
            StepState = "running";
            StatusText = runningStatus;
            try
            {
                ProtocolCardPaymentResult result = await _service.ExecuteAsync(request, cancellationToken).ConfigureAwait(true);
                ApplyResult(result);
            }
            catch (OperationCanceledException)
            {
                StepState = "idle";
                StatusText = Loc.T("已取消");
            }
            catch (Exception ex)
            {
                HasResult = true;
                ResultBadgeText = Loc.T("执行失败");
                ResultBadgeSeverity = "danger";
                ActiveStepIndex = 2;
                StepState = "danger";
                ResultModeText = "";
                AmountSummary = "";
                ManualActionRequired = false;
                ChallengeCheckoutUrl = "";
                ResultMessage = SensitiveDataSanitizer.Redact(ex.Message);
                StatusText = Loc.T("失败：") + ResultMessage;
            }
            finally
            {
                if (clearCardAfterRun)
                    ClearSensitiveCardFields();
                IsRunning = false;
            }
        }

        private void ApplyResult(ProtocolCardPaymentResult result)
        {
            HasResult = true;
            ResultBadgeText = result.BadgeLabel;
            ResultBadgeSeverity = result.BadgeSeverity;
            ResultModeText = FormatModeAndSession(result);
            AmountSummary = result.AmountDisplay;
            ResultMessage = result.SummaryMessage;
            ManualActionRequired = result.ManualActionRequired;
            ChallengeCheckoutUrl = result.CheckoutUrl;
            ResultJson = result.RawJson;

            if (result.ManualActionRequired)
            {
                ActiveStepIndex = 2;
                StepState = "warn";
                StatusText = Loc.T("已安全中止：检测到 3DS / 人机挑战，请点击“在浏览器打开结账页”手动完成。");
            }
            else if (result.Ok)
            {
                ActiveStepIndex = result.QuoteOnly ? 1 : 3;
                StepState = "success";
                StatusText = result.AmountDisplay.Length > 0
                    ? $"{result.BadgeLabel} · {result.AmountDisplay}"
                    : result.BadgeLabel;
            }
            else
            {
                ActiveStepIndex = result.QuoteOnly ? 0 : 2;
                StepState = "danger";
                StatusText = $"{result.BadgeLabel} · {result.SummaryMessage}";
            }
        }

        private static string FormatModeAndSession(ProtocolCardPaymentResult result)
        {
            var parts = new List<string>();
            if (!string.IsNullOrWhiteSpace(result.Mode))
                parts.Add(result.Mode);
            if (!string.IsNullOrWhiteSpace(result.SessionId))
                parts.Add(result.SessionId);
            if (!string.IsNullOrWhiteSpace(result.IntentStatus))
                parts.Add("intent=" + result.IntentStatus);
            return string.Join(" · ", parts);
        }

        private ProtocolCardPaymentRequest BuildRequest(bool quoteOnly, int timeoutSeconds)
            => new(
                CheckoutUrl: CheckoutUrl.Trim(),
                PublishableKey: PublishableKey.Trim(),
                BillingCountry: SelectedCountry?.Code ?? "US",
                Proxy: Proxy.Trim(),
                PaymentMethodId: PaymentMethodId.Trim(),
                CardNumber: CardNumber,
                CardExpMonth: CardExpMonth.Trim(),
                CardExpYear: CardExpYear.Trim(),
                CardCvc: CardCvc,
                ReadOnly: ReadOnlyMode,
                Email: AccountEmail.Trim(),
                SessionFile: SessionFile.Trim(),
                QuoteOnly: quoteOnly,
                TimeoutSeconds: timeoutSeconds);

        private bool TryValidateCommon(out int timeoutSeconds, out string error)
        {
            timeoutSeconds = 60;
            error = "";
            string url = (CheckoutUrl ?? "").Trim();
            if (!TryValidateHttpsUrl(url, out _))
            {
                error = Loc.T("请填写有效的 HTTPS 结账链接（cs_ 或 oaics_）。");
                return false;
            }

            string pk = (PublishableKey ?? "").Trim();
            if (pk.Length == 0 || !pk.StartsWith("pk_", StringComparison.OrdinalIgnoreCase))
            {
                error = Loc.T("请填写有效的 Stripe 公钥（pk_ 开头）。");
                return false;
            }

            string rawTimeout = (TimeoutText ?? "").Trim();
            if (rawTimeout.Length > 0)
            {
                if (!int.TryParse(rawTimeout, NumberStyles.Integer, CultureInfo.InvariantCulture, out int parsed)
                    || parsed < 5
                    || parsed > 300)
                {
                    error = Loc.T("超时秒数必须在 5 到 300 之间。");
                    return false;
                }
                timeoutSeconds = parsed;
            }

            return true;
        }

        private bool TryValidateCardInputs(out string error)
        {
            error = "";
            string digits = DigitsOnly(CardNumber);
            if (digits.Length is < 12 or > 19)
            {
                error = Loc.T("请填写卡号（12-19 位数字），或提供已有 PaymentMethod ID（pm_）。");
                return false;
            }

            string monthDigits = DigitsOnly(CardExpMonth);
            if (!int.TryParse(monthDigits, NumberStyles.Integer, CultureInfo.InvariantCulture, out int month)
                || month is < 1 or > 12)
            {
                error = Loc.T("有效期月份必须是 01-12。");
                return false;
            }

            string yearDigits = DigitsOnly(CardExpYear);
            if (yearDigits.Length is not (2 or 4))
            {
                error = Loc.T("有效期年份必须是 2 位或 4 位数字。");
                return false;
            }

            string cvcDigits = DigitsOnly(CardCvc);
            if (cvcDigits.Length is < 3 or > 4)
            {
                error = Loc.T("安全码 (CVC) 必须是 3-4 位数字。");
                return false;
            }

            return true;
        }

        private void ClearSensitiveCardFields()
        {
            CardNumber = "";
            CardCvc = "";
        }

        public static bool TryValidateHttpsUrl(string? raw, out string normalized)
        {
            normalized = "";
            string trimmed = (raw ?? "").Trim();
            if (trimmed.Length == 0) return false;
            if (!Uri.TryCreate(trimmed, UriKind.Absolute, out Uri? uri)) return false;
            if (!string.Equals(uri.Scheme, Uri.UriSchemeHttps, StringComparison.OrdinalIgnoreCase)) return false;
            normalized = uri.AbsoluteUri;
            return true;
        }

        private static string DigitsOnly(string? value)
        {
            if (string.IsNullOrWhiteSpace(value)) return "";
            var builder = new StringBuilder(value.Length);
            foreach (char ch in value)
            {
                if (ch is >= '0' and <= '9')
                    builder.Append(ch);
            }
            return builder.ToString();
        }
    }
}
