using SmsWorkbench;
using System.Net;
using System.Text;

namespace SmsWorkbench.Tests;

/// <summary>
/// The catalog parser, pinned against the **three shapes the three sms-activate
/// vendors actually return** (captured 2026-09-23).
///
/// <para>
/// Why this file exists: the parser used to assume one shape -- the nested one
/// smsbower returns -- and the failure mode was not an exception but a *lie*.
/// grizzly (9310 numbers) and herosms (617597) both came out as
/// "当前没有可用的 OpenAI 号码", because their `service` node holds the price
/// directly and the old code read `service`'s children as offers. Nothing threw,
/// nothing logged, and the operator saw an empty dropdown.
/// </para>
///
/// <para>
/// The payload snippets below are verbatim prefixes of real responses (numbers
/// trimmed to keep them readable; the *shape* and the field names are untouched).
/// </para>
/// </summary>
public sealed class SmsProviderCatalogClientTests
{
    private const string CountriesJson = """
        {
          "1": {"id": 1, "eng": "Ukraine", "chn": "乌克兰"},
          "3": {"id": 3, "eng": "Fiji", "chn": "斐济"},
          "4": {"id": 4, "eng": "Vietnam", "chn": "越南"},
          "151": {"id": 151, "eng": "Chile", "chn": "智利"}
        }
        """;

    /// <summary>smsbower: `country → service → provider_id → {price, count, provider_id}`.</summary>
    private const string SmsBowerPricesJson = """
        {
          "151": {"dr": {
            "2266": {"count": 1, "price": 0.054, "provider_id": 2266},
            "2579": {"count": 122, "price": 0.314, "provider_id": 2579},
            "3001": {"count": 1, "price": 0.179, "provider_id": 3001}
          }}
        }
        """;

    /// <summary>grizzly: `country → service → {price, count}` -- no provider level.</summary>
    private const string GrizzlyPricesJson = """
        {
          "3": {"dr": {"price": 0.013, "count": 240}},
          "151": {"dr": {"price": 0.04, "count": 220}}
        }
        """;

    /// <summary>herosms: `country → service → {cost, count, physicalCount}`.</summary>
    private const string HeroSmsPricesJson = """
        {
          "4": {"dr": {"cost": 0.03, "count": 617597, "physicalCount": 6387}}
        }
        """;

    [Fact]
    public void NestedShapeKeepsEveryProviderAsItsOwnOffer()
    {
        var countries = SmsProviderCatalogClient.ParseCatalog(CountriesJson, SmsBowerPricesJson);

        SmsProviderCountryChoice country = Assert.Single(countries);
        Assert.Equal("151", country.Id);
        Assert.Equal(new[] { "0.054", "0.179", "0.314" },
                     country.Tiers.Select(tier => tier.Price).ToArray());
        Assert.Equal(122, country.Tiers.Single(tier => tier.Price == "0.314").Count);
        Assert.Equal("2579", country.Tiers.Single(tier => tier.Price == "0.314").ProviderIds);
    }

    [Fact]
    public void FlatShapeWithPriceFieldIsRead()
    {
        // Regression: this used to yield zero countries, so the dialog reported
        // "no numbers" for a vendor that had hundreds.
        var countries = SmsProviderCatalogClient.ParseCatalog(CountriesJson, GrizzlyPricesJson);

        // Ordered by English name: Chile (151) before Fiji (3).
        Assert.Equal(new[] { "151", "3" }, countries.Select(country => country.Id).ToArray());
        SmsProviderCountryChoice fiji = countries.Single(country => country.Id == "3");
        Assert.Equal("0.013", Assert.Single(fiji.Tiers).Price);
        Assert.Equal(240, fiji.Tiers[0].Count);
    }

    [Fact]
    public void FlatShapeWithCostFieldIsRead()
    {
        // Regression: herosms calls the same field `cost`, and it also 404s
        // `getPricesV3` -- so this is the shape its `getPrices` answer produces.
        var countries = SmsProviderCatalogClient.ParseCatalog(CountriesJson, HeroSmsPricesJson);

        SmsProviderCountryChoice vietnam = Assert.Single(countries);
        Assert.Equal("4", vietnam.Id);
        Assert.Equal("0.03", Assert.Single(vietnam.Tiers).Price);
        Assert.Equal(617597, vietnam.Tiers[0].Count);
    }

