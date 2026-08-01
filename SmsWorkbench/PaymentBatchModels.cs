using CommunityToolkit.Mvvm.ComponentModel;

namespace SmsWorkbench
{
    public sealed record PaymentBatchAccount(string Email, bool HasAccessToken);

    public sealed record PaymentMethodOption(string Id, string DisplayName);

    public static class PaymentMethods
    {
        public static IReadOnlyList<PaymentMethodOption> BatchOptions { get; } = new[]
        {
            new PaymentMethodOption("paypal", "PayPal 支付链接"),
            new PaymentMethodOption("gopay", "GoPay 印尼协议"),
            new PaymentMethodOption("upi", "UPI 印度协议"),
            new PaymentMethodOption("ideal", "iDEAL 荷兰协议"),
            new PaymentMethodOption("pix", "PIX 巴西协议"),
            new PaymentMethodOption("kakao", "Kakao Pay 韩国协议"),
            new PaymentMethodOption("twint", "TWINT 瑞士协议"),
            new PaymentMethodOption("direct_card", "直卡 Checkout 直连结账"),
            new PaymentMethodOption("momo", "MoMo 越南扫码")
        };

        public static string Normalize(string paymentMethod)
        {
            string value = (paymentMethod ?? "").Trim().ToLowerInvariant().Replace("-", "_").Replace(" ", "_");
            return value switch
            {
                "gopay" or "go_pay" => "gopay",
                "upi" or "upiqr" or "upi_qr" => "upi",
                "ideal" => "ideal",
                "pix" => "pix",
                "kakao" or "kakao_pay" => "kakao",
                "blik" => "blik",
                "twint" => "twint",
                "direct_card" or "directcard" or "direct" or "zhika" or "card" or "checkout" => "direct_card",
                "momo" or "momo_qr" or "momoqr" => "momo",
                _ => "paypal"
            };
        }
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
