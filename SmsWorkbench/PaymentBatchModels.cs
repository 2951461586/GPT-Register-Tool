using CommunityToolkit.Mvvm.ComponentModel;

namespace SmsWorkbench
{
    public sealed record PaymentBatchAccount(string Email, bool HasAccessToken, string AccessToken = "");

    /// <summary>
    /// Payment-owned egress pools used by the batch extractor.  The UI keeps
    /// one proxy per line while the backend receives the same newline-delimited
    /// value and selects/rotates an entry per account and stage.
    /// </summary>
    public sealed record PaymentBatchProxyConfiguration(
        string CheckoutProxyPool = "",
        string ApproveProxyPool = "",
        string CheckoutCountry = "",
        string ApproveCountry = "",
        string UpdateCountry = "");

    public sealed record PaymentProxyCountryOption(string Code, string DisplayName);

    /// <summary>
    /// Test seam for the batch window's catalog-driven country options.  The
    /// production view model passes null and resolves through
    /// <see cref="PaymentMethods"/>; tests supply per-method overrides without
    /// mutating the embedded catalog.
    /// </summary>
    internal interface IPaymentCountryCatalog
    {
        IReadOnlyList<PaymentProxyCountryOption> CheckoutCountryOptions(string paymentMethod);
        IReadOnlyList<PaymentProxyCountryOption> ApproveCountryOptions(string paymentMethod);
    }

    public sealed partial class PaymentMatrixRow : ObservableObject
    {
        [ObservableProperty] private string name = "default";
        [ObservableProperty] private string registrationCountry = "";
        [ObservableProperty] private string checkoutCountry = "";
        [ObservableProperty] private string promotionCountry = "";
        [ObservableProperty] private string providerCountry = "";
        [ObservableProperty] private string approveCountry = "";
        [ObservableProperty] private string redirectCountry = "";
        [ObservableProperty] private string strategy = "";
        [ObservableProperty] private int sampleSize = 1;

        public bool IsValid()
        {
            bool Country(string value) => string.IsNullOrWhiteSpace(value)
                || Regex.IsMatch(value.Trim(), "^[A-Za-z]{2}$");
            return !string.IsNullOrWhiteSpace(Name)
                && SampleSize > 0
                && Country(RegistrationCountry)
                && Country(CheckoutCountry)
                && Country(PromotionCountry)
                && Country(ProviderCountry)
                && Country(ApproveCountry)
                && Country(RedirectCountry);
        }
    }

    public sealed partial class PaymentBatchResultRow : ObservableObject
    {
        [ObservableProperty] private string accountRef = "";
        [ObservableProperty] private string progressText = "0%";
        [ObservableProperty] private double progressPercent;
        [ObservableProperty] private string currentStage = "等待";
        [ObservableProperty] private string resultStatus = "等待";
        public string MatrixCell { get; init; } = "";
        public string AuthStatus { get; init; } = "";
        public string RefreshStatus { get; init; } = "";
        public string Eligibility { get; init; } = "";
        public string Decision { get; init; } = "";
        public string TerminalState { get; init; } = "";
        public string ErrorStage { get; init; } = "";
        public bool Retryable { get; init; }
        public string ResultKind { get; init; } = "";
        public string ResultValue { get; init; } = "";
        public bool ResultPresent { get; init; }
        public bool AuthorizationQueued { get; init; }
        public string AuthorizationStatus { get; init; } = "";
        public string AuthorizationDisplay => AuthorizationQueued
            ? AuthorizationStatus.Length > 0 ? AuthorizationStatus : "pending"
            : "";
        public string ResultDisplay => ResultValue.Length > 0
            ? ResultValue
            : ResultPresent ? "已生成（报告仅保留存在状态）" : Decision;
        public bool HasCopyableResult => ResultValue.Length > 0;
        public string CopyToolTip => HasCopyableResult
            ? $"复制{ResultKind}"
            : ResultPresent ? "报告仅保留支付结果存在状态" : "没有可复制的支付结果";
        public int Attempts { get; init; }
    }

    public sealed record PaymentBatchRequest(
        IReadOnlyList<PaymentBatchAccount> Accounts,
        string PaymentMethod,
        int Workers,
        int Retries,
        int Canary,
        string BatchId,
        string CheckoutProxyPool,
        string ApproveProxyPool,
        string CheckoutCountry,
        string ApproveCountry,
        bool JitRefresh,
        bool ProbeOnly,
        bool RequireZero,
        IReadOnlyList<PaymentMatrixRow> MatrixRows)
    {
        public bool ResumeCheckpoint { get; init; }
    }
}