    [Fact]
    public void FlatShapeWinsOverTheProviderLevelInterpretation()
    {
        // A flat node must not be re-read as `provider_id -> offer`: its children
        // are numbers named `price`/`count`, which parse as neither.
        var countries = SmsProviderCatalogClient.ParseCatalog(CountriesJson, GrizzlyPricesJson);

        Assert.All(countries, country => Assert.All(
            country.Tiers,
            tier => Assert.Equal("", tier.ProviderIds)));
    }

    [Fact]
    public void LegacyPriceAsKeyShapeStillParses()
    {
        // No observed vendor answers this today, but the previous parser accepted
        // it -- dropping it silently would turn a working path into an empty list.
        const string prices = """{"151": {"dr": {"0.054": 12, "0.09": 3}}}""";

        SmsProviderCountryChoice chile =
            Assert.Single(SmsProviderCatalogClient.ParseCatalog(CountriesJson, prices));
        Assert.Equal(new[] { "0.054", "0.09" }, chile.Tiers.Select(tier => tier.Price).ToArray());
    }

    [Fact]
    public void ZeroCountOffersAreDropped()
    {
        const string prices = """{"151": {"dr": {"price": 0.04, "count": 0}}}""";

        Assert.Empty(SmsProviderCatalogClient.ParseCatalog(CountriesJson, prices));
    }

    [Fact]
    public void CountriesWithoutTheRequestedServiceAreSkipped()
    {
        const string prices = """{"151": {"wa": {"price": 0.04, "count": 10}}}""";

        Assert.Empty(SmsProviderCatalogClient.ParseCatalog(CountriesJson, prices));
    }

    [Fact]
    public void AnUnrecognisedCountryIdStillProducesAChoiceNamedByItsId()
    {
        // `country_name` is optional in the config, and a vendor can add a country
        // before its name lands in `getCountries`; the id must not be dropped.
        const string prices = """{"9999": {"dr": {"price": 0.04, "count": 10}}}""";

        SmsProviderCountryChoice choice =
            Assert.Single(SmsProviderCatalogClient.ParseCatalog(CountriesJson, prices));
        Assert.Equal("9999", choice.Id);
        Assert.Equal("9999", choice.EnglishName);
        Assert.Equal("国家 9999", choice.DisplayName);
    }

    [Fact]
    public void ANonObjectPricePayloadIsRejectedRatherThanReadAsEmpty()
    {
        // An unparseable payload must fail loudly, not look like "this vendor has
        // no numbers". `ThrowsAny` because the reader throws the more specific
        // JsonReaderException, which derives from JsonException.
        Assert.ThrowsAny<System.Text.Json.JsonException>(
            () => SmsProviderCatalogClient.ParseCatalog(CountriesJson, "not json"));
    }

    [Fact]
    public void PricesAreOrderedCheapestFirst()
    {
        const string prices = """{"151": {"dr": {"price": 0.31, "count": 5}}}""";

        SmsProviderCountryChoice choice =
            Assert.Single(SmsProviderCatalogClient.ParseCatalog(CountriesJson, prices));
        Assert.True(choice.Tiers[0].NumericPrice > 0m);
        Assert.Equal("$0.31 / 个 · 库存 5", choice.Tiers[0].DisplayName);
    }

    [Fact]
    public void AnUnknownInventoryRendersAsUnqueriedRatherThanZero()
    {
        var tier = new SmsProviderPriceTier("0.1207", SmsProviderPriceTier.UnknownCount);

        Assert.Equal("$0.1207 / 个 · 库存未查询", tier.DisplayName);
    }

    [Fact]
    public async Task ThePriceActionFallsBackToTheLegacyOneWhenV3IsMissing()
    {
        // herosms answers `getPricesV3` with 404 and serves only `getPrices`.
        // Without this fallback the whole one-click dialog dead-ends on a 404
        // even though the vendor is perfectly able to rent.
        var handler = new ActionHandler(new Dictionary<string, (HttpStatusCode, string)>
        {
            ["getCountries"] = (HttpStatusCode.OK, CountriesJson),
            ["getPrices"] = (HttpStatusCode.OK, HeroSmsPricesJson),
        });
        using var http = new HttpClient(handler);

        var countries = await SmsProviderCatalogClient.LoadOpenAiCatalogAsync(
            http, "test-key", "https://example.test/api");

        Assert.Equal("4", Assert.Single(countries).Id);
        // The two requests run concurrently, so compare as a multiset, not in order.
        Assert.Equal(3, handler.Actions.Count);
        Assert.Contains("getCountries", handler.Actions);
        Assert.Contains("getPricesV3", handler.Actions);
        Assert.Contains("getPrices", handler.Actions);
    }

