using SmsWorkbench;

namespace SmsWorkbench.Tests;

public sealed class ProxyInputNormalizerTests
{
    [Fact]
    public void BareProviderEntryDefaultsToCanonicalHttpUrl()
    {
        string normalized = ProxyInputNormalizer.Normalize(
            "us.ipwo.net:7878:account_custom_zone_US:password");

        Assert.Equal(
            "http://account_custom_zone_US:password@us.ipwo.net:7878",
            normalized);
    }

    [Theory]
    [InlineData("http://user:pass@host:8080", "http://user:pass@host:8080")]
    [InlineData("socks5://user:pass@host:1080", "socks5://user:pass@host:1080")]
    [InlineData("socks5h://user:pass@host:1080", "socks5h://user:pass@host:1080")]
    [InlineData("host：8080：user：pass", "http://user:pass@host:8080")]
    public void SupportedFormsAreNormalized(string input, string expected)
    {
        Assert.Equal(expected, ProxyInputNormalizer.Normalize(input));
    }

    [Fact]
    public void PoolCountryInferenceRecognizesIpwoCustomZone()
    {
        Assert.Equal(
            "JP",
            ProxyInputNormalizer.InferCountryFromPool(
                "as.ipwo.net:7878:account_custom_zone_JP:password"));
    }

    [Fact]
    public void MixedCountryPoolDoesNotClaimOneCountry()
    {
        Assert.Equal(
            "",
            ProxyInputNormalizer.InferCountryFromPool(
                "us.ipwo.net:7878:account_custom_zone_US:password\n" +
                "as.ipwo.net:7878:account_custom_zone_JP:password"));
    }

    [Fact]
    public void InvalidSchemeErrorEchoesTheOffendingValue()
    {
        // Without the echo the user has to guess which token the validator
        // disliked. With it, the message reads "代理协议「socks4」不支持..."
        // and the offender is obvious from the dialog.
        FormatException exception = Assert.Throws<FormatException>(
            () => ProxyInputNormalizer.Normalize("socks4://host:1080"));
        Assert.Contains("socks4", exception.Message);
    }

    [Fact]
    public void InternalWhitespaceInSchemeIsTolerated()
    {
        // Copy-paste or IME half-state can leave "Socks 5h" with a stray
        // space. It must still normalize to the canonical scheme.
        Assert.Equal(
            "socks5h://host:1080",
            ProxyInputNormalizer.Normalize("Socks 5h://host:1080"));
    }

    [Fact]
    public void MixedCaseSocks5hNormalizesToLowercase()
    {
        Assert.Equal(
            "socks5h://host:1080",
            ProxyInputNormalizer.Normalize("SOCKS5h://host:1080"));
    }

    [Fact]
    public void QuotedJsonArrayEntryIsNormalized()
    {
        // Pasting a JSON array leaves the enclosing quotes on every entry.
        // The quote used to be treated as part of the scheme ("\"http\""),
        // which rejected the whole list with a bogus protocol error.
        Assert.Equal(
            "http://user:pass@us.ipwo.net:7878",
            ProxyInputNormalizer.Normalize("\"http://user:pass@us.ipwo.net:7878\","));
    }

    [Fact]
    public void JsonArrayPasteYieldsCleanProxyList()
    {
        string pasted = string.Join("\n", new[]
        {
            "[",
            "  \"http://acct_custom_zone_US:secret@us.ipwo.net:7878\",",
            "  \"socks5h://acct_custom_zone_US:secret@us.ipwo.net:1080\",",
            "]",
        });

        Assert.Equal(
            new[]
            {
                "http://acct_custom_zone_US:secret@us.ipwo.net:7878",
                "socks5h://acct_custom_zone_US:secret@us.ipwo.net:1080",
            },
            ProxyInputNormalizer.NormalizeList(pasted));
    }

    [Fact]
    public void PasteNoiseOnlyLinesAreDropped()
    {
        // The "[" / "]" lines of a JSON array carry no proxy. They must not
        // end up as blank entries in the saved config.
        Assert.Equal(
            new[] { "http://user:pass@host:7878" },
            ProxyInputNormalizer.NormalizeList("[\n]\n\"http://user:pass@host:7878\",\n"));
    }

    [Fact]
    public void CountryInferenceSurvivesJsonArrayPaste()
    {
        // Regression guard: inference goes through Normalize, so quoting the
        // entry used to make the country undetectable too.
        Assert.Equal(
            "US",
            ProxyInputNormalizer.InferCountry(
                "\"http://lizi1_custom_zone_US_sid_36268881_time_5:pw@us.ipwo.net:7878\""));
    }
}
