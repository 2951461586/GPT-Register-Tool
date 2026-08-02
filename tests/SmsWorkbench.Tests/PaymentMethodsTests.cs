using SmsWorkbench;

namespace SmsWorkbench.Tests;

public sealed class PaymentMethodsTests
{
    [Theory]
    [InlineData("kakao pay", "kakao")]
    [InlineData("upi-qr", "upi")]
    [InlineData("direct", "direct_card")]
    [InlineData("momo_qr", "momo")]
    public void NormalizeKeepsAliasesInsideTheCatalog(string value, string expected)
        => Assert.Equal(expected, PaymentMethods.Normalize(value));

    [Fact]
    public void SingleAccountAndBatchSurfacesUseOneCatalog()
    {
        Assert.Equal(9, PaymentMethods.All.Count);
        Assert.Contains(PaymentMethods.All, method => method.Id == "blik" && !method.BatchEnabled);
        Assert.DoesNotContain(PaymentMethods.BatchOptions, method => method.Id == "blik");
        Assert.All(PaymentMethods.BatchOptions, option =>
            Assert.Contains(PaymentMethods.All, method => method.Id == option.Id));
    }
}