    [Fact]
    public async Task TheLegacyActionIsNotRequestedWhenV3Answers()
    {
        // The fallback must be a fallback: one extra round trip per dialog open
        // would be paid by every provider to accommodate one of them.
        var handler = new ActionHandler(new Dictionary<string, (HttpStatusCode, string)>
        {
            ["getCountries"] = (HttpStatusCode.OK, CountriesJson),
            ["getPricesV3"] = (HttpStatusCode.OK, GrizzlyPricesJson),
        });
        using var http = new HttpClient(handler);

        var countries = await SmsProviderCatalogClient.LoadOpenAiCatalogAsync(
            http, "test-key", "https://example.test/api");

        Assert.Equal(2, countries.Count);
        Assert.Equal(2, handler.Actions.Count);
        Assert.Contains("getCountries", handler.Actions);
        Assert.Contains("getPricesV3", handler.Actions);
        Assert.DoesNotContain("getPrices", handler.Actions);
    }

    [Fact]
    public async Task FailingBothActionsNamesBothInTheError()
    {
        // The operator has to be able to tell "the vendor is down" from "we asked
        // for the wrong action" -- so the message must carry both attempts.
        var handler = new ActionHandler(new Dictionary<string, (HttpStatusCode, string)>
        {
            ["getCountries"] = (HttpStatusCode.OK, CountriesJson),
        });
        using var http = new HttpClient(handler);

        var error = await Assert.ThrowsAsync<InvalidDataException>(
            () => SmsProviderCatalogClient.LoadOpenAiCatalogAsync(http, "k", "https://example.test/api"));

        Assert.Contains("getPricesV3", error.Message);
        Assert.Contains("getPrices", error.Message);
    }

    [Fact]
    public async Task TheApiKeyIsSentAndTheEndpointKeepsItsOwnQueryString()
    {
        // A vendor whose base URL already carries `?` must get `&api_key=...`,
        // not a second `?` that silently truncates the key.
        var handler = new ActionHandler(new Dictionary<string, (HttpStatusCode, string)>
        {
            ["getCountries"] = (HttpStatusCode.OK, CountriesJson),
            ["getPricesV3"] = (HttpStatusCode.OK, GrizzlyPricesJson),
        });
        using var http = new HttpClient(handler);

        await SmsProviderCatalogClient.LoadOpenAiCatalogAsync(
            http, "test-key", "https://example.test/api?tenant=1");

        Assert.All(handler.Requested, url =>
        {
            Assert.Contains("?tenant=1&api_key=test-key", url);
            Assert.Equal(1, url.Count(character => character == '?'));
        });
    }

    /// <summary>
    /// Answers by `action=`, recording the requested URLs.
    ///
    /// The action is parsed out of the query string rather than matched with
    /// `Contains`, because `action=getPrices` is a **prefix of**
    /// `action=getPricesV3` -- a substring check would pass whether or not the
    /// fallback actually happened.
    /// </summary>
    private sealed class ActionHandler : HttpMessageHandler
    {
        private readonly Dictionary<string, (HttpStatusCode Status, string Body)> _responses;

        internal ActionHandler(Dictionary<string, (HttpStatusCode, string)> responses)
            => _responses = responses;

        internal List<string> Requested { get; } = new();

        internal List<string> Actions { get; } = new();

        protected override Task<HttpResponseMessage> SendAsync(
            HttpRequestMessage request, CancellationToken cancellationToken)
        {
            string url = request.RequestUri!.ToString();
            Requested.Add(url);
            Actions.Add(QueryValue(url, "action"));

            if (_responses.TryGetValue(Actions[^1], out var reply))
            {
                return Task.FromResult(new HttpResponseMessage(reply.Status)
                {
                    Content = new StringContent(reply.Body, Encoding.UTF8, "application/json"),
                });
            }
            return Task.FromResult(new HttpResponseMessage(HttpStatusCode.NotFound)
            {
                Content = new StringContent(""),
            });
        }

        private static string QueryValue(string url, string name)
        {
            int question = url.IndexOf('?');
            if (question < 0) return "";
            foreach (string pair in url[(question + 1)..].Split('&'))
            {
                int equals = pair.IndexOf('=');
                if (equals > 0 && pair[..equals] == name) return Uri.UnescapeDataString(pair[(equals + 1)..]);
            }
            return "";
        }
    }
}
