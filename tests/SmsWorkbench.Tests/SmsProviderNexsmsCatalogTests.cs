using SmsWorkbench;
using System.Net;
using System.Text;

namespace SmsWorkbench.Tests;

/// <summary>
/// The nexsms (`nexsms_json`) catalog reader, pinned against the **real
/// responses** captured 2026-09-23.
///
/// <para>
/// Why this file exists: the dialog used to read only the sms-activate family
/// and fall back to the config for anything else, so selecting NexSMS showed a
/// balance of `--` and exactly one country/tier pair -- the one already in the
/// config. The vendor serves 183 countries for OpenAI and thousands of numbers
/// per tier, none of which was reachable. Nothing threw; the operator just saw
/// a dropdown with one entry.
/// </para>
///
/// <para>
/// The payloads below are trimmed to remain readable, but the *shape* and the
/// field names are untouched: `{code, message, data}`, `data` as an array of
/// country objects, `priceMap` as `{price-string: stock}`, and both
/// `/api/countries` (`{id, name}`) and `/api/getCountryByService`
/// (`countryId`, `countryName`, `priceMap`) as they actually arrived.
/// </para>
/// </summary>
public sealed class SmsProviderNexsmsCatalogTests
{
    private const string CountriesJson = """
        {"code":0,"message":"success","data":[
          {"id":1,"name":"乌克兰"},
          {"id":4,"name":"菲律宾"},
          {"id":6,"name":"印度尼西亚"},
          {"id":151,"name":"智利"}
        ]}
        """;

    /// <summary>
    /// `getCountryByService` with no `countryId`: every country the vendor
    /// serves for this service. Note `countryName` is English here and the
    /// Chinese name only exists in the payload above -- which is why both
    /// endpoints are read.
    /// </summary>
    private const string QuotesJson = """
        {"code":0,"message":"success","data":[
          {"countryId":6,"countryName":"Indonesia","minPrice":0.1207,"maxPrice":1.8903,
           "medianPrice":0.1606,"phoneCode":"+62","serviceCode":"dr",
           "priceMap":{"0.1207":56174,"0.1428":64579,"0.1606":507265}},
          {"countryId":4,"countryName":"Philippines","minPrice":0.0622,"maxPrice":1.4604,
           "medianPrice":0.0692,"phoneCode":"+63","serviceCode":"dr",
           "priceMap":{"0.0622":2287,"0.0692":2441}},
          {"countryId":151,"countryName":"Chile","minPrice":0.2461,"maxPrice":1.9885,
           "medianPrice":0.4309,"phoneCode":"+56","serviceCode":"dr",
           "priceMap":{"0.2461":12,"0.4309":1855}}
        ]}
        """;

    [Fact]
    public void BothPayloadsAreMergedIntoOneChoicePerCountry()
    {
        var countries = SmsProviderCatalogClient.ParseNexsmsCatalog(CountriesJson, QuotesJson);

        // Ordered by English name: Chile, Indonesia, Philippines.
        Assert.Equal(new[] { "151", "6", "4" }, countries.Select(country => country.Id).ToArray());

        SmsProviderCountryChoice indonesia = countries.Single(country => country.Id == "6");
        // The English name comes from the quote payload, the Chinese one from
        // the country payload -- neither carries both.
        Assert.Equal("印度尼西亚 / Indonesia (6)", indonesia.DisplayName);
    }

    [Fact]
    public void EveryPriceMapEntryBecomesATierOrderedCheapestFirst()
    {
        var countries = SmsProviderCatalogClient.ParseNexsmsCatalog(CountriesJson, QuotesJson);

        SmsProviderCountryChoice indonesia = countries.Single(country => country.Id == "6");
        Assert.Equal(new[] { "0.1207", "0.1428", "0.1606" },
                     indonesia.Tiers.Select(tier => tier.Price).ToArray());
        Assert.Equal(56174, indonesia.Tiers[0].Count);
        Assert.Equal("$0.1207 / 个 · 库存 56174", indonesia.Tiers[0].DisplayName);
    }

    [Fact]
    public void TiersAreSortedHereRatherThanTrustingThePayloadOrder()
    {
        // Measured ascending on 2026-09-23, but the ordering is not relied on:
        // `NumericPrice` exists so the dialog can trust the sort, not the wire.
        const string shuffled = """
            {"code":0,"data":[{"countryId":6,"countryName":"Indonesia","priceMap":{
              "0.1606":300,"0.1207":100,"0.1428":200}}]}
            """;

        var countries = SmsProviderCatalogClient.ParseNexsmsCatalog(CountriesJson, shuffled);

        Assert.Equal(new[] { "0.1207", "0.1428", "0.1606" },
                     Assert.Single(countries).Tiers.Select(tier => tier.Price).ToArray());
    }

