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
}
