using SmsWorkbench;

namespace SmsWorkbench.Tests;

public sealed class PaymentMethodsTests
{
    private static readonly string[] WalletIds = ["gopay", "gcash", "grabpay"];

    [Theory]
    [InlineData("kakao pay", "kakao")]
    [InlineData("upi-qr", "upi")]
    [InlineData("direct", "direct_card")]
    [InlineData("momo_qr", "momo")]
    [InlineData("go-pay", "gopay")]
    [InlineData("grab pay", "grabpay")]
    public void NormalizeKeepsAliasesInsideTheCatalog(string value, string expected)
        => Assert.Equal(expected, PaymentMethods.Normalize(value));

    [Fact]
    public void SingleAccountAndBatchSurfacesUseOneCatalog()
    {
        Assert.Equal(12, PaymentMethods.All.Count);
        Assert.Equal("USD", PaymentMethods.Find("paypal").Currency);
        Assert.Equal("wallet", PaymentMethods.Find("gopay").Adapter);
        Assert.Equal(3, PaymentMethods.All.Count(method => method.Id is "gopay" or "gcash" or "grabpay"));
        Assert.Contains(PaymentMethods.All, method => method.Id == "blik" && !method.BatchEnabled);
        Assert.Contains(PaymentMethods.All, method => method.Id == "direct_card");
        Assert.DoesNotContain(PaymentMethods.BatchOptions, method => method.Id == "blik");
        Assert.All(WalletIds, id =>
        {
            Assert.Contains(PaymentMethods.BatchOptions, method => method.Id == id);
            Assert.Contains(PaymentMethods.RegistrationOptions, method => method.Id == id);
        });
        Assert.All(PaymentMethods.BatchOptions, option =>
            Assert.Contains(PaymentMethods.All, method => method.Id == option.Id));
    }

    [Theory]
    [InlineData("gopay", "ID")]
    [InlineData("gcash", "PH")]
    [InlineData("grabpay", "PH")]
    public void WalletCatalogUsesProviderDefaultCountry(string paymentMethod, string expectedCountry)
        => Assert.Equal(expectedCountry, PaymentMethods.Find(paymentMethod).DefaultCountry);

    [Fact]
    public void UnknownPaymentMethodDoesNotSilentlyBecomePaypal()
        => Assert.Equal("", PaymentMethods.Normalize("not-a-method"));
}