    [Fact]
    public void PriceStringsArePreservedVerbatim()
    {
        // The price is written back into the config and compared by the backend
        // against its own quote, so `0.1207` must not become `0.12070`.
        const string prices = """
            {"code":0,"data":[{"countryId":6,"countryName":"Indonesia","priceMap":{"0.12070":5}}]}
            """;

        var countries = SmsProviderCatalogClient.ParseNexsmsCatalog(CountriesJson, prices);

        Assert.Equal("0.1207", Assert.Single(countries).Tiers[0].Price);
    }

    [Fact]
    public void ACountryTheVendorQuotesButDoesNotNameStillRents()
    {
        // The two lists disagree by 12 rows in each direction -- a country with
        // offers but no Chinese name is rentable and must not be dropped.
        const string quotes = """
            {"code":0,"data":[{"countryId":9999,"countryName":"Atlantis","priceMap":{"0.5":7}}]}
            """;

        SmsProviderCountryChoice choice =
            Assert.Single(SmsProviderCatalogClient.ParseNexsmsCatalog(CountriesJson, quotes));
        Assert.Equal("9999", choice.Id);
        Assert.Equal("Atlantis (9999)", choice.DisplayName);
    }

    [Fact]
    public void ACountryWithNoOffersIsDroppedRatherThanShownEmpty()
    {
        // A named country with an empty `priceMap` would render as a dropdown
        // entry that rents nothing.
        const string quotes = """
            {"code":0,"data":[
              {"countryId":6,"countryName":"Indonesia","priceMap":{}},
              {"countryId":4,"countryName":"Philippines","priceMap":{"0.0622":2287}}
            ]}
            """;

        SmsProviderCountryChoice choice =
            Assert.Single(SmsProviderCatalogClient.ParseNexsmsCatalog(CountriesJson, quotes));
        Assert.Equal("4", choice.Id);
    }

    [Fact]
    public void ZeroStockTiersAreDroppedLikeTheSmsActivateReader()
    {
        const string quotes = """
            {"code":0,"data":[{"countryId":6,"countryName":"Indonesia","priceMap":{
              "0.1207":0,"0.1428":9}}]}
            """;

        var countries = SmsProviderCatalogClient.ParseNexsmsCatalog(CountriesJson, quotes);

        Assert.Equal("0.1428", Assert.Single(Assert.Single(countries).Tiers).Price);
    }

    [Fact]
    public void AQuotesPayloadThatIsNotAnArrayFailsLoudly()
    {
        // `data: null` is what the vendor answers for a country id it does not
        // serve; read as "no countries" it would look like an empty catalog
        // rather than a wrong request.
        const string quotes = """{"code":0,"message":"success","data":null}""";

        Assert.Throws<InvalidDataException>(
            () => SmsProviderCatalogClient.ParseNexsmsCatalog(CountriesJson, quotes));
    }

    [Fact]
    public void ANonZeroCodeCarriesTheVendorMessage()
    {
        // Only `code == 0` is success. The message has to survive into the
        // exception, because "密钥无效" and "余额不足" need different reactions.
        const string quotes = """{"code":401,"message":"api key invalid","data":null}""";

        var error = Assert.Throws<InvalidDataException>(
            () => SmsProviderCatalogClient.ParseNexsmsCatalog(CountriesJson, quotes));
        Assert.Contains("401", error.Message);
        Assert.Contains("api key invalid", error.Message);
    }

    [Fact]
    public void AStringCodeZeroIsStillSuccess()
    {
        // The Python client makes the same concession (`nexsms._unwrap`
        // compares `int(code)`), and the two must agree: a reply the CLI
        // accepts is a reply the dialog has to accept.
        const string quotes = """
            {"code":"0","message":"success","data":[
              {"countryId":4,"countryName":"Philippines","priceMap":{"0.0622":2287}}]}
            """;

        Assert.Single(SmsProviderCatalogClient.ParseNexsmsCatalog(CountriesJson, quotes));
    }

