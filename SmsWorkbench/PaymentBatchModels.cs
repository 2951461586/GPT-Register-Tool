using CommunityToolkit.Mvvm.ComponentModel;

namespace SmsWorkbench
{
    public sealed record PaymentBatchAccount(string Email, bool HasAccessToken);

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

    public sealed class PaymentBatchResultRow
    {
        public string AccountRef { get; init; } = "";
        public string MatrixCell { get; init; } = "";
        public string AuthStatus { get; init; } = "";
        public string RefreshStatus { get; init; } = "";
        public string Eligibility { get; init; } = "";
        public string Decision { get; init; } = "";
        public int Attempts { get; init; }
    }

    public sealed record PaymentBatchRequest(
        IReadOnlyList<PaymentBatchAccount> Accounts,
        string PaymentMethod,
        int Workers,
        int Retries,
        int Canary,
        string BatchId,
        string Proxy,
        bool JitRefresh,
        bool ProbeOnly,
        bool RequireZero,
        IReadOnlyList<PaymentMatrixRow> MatrixRows);
}
