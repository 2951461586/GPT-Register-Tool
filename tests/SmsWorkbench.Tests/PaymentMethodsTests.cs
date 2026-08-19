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
        Assert.Equal(15, PaymentMethods.All.Count);
        Assert.Equal("USD", PaymentMethods.Find("paypal").Currency);
        Assert.Equal("wallet", PaymentMethods.Find("gopay").Adapter);
        Assert.Equal(3, PaymentMethods.All.Count(method => method.Id is "gopay" or "gcash" or "grabpay"));
        Assert.Contains(PaymentMethods.All, method => method.Id == "blik" && !method.BatchEnabled);
        Assert.Contains(PaymentMethods.All, method => method.Id == "direct_card");
        Assert.DoesNotContain(PaymentMethods.BatchOptions, method => method.Id == "blik");
        Assert.All(new[] { "qris", "bizum", "naver_pay" }, id =>
        {
            Assert.Contains(PaymentMethods.All, method => method.Id == id && method.BatchEnabled && !method.RegistrationEnabled);
            Assert.Contains(PaymentMethods.BatchOptions, method => method.Id == id);
            Assert.DoesNotContain(PaymentMethods.RegistrationOptions, method => method.Id == id);
        });
        Assert.All(new[] { "qris", "bizum", "naver_pay" }, id =>
            Assert.Contains(PaymentMethods.All, method => method.Id == id && method.Adapter == "regional_wallet"));
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
    public void GoPayUsesThailandForCheckoutUpdate()
    {
        Assert.Equal("TH", PaymentMethods.DefaultUpdateCountry("gopay", "ID"));
        Assert.Equal("PH", PaymentMethods.DefaultUpdateCountry("gcash", "PH"));
    }

    [Fact]
    public void UnknownPaymentMethodDoesNotSilentlyBecomePaypal()
        => Assert.Equal("", PaymentMethods.Normalize("not-a-method"));

    [Fact]
    public void CountryOptionsComeFromTheTopLevelCatalogDefaults()
    {
        Assert.Equal(13, PaymentMethods.CheckoutCountryOptions("momo").Count);
        Assert.Equal(new PaymentProxyCountryOption("US", "美国 US"), PaymentMethods.CheckoutCountryOptions("momo")[0]);
        Assert.Equal(new PaymentProxyCountryOption("BR", "巴西 BR"), PaymentMethods.CheckoutCountryOptions("momo")[12]);
        Assert.Equal(
            PaymentMethods.CheckoutCountryOptions("momo"),
            PaymentMethods.ApproveCountryOptions("momo"));
        Assert.Equal(16, PaymentMethods.StageCountryOptions.Count);
        Assert.Equal(new PaymentProxyCountryOption("US", "美国 US"), PaymentMethods.StageCountryOptions[0]);
        Assert.Equal(20, PaymentMethods.BillingCountryOptions.Count);
        Assert.Equal(new PaymentProxyCountryOption("US", "US - 美国"), PaymentMethods.BillingCountryOptions[0]);
        Assert.Equal(new PaymentProxyCountryOption("IE", "IE - 爱尔兰"), PaymentMethods.BillingCountryOptions[19]);
    }

    [Fact]
    public void CountryOptionsFallBackToTopLevelDefaultsForUnknownMethods()
    {
        Assert.Equal(
            PaymentMethods.CheckoutCountryOptions("momo"),
            PaymentMethods.CheckoutCountryOptions("not-a-method"));
        Assert.Equal(
            PaymentMethods.ApproveCountryOptions("momo"),
            PaymentMethods.ApproveCountryOptions("not-a-method"));
    }

    [Fact]
    public void MethodLevelCountryOverridesWinOverTopLevelDefaults()
    {
        PaymentMethodCatalogDocument catalog = PaymentMethods.ParseCatalog("""
            {
              "schema": "payment_methods.v1",
              "default_method": "paypal",
              "checkout_countries": [{"code":"US","label":"美国 US"}],
              "approve_countries": [{"code":"JP","label":"日本 JP"}],
              "stage_countries": [{"code":"US","label":"美国 US"}],
              "billing_countries": [{"code":"US","label":"US - 美国"}],
              "methods": [
                {"id":"paypal","display_name":"PayPal","country":"US","currency":"USD","adapter":"native_paypal"},
                {
                  "id":"gopay","display_name":"GoPay","country":"ID","currency":"IDR","adapter":"wallet",
                  "checkout_countries": [{"code":"ID","label":"印度尼西亚 ID"}],
                  "approve_countries": [{"code":"TR","label":"土耳其 TR"}]
                }
              ]
            }
            """);

        Assert.Equal(
            new[] { new PaymentProxyCountryOption("ID", "印度尼西亚 ID") },
            PaymentMethods.ResolveCheckoutCountryOptions(catalog, "gopay"));
        Assert.Equal(
            new[] { new PaymentProxyCountryOption("TR", "土耳其 TR") },
            PaymentMethods.ResolveApproveCountryOptions(catalog, "gopay"));
        Assert.Equal(
            new[] { new PaymentProxyCountryOption("US", "美国 US") },
            PaymentMethods.ResolveCheckoutCountryOptions(catalog, "paypal"));
        Assert.Equal(
            new[] { new PaymentProxyCountryOption("JP", "日本 JP") },
            PaymentMethods.ResolveApproveCountryOptions(catalog, "paypal"));
        Assert.Equal(
            new[] { new PaymentProxyCountryOption("US", "美国 US") },
            PaymentMethods.ResolveCheckoutCountryOptions(catalog, "missing"));
    }

    [Fact]
    public void PayPalApproveCountrySelectorUsesTheGeneralCountryList()
    {
        Assert.Contains(
            PaymentMethods.ApproveCountryOptions("paypal"),
            country => country.Code == "TR");
        Assert.Contains(
            PaymentMethods.ApproveCountryOptions("gopay"),
            country => country.Code == "TR");
    }

    [Theory]
    [InlineData("id")]
    [InlineData("USA")]
    [InlineData("1P")]
    public void InvalidMethodCountryCodeNamesTheMethod(string badCode)
    {
        string json = $$"""
            {
              "schema": "payment_methods.v1",
              "default_method": "paypal",
              "checkout_countries": [{"code":"US","label":"美国 US"}],
              "approve_countries": [{"code":"JP","label":"日本 JP"}],
              "stage_countries": [{"code":"US","label":"美国 US"}],
              "billing_countries": [{"code":"US","label":"US - 美国"}],
              "methods": [
                {"id":"paypal","display_name":"PayPal","country":"US","currency":"USD","adapter":"native_paypal"},
                {
                  "id":"gopay","display_name":"GoPay","country":"ID","currency":"IDR","adapter":"wallet",
                  "approve_countries": [{"code":"{{badCode}}","label":"bad"}]
                }
              ]
            }
            """;

        InvalidOperationException exception = Assert.Throws<InvalidOperationException>(
            () => PaymentMethods.ParseCatalog(json));
        Assert.Contains("gopay", exception.Message, StringComparison.Ordinal);
        Assert.Contains(badCode, exception.Message, StringComparison.Ordinal);
    }

    [Fact]
    public void InvalidTopLevelCountryCodeNamesTheList()
    {
        string json = """
            {
              "schema": "payment_methods.v1",
              "default_method": "paypal",
              "checkout_countries": [{"code":"usa","label":"bad"}],
              "approve_countries": [{"code":"JP","label":"日本 JP"}],
              "stage_countries": [{"code":"US","label":"美国 US"}],
              "billing_countries": [{"code":"US","label":"US - 美国"}],
              "methods": [
                {"id":"paypal","display_name":"PayPal","country":"US","currency":"USD","adapter":"native_paypal"}
              ]
            }
            """;

        InvalidOperationException exception = Assert.Throws<InvalidOperationException>(
            () => PaymentMethods.ParseCatalog(json));
        Assert.Contains("checkout_countries", exception.Message, StringComparison.Ordinal);
        Assert.Contains("usa", exception.Message, StringComparison.Ordinal);
    }

    [Fact]
    public void EmptyMethodCountryOverrideIsRejected()
    {
        string json = """
            {
              "schema": "payment_methods.v1",
              "default_method": "paypal",
              "checkout_countries": [{"code":"US","label":"美国 US"}],
              "approve_countries": [{"code":"JP","label":"日本 JP"}],
              "stage_countries": [{"code":"US","label":"美国 US"}],
              "billing_countries": [{"code":"US","label":"US - 美国"}],
              "methods": [
                {"id":"paypal","display_name":"PayPal","country":"US","currency":"USD","adapter":"native_paypal","checkout_countries":[]}
              ]
            }
            """;

        InvalidOperationException exception = Assert.Throws<InvalidOperationException>(
            () => PaymentMethods.ParseCatalog(json));
        Assert.Contains("paypal", exception.Message, StringComparison.Ordinal);
    }
}