    [Fact]
    public async Task TheCatalogIsTwoConcurrentRequestsWithTheKeyAsApiKey()
    {
        // The field name is `apiKey`, not `api_key` -- and the paths are the
        // vendor's own `/api/...` ones, not an `?action=` handler. Both
        // differences are silent failures on the wire: a wrong field name is a
        // 401 and a wrong path a 404, neither of which says why.
        var handler = new NexsmsHandler(new Dictionary<string, string>
        {
            ["/api/countries"] = CountriesJson,
            ["/api/getCountryByService"] = QuotesJson,
        });
        using var http = new HttpClient(handler);

        var countries = await SmsProviderCatalogClient.LoadNexsmsCatalogAsync(
            http, "test-key", "https://api.nexsms.net", "dr");

        Assert.Equal(3, countries.Count);
        Assert.Equal(2, handler.Paths.Count);
        Assert.Contains("/api/countries", handler.Paths);
        Assert.Contains("/api/getCountryByService", handler.Paths);
        Assert.All(handler.Requested, url =>
        {
            Assert.Contains("apiKey=test-key", url);
            Assert.DoesNotContain("api_key=", url);
        });
        // The service has to be on the quote request, or the vendor answers
        // with all countries for an unspecified service.
        Assert.Contains(handler.Requested, url => url.Contains("serviceCode=dr"));
    }

    [Fact]
    public async Task TheApiKeyIsSentAndTheEndpointKeepsItsOwnQueryString()
    {
        var handler = new NexsmsHandler(new Dictionary<string, string>
        {
            ["/api/countries"] = CountriesJson,
            ["/api/getCountryByService"] = QuotesJson,
        });
        using var http = new HttpClient(handler);

        await SmsProviderCatalogClient.LoadNexsmsCatalogAsync(
            http, "test-key", "https://api.nexsms.net?tenant=1", "dr");

        Assert.All(handler.Requested, url =>
        {
            Assert.Contains("?tenant=1&apiKey=test-key", url);
            Assert.Equal(1, url.Count(character => character == '?'));
        });
    }

    [Fact]
    public async Task TheBalanceComesOutOfTheEnvelopeAsTheVendorSpelledIt()
    {
        // `"0.2000"` is a string on the wire and is returned verbatim: parsing
        // it to a decimal and back would print `0.2`, a different-looking
        // amount than the vendor reported.
        var handler = new NexsmsHandler(new Dictionary<string, string>
        {
            ["/api/balance"] = """
                {"code":0,"message":"success","data":
                  {"userId":63580,"username":"operator@example.test","balance":"0.2000"}}
                """,
        });
        using var http = new HttpClient(handler);

        string balance = await SmsProviderCatalogClient.LoadNexsmsBalanceAsync(
            http, "test-key", "https://api.nexsms.net");

        Assert.Equal("0.2000", balance);
    }

    [Fact]
    public async Task ABalanceReplyWithoutAnAmountIsRejectedRatherThanShownAsZero()
    {
        // The header would otherwise read "当前平台余额：$" or "$0.00", both of
        // which claim a balance the vendor never stated.
        var handler = new NexsmsHandler(new Dictionary<string, string>
        {
            ["/api/balance"] = """{"code":0,"message":"success","data":{"userId":1}}""",
        });
        using var http = new HttpClient(handler);

        await Assert.ThrowsAsync<InvalidDataException>(
            () => SmsProviderCatalogClient.LoadNexsmsBalanceAsync(
                http, "k", "https://api.nexsms.net"));
    }

    /// <summary>
    /// Answers by path, recording the requested URLs.
    ///
    /// Matched on the path rather than with `Contains`, because
    /// `/api/getCountryByService` and `/api/countries` are distinct enough that
    /// a substring check would need to know which is a prefix of which -- and
    /// it is not, so an inexact check would simply be a weaker one.
    /// </summary>
    private sealed class NexsmsHandler : HttpMessageHandler
    {
        private readonly Dictionary<string, string> _responses;

        internal NexsmsHandler(Dictionary<string, string> responses) => _responses = responses;

        internal List<string> Requested { get; } = new();

        internal List<string> Paths { get; } = new();

        protected override Task<HttpResponseMessage> SendAsync(
            HttpRequestMessage request, CancellationToken cancellationToken)
        {
            string url = request.RequestUri!.ToString();
            Requested.Add(url);
            string path = request.RequestUri!.AbsolutePath;
            Paths.Add(path);

            if (_responses.TryGetValue(path, out string? body))
            {
                return Task.FromResult(new HttpResponseMessage(HttpStatusCode.OK)
                {
                    Content = new StringContent(body, Encoding.UTF8, "application/json"),
                });
            }
            return Task.FromResult(new HttpResponseMessage(HttpStatusCode.NotFound)
            {
                Content = new StringContent(""),
            });
        }
    }
}
