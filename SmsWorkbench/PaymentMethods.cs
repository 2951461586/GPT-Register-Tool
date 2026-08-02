namespace SmsWorkbench
{
    public sealed record PaymentMethodOption(string Id, string DisplayName);

    public sealed record PaymentMethodDefinition(
        string Id,
        string DisplayName,
        string DefaultCountry,
        string SingleAccountDescription,
        bool BatchEnabled = true);

    public static class PaymentMethods
    {
        public static IReadOnlyList<PaymentMethodDefinition> All { get; } = new[]
        {
            new PaymentMethodDefinition("paypal", "PayPal", "US", "PayPal - 美国/全球 BA 授权链接"),
            new PaymentMethodDefinition("upi", "UPI", "IN", "UPI - 印度统一支付接口"),
            new PaymentMethodDefinition("ideal", "iDEAL", "NL", "iDEAL - 荷兰银行支付"),
            new PaymentMethodDefinition("pix", "PIX", "BR", "PIX - 巴西即时支付"),
            new PaymentMethodDefinition("kakao", "Kakao Pay", "KR", "Kakao Pay - 韩国钱包支付"),
            new PaymentMethodDefinition("blik", "BLIK", "PL", "BLIK - 波兰银行码支付（提交六位码）", BatchEnabled: false),
            new PaymentMethodDefinition("twint", "TWINT", "CH", "TWINT - 瑞士钱包支付"),
            new PaymentMethodDefinition("direct_card", "直卡 Checkout", "PH", "直卡 Checkout - 直接刷卡结账链接"),
            new PaymentMethodDefinition("momo", "MoMo", "VN", "MoMo - 越南钱包扫码支付")
        };

        public static IReadOnlyList<PaymentMethodOption> BatchOptions { get; } = All
            .Where(method => method.BatchEnabled)
            .Select(method => new PaymentMethodOption(method.Id, RegistrationDisplayName(method)))
            .ToArray();

        public static IReadOnlyList<PaymentMethodOption> RegistrationOptions { get; } = BatchOptions;

        public static string Normalize(string? paymentMethod)
        {
            string value = (paymentMethod ?? "").Trim().ToLowerInvariant().Replace("-", "_").Replace(" ", "_");
            return value switch
            {
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

        public static string DisplayName(string? paymentMethod)
            => Find(paymentMethod).DisplayName;

        public static PaymentMethodDefinition Find(string? paymentMethod)
        {
            string normalized = Normalize(paymentMethod);
            return All.First(method => method.Id == normalized);
        }

        private static string RegistrationDisplayName(PaymentMethodDefinition method)
            => method.Id switch
            {
                "paypal" => "PayPal 支付链接",
                "direct_card" => "直卡 Checkout 直连结账",
                "momo" => "MoMo 越南扫码",
                _ => method.DisplayName + " " + CountryName(method.DefaultCountry) + "协议"
            };

        private static string CountryName(string country)
            => country switch
            {
                "IN" => "印度",
                "NL" => "荷兰",
                "BR" => "巴西",
                "KR" => "韩国",
                "CH" => "瑞士",
                _ => ""
            };
    }
}
